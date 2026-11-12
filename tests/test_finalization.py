import asyncio

import pytest

from laxcode.run_errors import make_run_error
from laxcode.runtime.cancellation import CancelCommand, RunSnapshot
from laxcode.runtime.finalization import RunFinalizer
from laxcode.runtime.events import EventStoreError
from laxcode.storage.database import DatabaseError
from model_helpers import ModelStream, client_for
from test_chat import chat_api, chat_path, payload


async def unstarted_run(h):
    run = await h.context.repository.create_run(
        user_id=h.conversation['user_id'], session_id=h.conversation['session_id'],
        user_query='hello', model='example:model-name', context_limit=100, output_limit=10,
    )
    await h.context.events.create_run(run.id)
    await h.context.repository.request_cancellation(run.user_id, run.id, run.resume_time)
    return run


@pytest.mark.anyio
@pytest.mark.parametrize('outcome,method_name', [
    ('success', 'finish_succeeded'),
    ('failed', 'finish_submission_failed'),
    ('failed', 'finish_startup_failed'),
    ('failed', 'finish_execution_failed'),
    ('incomplete', 'finish_startup_failed'),
    ('incomplete', 'finish_execution_failed'),
    ('cancelled', 'finish_user_cancelled'),
    ('shutdown', 'finish_startup_interrupted'),
    ('shutdown', 'finish_execution_interrupted'),
])
async def test_finalization_without_local_worker_is_idempotent_and_replies(chat_api, outcome, method_name):
    h = chat_api
    run = await unstarted_run(h)
    finalizer = RunFinalizer(h.context.repository, h.context.events)
    method = getattr(finalizer, method_name)
    options = {}
    if method_name in ('finish_startup_failed', 'finish_execution_failed'):
        options['error'] = make_run_error(
            'model_incomplete_max_output_tokens' if outcome == 'incomplete' else 'agent_execution_failed',
        )
    expected = 'failed' if outcome in ('shutdown', 'incomplete') else outcome
    async with h.manager.cancellation.results(run.id, 0) as sub:
        first, second = await asyncio.gather(*[method(run.id, 0, **options) for _ in range(2)])
        async with asyncio.timeout(2):
            result = await h.manager.cancellation.wait_result(sub, CancelCommand(run_id=run.id, resume_time=0))
    assert RunSnapshot.model_validate(first) == RunSnapshot.model_validate(second) == result
    assert result.status == expected
    assert not h.manager.executions
    replay = (await h.api.get(f'/api/v1/runs/{run.id}/events')).text
    assert replay.count('event: sse_end') == 1
    assert replay.count('event: error') == (outcome == 'failed')
    assert replay.count('event: response_incomplete') == (outcome == 'incomplete')
    event_status = 'completed' if outcome == 'success' else 'cancelled' if outcome == 'shutdown' else outcome
    assert f'"status": "{event_status}"' in replay


@pytest.mark.anyio
async def test_natural_failure_reply_keeps_snapshot_when_resumed_during_sse(chat_api, monkeypatch):
    h = chat_api
    run = await unstarted_run(h)
    publishing, release = asyncio.Event(), asyncio.Event()
    publish = h.context.events.publish

    async def blocked(event):
        if event['type'] == 'error':
            publishing.set()
            await release.wait()
        await publish(event)

    monkeypatch.setattr(h.context.events, 'publish', blocked)
    finalizer = RunFinalizer(h.context.repository, h.context.events)
    async with h.manager.cancellation.results(run.id, 0) as sub:
        finishing = asyncio.create_task(finalizer.finish_execution_failed(run.id, 0, make_run_error('agent_execution_failed')))
        try:
            await asyncio.wait_for(publishing.wait(), 2)
            resumed = await h.context.repository.resume_run(run.user_id, run.id, 0)
            assert resumed.resume_time == 1 and resumed.cancel_requested_at is None
        finally:
            release.set()
        await finishing
        async with asyncio.timeout(2):
            result = await h.manager.cancellation.wait_result(sub, CancelCommand(run_id=run.id, resume_time=0))
    assert result.resume_time == 0 and result.status == 'failed'
    with pytest.raises(DatabaseError) as stale:
        await finalizer.finish_user_cancelled(run.id, 0)
    assert stale.value.status == 409
    current = await h.context.repository.get_run(run.id, run.user_id)
    assert current.resume_time == 1 and current.status == 'waiting'


@pytest.mark.anyio
@pytest.mark.parametrize('phase', ['database', 'sse'])
async def test_finalization_timeout_still_replies_to_cancel_waiters(chat_api, monkeypatch, phase):
    h = chat_api
    run = await unstarted_run(h)

    async def stuck(*args, **kwargs):
        await asyncio.Future()

    if phase == 'database':
        monkeypatch.setattr(h.context.repository, 'finish_run', stuck)
    else:
        monkeypatch.setattr(h.context.events, 'publish', stuck)
    finalizer = RunFinalizer(h.context.repository, h.context.events)
    async with h.manager.cancellation.results(run.id, 0) as sub:
        async with asyncio.timeout(8):
            current = await finalizer.finish_execution_failed(run.id, 0, make_run_error('agent_execution_failed'))
            if phase == 'database':
                assert current is None
                with pytest.raises(EventStoreError) as error:
                    await h.manager.cancellation.wait_result(sub, CancelCommand(run_id=run.id, resume_time=0))
                assert error.value.status == 503
            else:
                result = await h.manager.cancellation.wait_result(sub, CancelCommand(run_id=run.id, resume_time=0))
                assert result == RunSnapshot.model_validate(current) and result.status == 'failed'


@pytest.mark.anyio
@pytest.mark.parametrize('storage_fails', [False, True])
async def test_execution_failure_has_one_error_boundary(chat_api, monkeypatch, storage_fails):
    h = chat_api
    attempts = []
    finish = h.context.repository.finish_run

    async def tracked(*args, **kwargs):
        attempts.append(kwargs['error'].code)
        if storage_fails:
            raise DatabaseError()
        return await finish(*args, **kwargs)

    class BrokenGraph:
        async def ainvoke(self, *args, **kwargs):
            raise RuntimeError('execution failed')

    monkeypatch.setattr(h.context.repository, 'finish_run', tracked)
    h.manager.graph = BrokenGraph()
    run_id = (await h.api.post(chat_path(h), json=payload())).json()['run_id']
    async with asyncio.timeout(3):
        while h.manager.executions:
            await asyncio.sleep(.01)
    assert attempts == ['agent_execution_failed']
    status = (await h.api.get(f'/api/v1/runs/{run_id}')).json()['status']
    assert status == ('running' if storage_fails else 'failed')


@pytest.mark.anyio
async def test_old_execution_cleanup_keeps_resumed_worker_registered(chat_api, monkeypatch):
    h = chat_api
    publishing, release, running = (asyncio.Event() for _ in range(3))
    publish = h.context.events.publish

    async def blocked(event):
        if event['type'] == 'error' and event['resume_time'] == 0:
            publishing.set()
            await release.wait()
        await publish(event)

    monkeypatch.setattr(h.context.events, 'publish', blocked)
    async with client_for([ModelStream([]), ModelStream([], wait=running)], []) as client:
        h.context.client = client
        run_id = (await h.api.post(chat_path(h), json=payload())).json()['run_id']
        await asyncio.wait_for(publishing.wait(), 2)
        old = h.manager.executions[run_id, 0]
        try:
            response = await h.api.post(f'/api/v1/runs/{run_id}/resume', json={'resume_time': 0})
            assert response.status_code == 202
            await asyncio.wait_for(running.wait(), 2)
            new = h.manager.executions[run_id, 1]
            assert old is not new and not old.task.done() and not new.task.done()
        finally:
            release.set()
        await asyncio.gather(old.task, return_exceptions=True)
        assert (run_id, 0) not in h.manager.executions
        assert h.manager.executions[run_id, 1] is new and not new.task.done()
        assert (await h.api.post(f'/api/v1/runs/{run_id}/cancel')).json()['status'] == 'cancelled'
        assert not h.manager.executions
