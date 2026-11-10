import asyncio
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import select

from laxcode.storage.database import DatabaseError
from laxcode.storage.models import Message, Run, Session, SessionMessage
from laxcode.tools.executor import run_sync_tool
from model_helpers import ModelStream, client_for, completed
from storage_helpers import message_snapshot
from test_chat import chat_api, chat_path, payload, wait_run


@pytest.mark.anyio
async def test_cancel_preserves_history_events_and_allows_next_run(chat_api):
    h = chat_api
    entered = asyncio.Event()
    stream = ModelStream([{'type': 'response.output_text.delta', 'delta': 'unfinished'}], wait=entered)
    async with client_for(stream, []) as client:
        h.context.client = client
        submitted = await h.api.post(chat_path(h), json=payload())
        run_id = submitted.json()['run_id']
        await asyncio.wait_for(entered.wait(), 2)
        responses = await asyncio.gather(*[
            h.api.post(f'/api/v1/runs/{run_id}/cancel') for _ in range(3)
        ])
        assert all(r.status_code == 200 and r.json()['status'] == 'cancelled' for r in responses)
        assert responses[0].json()['error'] is None
        assert responses[0].json()['finished_at']
        assert stream.closed and run_id not in h.manager.tasks
        replay = await h.api.get(f'/api/v1/runs/{run_id}/events')
        assert replay.text.count('event: sse_end') == 1
        assert '"status": "cancelled"' in replay.text
        _, rows = await message_snapshot(h.context.repository, h.conversation['session_id'])
        assert rows[0] == rows[1] and [r.role for r in rows[0]] == ['user']
    async with client_for(ModelStream([completed('next answer')]), []) as client:
        h.context.client = client
        next_run = (await h.api.post(chat_path(h), json=payload(user_query='continue'))).json()['run_id']
        assert (await wait_run(h, next_run))['status'] == 'success'
        assert (await h.api.post(f'/api/v1/runs/{next_run}/cancel')).json()['status'] == 'success'


@pytest.mark.anyio
async def test_cancel_before_worker_starts(chat_api):
    h = chat_api
    run = await h.manager._submit(dict(user_id=h.conversation['user_id'], session_id=h.conversation['session_id'],
        user_query='hello', model='example:model-name', context_limit=100, output_limit=10))
    await h.manager._stop(run)
    result = await h.context.repository.get_run(run.id, run.user_id)
    assert result.status == 'cancelled' and result.started_at is None
    assert not h.captured
    assert await message_snapshot(h.context.repository, run.session_id) == (1, [[], []])


@pytest.mark.anyio
async def test_soft_delete_filters_all_user_access_and_preserves_data(chat_api):
    h = chat_api
    run_id = (await h.api.post(chat_path(h), json=payload())).json()['run_id']
    await wait_run(h, run_id)
    sid = h.conversation['session_id']
    path = f'/api/v1/sessions/{sid}'
    result = await h.api.delete(path)
    assert result.status_code == 204 and result.content == b''
    assert (await h.api.delete(path)).status_code == 204
    assert (await h.api.get(f"/api/v1/projects/{h.conversation['project_id']}/sessions")).json() == []
    for url in (path, path + '/messages', f'/api/v1/runs/{run_id}', f'/api/v1/runs/{run_id}/events'):
        assert (await h.api.get(url)).status_code == 404
    assert (await h.api.post(f'/api/v1/runs/{run_id}/cancel')).status_code == 404
    assert (await h.api.patch(path, json={'name': 'no'})).status_code == 404
    assert (await h.api.post(chat_path(h), json=payload())).status_code == 404
    async with h.context.repository.db.transaction() as tx:
        assert (await tx.get(Session, sid)).deleted_at is not None
        assert (await tx.get(Run, run_id)).status == 'success'
        for model in (Message, SessionMessage):
            assert len(list(await tx.scalars(select(model).where(model.session_id == sid)))) == 2
    assert (await h.context.events.js.stream_info(h.context.events.stream_name(run_id))).state.messages > 0
    new = await h.api.post(f"/api/v1/projects/{h.conversation['project_id']}/sessions")
    assert new.json()['name'] == '会话 2'
    with pytest.raises(DatabaseError) as error:
        await h.context.repository.append_message(**h.conversation, role='user', content='no')
    assert error.value.status == 404


@pytest.mark.anyio
async def test_control_checks_auth_and_ownership(chat_api):
    h = chat_api
    run_id = (await h.api.post(chat_path(h), json=payload())).json()['run_id']
    await wait_run(h, run_id)
    token = (await h.api.post('/api/v1/login', json={'user_id': str(uuid4())})).json()['access_token']
    for method, path in [('DELETE', f"/api/v1/sessions/{h.conversation['session_id']}"),
                         ('POST', f'/api/v1/runs/{run_id}/cancel')]:
        assert (await h.api.request(method, path, headers={'Authorization': ''})).status_code == 401
        assert (await h.api.request(method, path, headers={'Authorization': f'Bearer {token}'})).status_code == 404
    assert (await h.api.delete('/api/v1/sessions/999999999')).status_code == 404
    assert (await h.api.post('/api/v1/runs/999999999/cancel')).status_code == 404


@pytest.mark.anyio
async def test_delete_waits_for_submission_and_survives_request_disconnect(chat_api, monkeypatch):
    h = chat_api
    entered, release = asyncio.Event(), asyncio.Event()
    create = h.context.events.create_run
    async def blocked_create(run_id):
        entered.set()
        await release.wait()
        await create(run_id)
    monkeypatch.setattr(h.context.events, 'create_run', blocked_create)
    submitting = asyncio.create_task(h.api.post(chat_path(h), json=payload()))
    await asyncio.wait_for(entered.wait(), 2)
    deleting = asyncio.create_task(h.api.delete(f"/api/v1/sessions/{h.conversation['session_id']}"))
    async with asyncio.timeout(2):
        while not h.manager.controls:
            await asyncio.sleep(0)
    deleting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await deleting
    release.set()
    assert (await submitting).status_code == 202
    await asyncio.wait_for(asyncio.gather(*h.manager.controls), 5)
    assert (await h.api.get(f"/api/v1/sessions/{h.conversation['session_id']}")).status_code == 404
    assert not h.manager.tasks


@pytest.mark.anyio
async def test_cancellation_drains_cleanup_once_before_delete_and_shutdown(chat_api):
    h = chat_api
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    class Graph:
        async def ainvoke(self, *args, **kwargs):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cleaning.set()
                await release.wait()
    h.manager.graph = Graph()
    run_id = (await h.api.post(chat_path(h), json=payload())).json()['run_id']
    await asyncio.wait_for(entered.wait(), 2)
    cancel = asyncio.create_task(h.api.post(f'/api/v1/runs/{run_id}/cancel'))
    await asyncio.wait_for(cleaning.wait(), 2)
    deleting = asyncio.create_task(h.api.delete(f"/api/v1/sessions/{h.conversation['session_id']}"))
    async with asyncio.timeout(2):
        while len(h.manager.controls) < 2:
            await asyncio.sleep(0)
    closing = asyncio.create_task(h.manager.close())
    await asyncio.sleep(0)
    assert not cancel.done() and not deleting.done() and not closing.done()
    assert h.manager.tasks[run_id].cancelling() == 1
    release.set()
    assert (await cancel).json()['status'] == 'cancelled'
    assert (await deleting).status_code == 204
    await closing
    assert not h.manager.tasks


@pytest.mark.anyio
@pytest.mark.parametrize('outcome', ['success', 'failed'])
async def test_natural_completion_wins_cancel_without_losing_end_event(chat_api, monkeypatch, outcome):
    h = chat_api
    entered, release = asyncio.Event(), asyncio.Event()
    finish = h.context.repository.finish_run
    async def blocked_finish(run_id, **kwargs):
        if 'cancelled' not in kwargs:
            entered.set()
            await release.wait()
        return await finish(run_id, **kwargs)
    monkeypatch.setattr(h.context.repository, 'finish_run', blocked_finish)
    if outcome == 'failed':
        class BrokenGraph:
            async def ainvoke(self, *args, **kwargs):
                raise RuntimeError('test failure')
        h.manager.graph = BrokenGraph()
    run_id = (await h.api.post(chat_path(h), json=payload())).json()['run_id']
    await asyncio.wait_for(entered.wait(), 2)
    cancel = asyncio.create_task(h.api.post(f'/api/v1/runs/{run_id}/cancel'))
    async with asyncio.timeout(2):
        while not h.manager.tasks[run_id].cancelling():
            await asyncio.sleep(0)
    release.set()
    assert (await cancel).json()['status'] == outcome
    replay = await h.api.get(f'/api/v1/runs/{run_id}/events')
    expected = 'completed' if outcome == 'success' else 'failed'
    assert replay.text.count('event: sse_end') == 1 and f'"status": "{expected}"' in replay.text


@pytest.mark.anyio
async def test_delete_does_not_hide_session_on_terminal_commit_failure(chat_api, monkeypatch):
    h = chat_api
    entered = asyncio.Event()
    async with client_for(ModelStream([], wait=entered), []) as client:
        h.context.client = client
        run_id = (await h.api.post(chat_path(h), json=payload())).json()['run_id']
        await asyncio.wait_for(entered.wait(), 2)
        finish = h.context.repository.finish_run
        async def unavailable(*args, **kwargs):
            raise DatabaseError()
        monkeypatch.setattr(h.context.repository, 'finish_run', unavailable)
        path = f"/api/v1/sessions/{h.conversation['session_id']}"
        assert (await h.api.delete(path)).status_code == 503
        assert (await h.api.get(path)).status_code == 200
        monkeypatch.setattr(h.context.repository, 'finish_run', finish)
        assert (await h.api.delete(path)).status_code == 204
        async with h.context.repository.db.transaction() as tx:
            assert (await tx.get(Run, run_id)).status == 'cancelled'


@pytest.mark.anyio
async def test_sync_tool_cancellation_waits_for_thread_even_on_repeated_cancel():
    started, release, finished = Event(), Event(), Event()
    def work(*args):
        started.set()
        assert release.wait(5)
        finished.set()
        return 'done'
    task = asyncio.create_task(run_sync_tool(work, None, None))
    try:
        async with asyncio.timeout(2):
            while not started.is_set():
                await asyncio.sleep(.01)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


@pytest.mark.anyio
async def test_guest_bootstrap_does_not_reuse_deleted_session(chat_api):
    h = chat_api
    body = {'user_id': h.conversation['user_id'], 'from_page': 'guest_login'}
    first = (await h.api.post('/api/v1/login', json=body)).json()['initial_session_id']
    assert (await h.api.delete(f'/api/v1/sessions/{first}')).status_code == 204
    second = (await h.api.post('/api/v1/login', json=body)).json()['initial_session_id']
    assert second != first


@pytest.mark.anyio
async def test_cancel_in_tool_turn_keeps_completed_pairs_for_next_chat(chat_api, monkeypatch):
    from laxcode.tools.registry import TOOLS, Tool
    from test_tool_loop import tool_response
    h = chat_api
    entered = asyncio.Event()
    calls = 0
    async def read(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            return 'first file'
        entered.set()
        await asyncio.Future()
    monkeypatch.setitem(TOOLS, 'read_file', Tool(TOOLS['read_file'].schema, read))
    captured = []
    async with client_for([ModelStream([tool_response()]), ModelStream([completed('continued')])], captured) as client:
        h.context.client = client
        run_id = (await h.api.post(chat_path(h), json=payload())).json()['run_id']
        await asyncio.wait_for(entered.wait(), 2)
        assert (await h.api.post(f'/api/v1/runs/{run_id}/cancel')).json()['status'] == 'cancelled'
        _, rows = await message_snapshot(h.context.repository, h.conversation['session_id'])
        assert [row.role for row in rows[0]] == ['user', 'assistant', 'tool']
        assert len(rows[0][1].tool_call) == 2
        next_run = (await h.api.post(chat_path(h), json=payload(user_query='continue'))).json()['run_id']
        assert (await wait_run(h, next_run))['status'] == 'success'
        assert [item['call_id'] for item in captured[1]['input'] if item.get('type') == 'function_call'] == ['call_0']
        assert [item['call_id'] for item in captured[1]['input'] if item.get('type') == 'function_call_output'] == ['call_0']


@pytest.mark.anyio
async def test_cancel_status_survives_event_publish_failure(chat_api, monkeypatch):
    from laxcode.runtime.events import EventStoreError
    h = chat_api
    entered = asyncio.Event()
    async with client_for(ModelStream([], wait=entered), []) as client:
        h.context.client = client
        run_id = (await h.api.post(chat_path(h), json=payload())).json()['run_id']
        await asyncio.wait_for(entered.wait(), 2)
        async def unavailable(event):
            raise EventStoreError()
        monkeypatch.setattr(h.context.events, 'publish', unavailable)
        assert (await h.api.post(f'/api/v1/runs/{run_id}/cancel')).json()['status'] == 'cancelled'
        assert (await h.api.get(f'/api/v1/runs/{run_id}')).json()['status'] == 'cancelled'
