import asyncio
import json
from uuid import uuid4

import pytest
from sqlalchemy import inspect, select

from laxagent.agent.context import AgentContext
from laxagent.agent.graph import build_graph
from laxagent.context.prompts import system_prompt
from laxagent.memory import MEMORY_CATEGORY_LABELS, MemoryUnavailableError
from laxagent.storage.database import DatabaseError
from laxagent.run_errors import RunError
from laxagent.storage.models import UserConfig
from laxagent.tools.context import ToolContext
from laxagent.tools.executor import execute_tool
from laxagent.tools.registry import MEMORY_TOOLS, tool_definitions
from model_helpers import ModelStream, client_for, completed
from storage_helpers import message_snapshot, run_agent
from test_agent import MemoryEvents, state
from test_memory import tool_response, write_args
from test_web_api import login, web_api


@pytest.mark.anyio
async def test_user_config_schema_defaults_and_atomic_upsert(repository, conversation):
    user = conversation['user_id']
    def check(connection):
        schema = inspect(connection)
        columns = {column['name']: column for column in schema.get_columns('user_configs')}
        assert set(columns) == {'id', 'user_id', 'configs'}
        assert all(not column['nullable'] for column in columns.values())
        assert columns['id']['identity']['always']
        assert str(columns['configs']['type']) == 'JSONB'
        assert schema.get_foreign_keys('user_configs') == []
        assert any(c['column_names'] == ['user_id'] for c in schema.get_unique_constraints('user_configs'))
    async with repository.db.engine.connect() as connection:
        await connection.run_sync(check)
    assert await repository.get_user_configs(user) == {}
    async with repository.db.transaction() as tx:
        assert await tx.scalar(select(UserConfig.id)) is None
    await asyncio.gather(*(repository.update_user_config(user, f'key-{i}', str(i)) for i in range(10)))
    assert await repository.get_user_configs(user) == {f'key-{i}': str(i) for i in range(10)}
    await repository.update_user_config(user, 'key-0', 'new')
    assert (await repository.get_user_configs(user))['key-0'] == 'new'
    other = str(uuid4())
    async with repository.db.transaction() as tx:
        row = UserConfig(user_id=other)
        tx.add(row)
        await tx.flush()
        assert row.configs == {}
    with pytest.raises(DatabaseError):
        async with repository.db.transaction() as tx:
            tx.add(UserConfig(user_id=other, configs={}))
    with pytest.raises(DatabaseError):
        async with repository.db.transaction() as tx:
            tx.add(UserConfig(user_id=str(uuid4()), configs=[]))


@pytest.mark.anyio
async def test_config_api_strict_values_identity_and_preservation(web_api, repository, conversation):
    user = conversation['user_id']
    await login(web_api, user)
    path = '/api/v1/user-configs'
    assert (await web_api.get(path)).json() == {'configs': {}}
    for payload in [{'key': '', 'value': '1'}, {'key': ' \n\u3000', 'value': '1'},
                    {'key': 1, 'value': '1'}, {'key': 'enable_memory', 'value': 1},
                    {'key': 'enable_memory', 'value': True}, {'key': 'x', 'value': None},
                    {'key': 'x', 'value': '1', 'user_id': user}]:
        assert (await web_api.patch(path, json=payload)).status_code == 422
    for key, value in [('enable_memory', '1'), ('custom', ''), (' spaced ', ' value ')]:
        assert (await web_api.patch(path, json={'key': key, 'value': value})).status_code == 200
    expected = {'enable_memory': '1', 'custom': '', ' spaced ': ' value '}
    assert (await web_api.get(path)).json() == {'configs': expected}
    await login(web_api, str(uuid4()))
    assert (await web_api.get(path)).json() == {'configs': {}}
    await web_api.patch(path, json={'key': 'enable_memory', 'value': '0'})
    assert await repository.get_user_configs(user) == expected
    web_api.headers.pop('Authorization')
    for method, url, body in [('GET', path, None), ('PATCH', path, {'key': 'x', 'value': '1'}),
                              ('GET', '/api/v1/memories', None), ('DELETE', '/api/v1/memories', None),
                              ('DELETE', '/api/v1/memories/1', None)]:
        assert (await web_api.request(method, url, json=body)).status_code == 401


@pytest.mark.anyio
async def test_grouped_memories_and_idempotent_owned_deletes(web_api, repository, conversation):
    user = conversation['user_id']
    await login(web_api, user)
    first = await repository.write_memory(user_id=user, **write_args(detail='<script>plain text</script>' * 100))
    second = await repository.write_memory(user_id=user, **write_args())
    other = str(uuid4())
    await repository.create_user(other)
    foreign = await repository.write_memory(user_id=other, **write_args())
    # The default disabled setting does not prevent management or expose foreign rows.
    response = await web_api.get('/api/v1/memories')
    assert response.status_code == 200
    groups = response.json()['categories']
    assert [(g['category'], g['label']) for g in groups] == list(MEMORY_CATEGORY_LABELS.items())
    assert groups[1]['items'] == [{k: row[k] for k in ('id', 'abstract', 'detail')} for row in (first, second)]
    assert all(not g['items'] for g in groups if g['category'] != 'preference')
    await repository.append_message(**conversation, role='assistant', content=first['detail'])
    before = await message_snapshot(repository, conversation['session_id'])
    for memory_id in (foreign['id'], first['id'], first['id'], 2**31 - 1):
        response = await web_api.delete(f'/api/v1/memories/{memory_id}')
        assert response.status_code == 204 and response.content == b''
    assert len(await repository.list_memories(user)) == 1
    for _ in range(2):
        assert (await web_api.delete('/api/v1/memories')).status_code == 204
    assert await repository.list_memories(user) == []
    assert len(await repository.list_memories(other)) == 1
    assert await message_snapshot(repository, conversation['session_id']) == before
    assert (await web_api.delete('/api/v1/memories/0')).status_code == 422
    assert (await web_api.delete('/api/v1/memories/2147483648')).status_code == 422
    await repository.write_memory(user_id=user, **write_args())
    assert len(await repository.list_memories(user)) == 1


@pytest.mark.anyio
@pytest.mark.parametrize('configs', [None, {}, {'enable_memory': '0'}, {'enable_memory': 'true'},
                                     {'enable_memory': ' 1'}, {'enable_memory': ''}, {'enable_memory': 1}])
async def test_disabled_definitions_prompt_and_forged_calls(configs, repository, conversation):
    assert not MEMORY_TOOLS.intersection(t['name'] for t in tool_definitions(configs))
    assert '长期记忆' not in system_prompt('/workspace', configs)
    for name in MEMORY_TOOLS:
        result = await execute_tool(name, '{}', user_configs=configs,
                                    context=ToolContext(conversation['user_id'], repository, workspace='/unused'))
        assert result == {'error_code': 'UNKNOWN_TOOL', 'result': {}}
    assert await repository.list_memories(conversation['user_id']) == []


@pytest.mark.anyio
async def test_run_snapshot_survives_toggle_and_history_survives_deletion(repository, conversation):
    user = conversation['user_id']
    await repository.update_user_config(user, 'enable_memory', '1')
    old = await repository.write_memory(user_id=user, **write_args(detail='remembered sentinel'))
    class ToggleEvents(MemoryEvents):
        async def publish(self, event):
            if event['type'] == 'message_end':
                await repository.update_user_config(user, 'enable_memory', '0')
            await super().publish(event)
    streams = [ModelStream([tool_response('get_memory_details', {'ids': [old['id']]})]),
               ModelStream([tool_response('write_memory', write_args())]), ModelStream([completed()])]
    captured = []
    async with client_for(streams, captured) as client:
        result = await run_agent(build_graph(), state(conversation), AgentContext(client, ToggleEvents(), repository))
    assert result['user_configs'] == {'enable_memory': '1'}
    assert await repository.get_user_configs(user) == {'enable_memory': '0'}
    assert len(await repository.list_memories(user)) == 2
    for request in captured:
        assert MEMORY_TOOLS.issubset(t['name'] for t in request['tools'])
    before = await message_snapshot(repository, conversation['session_id'])
    await repository.delete_memories(user)
    assert await message_snapshot(repository, conversation['session_id']) == before
    captured = []
    streams = [ModelStream([tool_response('write_memory', write_args())]), ModelStream([completed()])]
    async with client_for(streams, captured) as client:
        result = await run_agent(build_graph(), state(conversation), AgentContext(client, MemoryEvents(), repository))
    assert result['user_configs'] == {'enable_memory': '0'}
    assert 'remembered sentinel' in json.dumps(captured[0]['input'])
    assert '长期记忆' not in captured[0]['input'][0]['content']
    assert not MEMORY_TOOLS.intersection(t['name'] for t in captured[0]['tools'])
    assert json.loads(captured[1]['input'][-1]['output'].split('\n', 1)[-1])['error_code'] == 'UNKNOWN_TOOL'
    assert await repository.list_memories(user) == []


@pytest.mark.anyio
async def test_parallel_users_have_independent_snapshots(repository, conversation):
    await repository.update_user_config(conversation['user_id'], 'enable_memory', '1')
    other = str(uuid4())
    await repository.create_user(other)
    project = await repository.create_project(other, 'other')
    session = await repository.create_session(other, project.id)
    conversations = [conversation, {'user_id': other, 'project_id': project.id, 'session_id': session.id}]
    async def execute(conversation):
        captured = []
        async with client_for(ModelStream([completed()]), captured) as client:
            result = await run_agent(build_graph(), state(conversation), AgentContext(client, MemoryEvents(), repository))
        return result, captured
    enabled, disabled = await asyncio.gather(*(execute(c) for c in conversations))
    assert enabled[0]['user_configs'] == {'enable_memory': '1'}
    assert disabled[0]['user_configs'] == {}
    assert MEMORY_TOOLS.issubset(t['name'] for t in enabled[1][0]['tools'])
    assert not MEMORY_TOOLS.intersection(t['name'] for t in disabled[1][0]['tools'])


@pytest.mark.anyio
async def test_config_read_failure_fails_run_before_model(repository, conversation, monkeypatch):
    async def fail(*args):
        raise DatabaseError()
    monkeypatch.setattr(repository, 'get_user_configs', fail)
    captured, events = [], MemoryEvents()
    async with client_for(ModelStream([completed()]), captured) as client:
        with pytest.raises(RunError, match="Run data operation failed"):
            await run_agent(build_graph(), state(conversation), AgentContext(client, events, repository))
    assert captured == []
    assert events.items[-1]['content'] == {'status': 'failed'}


@pytest.mark.anyio
async def test_merge_racing_delete_is_atomic(repository, conversation):
    user = conversation['user_id']
    first = await repository.write_memory(user_id=user, **write_args())
    second = await repository.write_memory(user_id=user, **write_args())
    results = await asyncio.wait_for(asyncio.gather(
        repository.write_memory(user_id=user, **write_args(merge_ids=[first['id'], second['id']])),
        repository.delete_memories(user, first['id']), return_exceptions=True,
    ), 5)
    assert results[1] is None
    remaining = await repository.list_memories(user)
    if isinstance(results[0], MemoryUnavailableError):
        assert [m['id'] for m in remaining] == [second['id']]
    else:
        assert not isinstance(results[0], Exception)
        assert [m['id'] for m in remaining] == [results[0]['id']]
