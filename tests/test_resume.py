import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import select

from laxagent.agent.graph import build_graph
from laxagent.runtime.events import EventStoreError
from laxagent.storage.checkpoints import open_checkpointer
from laxagent.storage.database import DatabaseError
from laxagent.storage.models import Run
from model_helpers import ModelStream, client_for, completed
from storage_helpers import message_snapshot
from test_chat import chat_api, chat_path, payload, wait_run
from test_tool_loop import tool_response


async def resume(h, run_id, attempt=0):
    return await h.api.post(f'/api/v1/runs/{run_id}/resume', json={'resume_time': attempt})


@pytest.mark.anyio
async def test_model_failure_resumes_same_run_with_original_config_and_is_exclusive(chat_api):
    h = chat_api
    captured = []
    async with client_for([ModelStream([]), ModelStream([completed('recovered')])], captured) as client:
        h.context.client = client
        run_id = (await h.api.post(chat_path(h), json=payload())).json()['run_id']
        failed = await wait_run(h, run_id)
        assert failed['error_code'] == 'model_stream_ended'
        assert failed['error_desc'] == '模型回答意外中断，请重试。'
        original = await h.context.repository.get_run(run_id, h.conversation['user_id'])
        await h.context.repository.update_user_config(original.user_id, 'enable_memory', '1')
        results = await asyncio.gather(resume(h, run_id), resume(h, run_id))
        assert sorted(r.status_code for r in results) == [202, 409]
        submitted = next(r.json() for r in results if r.status_code == 202)
        assert submitted == {'run_id': run_id, 'session_id': original.session_id, 'status': 'waiting', 'resume_time': 1}
        ended = await wait_run(h, run_id)
        assert ended['status'] == 'success' and ended['resume_time'] == 1
        assert ended['error'] is ended['error_code'] is ended['error_desc'] is None
        current = await h.context.repository.get_run(run_id, original.user_id)
        assert current.thread_id == original.thread_id and current.created_at == original.created_at
        assert captured[0]['input'] == captured[1]['input'] and captured[0]['tools'] == captured[1]['tools']
        _, rows = await message_snapshot(h.context.repository, original.session_id)
        assert rows[0] == rows[1] and [r.role for r in rows[0]] == ['user', 'assistant']
        events = (await h.api.get(f'/api/v1/runs/{run_id}/events')).text
        assert '"resume_time": 0' not in events and '"resume_time": 1' in events
        assert events.count('event: sse_end') == 1 and 'recovered' in events
        previous = (await h.api.get(f'/api/v1/runs/{run_id}/events?resume_time=0')).text
        assert '"resume_time": 1' not in previous and 'event: error' in previous
        assert (await h.api.get(f'/api/v1/runs/{run_id}/events?resume_time=2')).status_code == 409


@pytest.mark.anyio
async def test_cancelled_tool_gets_unknown_results_then_new_tools_execute(chat_api, monkeypatch):
    h = chat_api
    entered = asyncio.Event()
    executions = []
    async def execute(name, arguments, **kwargs):
        executions.append(arguments)
        if len(executions) == 1:
            entered.set()
            await asyncio.Future()
        return {'error_code': None, 'result': 'checked'}
    monkeypatch.setattr('laxagent.agent.nodes.loop.execute_tool', execute)
    second = tool_response(('check.txt',))
    second['response']['output'][-1]['call_id'] = 'new_call'
    captured = []
    async with client_for([ModelStream([tool_response()]), ModelStream([second]),
                           ModelStream([completed('done')])], captured) as client:
        h.context.client = client
        run_id = (await h.api.post(chat_path(h), json=payload())).json()['run_id']
        await asyncio.wait_for(entered.wait(), 3)
        assert (await h.api.post(f'/api/v1/runs/{run_id}/cancel')).json()['status'] == 'cancelled'
        assert (await resume(h, run_id)).status_code == 202
        assert (await wait_run(h, run_id))['status'] == 'success'
        assert len(executions) == 2  # interrupted call + the new model-requested check
        outputs = [i for i in captured[1]['input'] if i.get('type') == 'function_call_output']
        assert [i['call_id'] for i in outputs] == ['call_0', 'call_1']
        assert all('异常中断，工具执行状态未知，请先检查工作区状态，不要盲目重试' in i['output'] for i in outputs)
        _, rows = await message_snapshot(h.context.repository, h.conversation['session_id'])
        assert rows[0] == rows[1]
        assert sum(r.role == 'user' for r in rows[0]) == 1
        assert sum('TOOL_STATE_UNCERTAIN' in r.content for r in rows[0]) == 2


@pytest.mark.anyio
async def test_postgres_checkpoint_survives_connection_and_graph_recreation(chat_api, migrated_database):
    h = chat_api
    async with client_for([ModelStream([]), ModelStream([completed('after restart')])], []) as client:
        h.context.client = client
        async with open_checkpointer(migrated_database) as saver:
            h.manager.graph = build_graph(saver)
            run_id = (await h.api.post(chat_path(h), json=payload())).json()['run_id']
            assert (await wait_run(h, run_id))['status'] == 'failed'
        async with open_checkpointer(migrated_database) as saver:
            h.manager.graph = build_graph(saver)
            assert (await resume(h, run_id)).status_code == 202
            assert (await wait_run(h, run_id))['status'] == 'success'
            run = await h.context.repository.get_run(run_id, h.conversation['user_id'])
            state = await h.manager.graph.aget_state({'configurable': {'thread_id': run.thread_id}})
            assert not state.next and state.values['messages'][-1]['content'] == 'after restart'
            await saver.adelete_thread(run.thread_id)


@pytest.mark.anyio
@pytest.mark.parametrize('phase', ['prepare', 'save_model'])
async def test_committed_user_and_assistant_writes_replay_without_duplicates(chat_api, monkeypatch, phase):
    h = chat_api
    original = h.context.repository.append_message
    failed = False
    async def append(**kwargs):
        nonlocal failed
        value = await original(**kwargs)
        if not failed and kwargs['role'] == ('user' if phase == 'prepare' else 'assistant'):
            failed = True
            raise DatabaseError('commit acknowledgement lost')
        return value
    monkeypatch.setattr(h.context.repository, 'append_message', append)
    run_id = (await h.api.post(chat_path(h), json=payload())).json()['run_id']
    assert (await wait_run(h, run_id))['status'] == 'failed'
    assert (await resume(h, run_id)).status_code == 202
    assert (await wait_run(h, run_id))['status'] == 'success'
    _, rows = await message_snapshot(h.context.repository, h.conversation['session_id'])
    assert rows[0] == rows[1] and [r.role for r in rows[0]] == ['user', 'assistant']
    assert len(h.captured) == 1


@pytest.mark.anyio
async def test_resume_after_model_end_event_failure_preserves_complete_output(chat_api, monkeypatch):
    h = chat_api
    publish = h.context.events.publish
    failed = False
    async def broken(event):
        nonlocal failed
        if event['type'] == 'message_end' and not failed:
            failed = True
            raise EventStoreError()
        await publish(event)
    monkeypatch.setattr(h.context.events, 'publish', broken)
    run_id = (await h.api.post(chat_path(h), json=payload())).json()['run_id']
    assert (await wait_run(h, run_id))['status'] == 'failed'
    assert (await resume(h, run_id)).status_code == 202
    assert (await wait_run(h, run_id))['status'] == 'success'
    assert len(h.captured) == 1


@pytest.mark.anyio
async def test_completed_graph_only_retries_terminal_commit(chat_api, monkeypatch):
    h = chat_api
    finish = h.context.repository.finish_run
    async def broken(run_id, **kwargs):
        if not kwargs.get('error'):
            raise DatabaseError()
        return await finish(run_id, **kwargs)
    monkeypatch.setattr(h.context.repository, 'finish_run', broken)
    run_id = (await h.api.post(chat_path(h), json=payload())).json()['run_id']
    assert (await wait_run(h, run_id))['status'] == 'failed'
    monkeypatch.setattr(h.context.repository, 'finish_run', finish)
    assert (await resume(h, run_id)).status_code == 202
    assert (await wait_run(h, run_id))['status'] == 'success'
    assert len(h.captured) == 1


@pytest.mark.anyio
async def test_missing_checkpoint_rejected_without_mutation(chat_api):
    h = chat_api
    run = await h.context.repository.create_run(user_id=h.conversation['user_id'], session_id=h.conversation['session_id'],
        user_query='hello', model='example:model-name', context_limit=100, output_limit=10)
    await h.context.events.create_run(run.id)
    await h.context.repository.finish_run(run.id, resume_time=run.resume_time, cancelled=True)
    response = await resume(h, run.id)
    assert response.status_code == 409 and '恢复点' in response.text
    current = (await h.api.get(f'/api/v1/runs/{run.id}')).json()
    assert current['status'] == 'cancelled' and current['resume_time'] == 0


@pytest.mark.anyio
async def test_resume_authorization_latest_status_and_request_validation(chat_api):
    h = chat_api
    run_id = (await h.api.post(chat_path(h), json=payload())).json()['run_id']
    await wait_run(h, run_id)
    assert (await resume(h, run_id)).status_code == 409
    path = f'/api/v1/runs/{run_id}/resume'
    for body in ({}, {'resume_time': -1}, {'resume_time': True}, {'resume_time': '0'}, {'resume_time': 0, 'model': 'x'}):
        assert (await h.api.post(path, json=body)).status_code == 422
    assert (await h.api.post(path, json={'resume_time': 0}, headers={'Authorization': ''})).status_code == 401
    token = (await h.api.post('/api/v1/login', json={'user_id': str(uuid4())})).json()['access_token']
    assert (await h.api.post(path, json={'resume_time': 0}, headers={'Authorization': f'Bearer {token}'})).status_code == 404
    async with client_for(ModelStream([]), []) as client:
        h.context.client = client
        failed_id = (await h.api.post(chat_path(h), json=payload())).json()['run_id']
        await wait_run(h, failed_id)
    newer = await h.manager._submit(dict(user_id=h.conversation['user_id'], session_id=h.conversation['session_id'],
        user_query='new', model='example:model-name', context_limit=100, output_limit=10))
    await h.manager.cancel(newer.user_id, newer.id)
    assert (await resume(h, failed_id)).status_code == 409
    await h.api.delete(f"/api/v1/sessions/{h.conversation['session_id']}")
    assert (await resume(h, newer.id)).status_code == 404


@pytest.mark.anyio
async def test_repeated_failed_resumes_increment_once_and_reject_stale_attempts(chat_api):
    h = chat_api
    async with client_for([ModelStream([]), ModelStream([]), ModelStream([completed('ok')])], []) as client:
        h.context.client = client
        run_id = (await h.api.post(chat_path(h), json=payload())).json()['run_id']
        await wait_run(h, run_id)
        assert (await resume(h, run_id)).status_code == 202
        assert (await wait_run(h, run_id))['status'] == 'failed'
        assert (await resume(h, run_id)).status_code == 409
        assert (await resume(h, run_id, 1)).status_code == 202
        assert (await wait_run(h, run_id))['resume_time'] == 2
        async with h.context.repository.db.transaction() as tx:
            assert len(list(await tx.scalars(select(Run)))) == 1


@pytest.mark.anyio
async def test_resume_does_not_wait_for_old_terminal_events(chat_api, monkeypatch):
    h = chat_api
    publishing, release = asyncio.Event(), asyncio.Event()
    publish = h.context.events.publish
    async def blocked(event):
        if event['type'] == 'error' and event['resume_time'] == 0:
            publishing.set()
            await release.wait()
        await publish(event)
    monkeypatch.setattr(h.context.events, 'publish', blocked)
    async with client_for([ModelStream([]), ModelStream([completed('done')])], []) as client:
        h.context.client = client
        run_id = (await h.api.post(chat_path(h), json=payload())).json()['run_id']
        await asyncio.wait_for(publishing.wait(), 3)
        assert (await h.api.get(f'/api/v1/runs/{run_id}')).json()['status'] == 'failed'
        pending = asyncio.create_task(resume(h, run_id))
        try:
            async with asyncio.timeout(2):
                while not h.manager.controls:
                    await asyncio.sleep(0)
            assert (await asyncio.wait_for(pending, 2)).status_code == 202
        finally:
            release.set()
        assert (await wait_run(h, run_id))['status'] == 'success'


@pytest.mark.anyio
async def test_each_resume_gets_a_new_graph_step_budget(chat_api):
    h = chat_api
    h.context.recursion_limit = 1
    run_id = (await h.api.post(chat_path(h), json=payload())).json()['run_id']
    assert (await wait_run(h, run_id))['error_code'] == 'graph_step_limit_reached'
    response = await resume(h, run_id)
    assert response.status_code == 202
    ended = await wait_run(h, run_id)
    assert ended['status'] == 'success' and ended['resume_time'] == 1
    assert len(h.captured) == 1


@pytest.mark.anyio
async def test_resume_survives_http_disconnect_and_can_be_cancelled_again(chat_api, monkeypatch):
    h = chat_api
    entered, release, generating = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = h.context.repository.resume_run
    async def blocked(*args):
        entered.set()
        await release.wait()
        return await original(*args)
    async with client_for([ModelStream([]), ModelStream([], wait=generating)], []) as client:
        h.context.client = client
        run_id = (await h.api.post(chat_path(h), json=payload())).json()['run_id']
        await wait_run(h, run_id)
        monkeypatch.setattr(h.context.repository, 'resume_run', blocked)
        request = asyncio.create_task(resume(h, run_id))
        await asyncio.wait_for(entered.wait(), 3)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        release.set()
        await asyncio.wait_for(generating.wait(), 3)
        cancelled = (await h.api.post(f'/api/v1/runs/{run_id}/cancel')).json()
        assert cancelled['status'] == 'cancelled' and cancelled['resume_time'] == 1
        events = (await h.api.get(f'/api/v1/runs/{run_id}/events')).text
        assert '"resume_time": 0' not in events and '"status": "cancelled"' in events
