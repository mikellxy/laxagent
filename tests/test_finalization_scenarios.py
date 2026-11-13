import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from laxagent.run_errors import RunError, make_run_error
from laxagent.runtime.cancellation import CancelCommand
from laxagent.runtime.events import EventStoreError
from laxagent.runtime.finalization import RunFinalizer
from laxagent.runtime.runner import classify_run_error
from laxagent.storage.database import DatabaseError
from test_chat import chat_api


@pytest.mark.anyio
@pytest.mark.parametrize('stage', ['stream', 'closing', 'dispatch'])
async def test_submission_failure_confirms_concurrent_cancel(chat_api, monkeypatch, stage):
    h = chat_api
    entered, release = asyncio.Event(), asyncio.Event()
    create = h.context.events.create_run
    submitted = []
    failure = EventStoreError() if stage == 'stream' else RuntimeError('dispatch failed')

    async def blocked_create(run_id):
        submitted.append(run_id)
        entered.set()
        await release.wait()
        await create(run_id)
        if stage == 'stream':
            # A stream may exist even when its creation acknowledgement is lost.
            raise failure
        if stage == 'closing':
            h.manager.closing = True

    def broken_dispatch(run):
        raise failure

    monkeypatch.setattr(h.context.events, 'create_run', blocked_create)
    if stage == 'dispatch':
        monkeypatch.setattr(h.manager, '_dispatch', broken_dispatch)
    submitting = asyncio.create_task(h.manager.submit(
        user_id=h.conversation['user_id'], session_id=h.conversation['session_id'],
        user_query='hello', model='example:model-name',
        context_limit=100, output_limit=10,
    ))
    cancelling = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        run_id = submitted[0]
        cancelling = asyncio.create_task(h.manager.cancel(h.conversation['user_id'], run_id))
        async with asyncio.timeout(2):
            while (await h.context.repository.get_run(run_id, h.conversation['user_id'])).cancel_requested_at is None:
                await asyncio.sleep(.01)
        release.set()
        with pytest.raises(DatabaseError if stage == 'closing' else type(failure)) as caught:
            await asyncio.wait_for(submitting, 3)
        if stage != 'closing':
            assert caught.value is failure
        else:
            assert str(caught.value) == 'Agent runtime is shutting down'
        snapshot = await asyncio.wait_for(cancelling, 3)
        assert snapshot.status == 'failed' and snapshot.error_code == 'run_submission_failed'
        assert snapshot.started_at is None and snapshot.finished_at is not None
        assert not h.manager.executions

        # Retrying cleanup must not publish a second terminal event.
        await h.manager.finalizer.finish_submission_failed(run_id, 0)
        replay = (await h.api.get(f'/api/v1/runs/{run_id}/events')).text
        assert replay.count('event: error') == replay.count('event: sse_end') == 1
        assert '"status": "failed"' in replay
    finally:
        release.set()
        pending = [task for task in (submitting, cancelling) if task is not None]
        for task in pending:
            if not task.done():
                task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.anyio
@pytest.mark.parametrize('user_cancelled', [False, True])
@pytest.mark.parametrize('stage', ['startup', 'execution'])
async def test_interruption_preserves_terminal_contract(chat_api, monkeypatch, user_cancelled, stage):
    h = chat_api
    run = await h.context.repository.create_run(
        user_id=h.conversation['user_id'], session_id=h.conversation['session_id'],
        user_query='hello', model='example:model-name', context_limit=100, output_limit=10,
    )
    await h.context.events.create_run(run.id)
    entered = asyncio.Event()
    get_run = h.context.repository.get_run

    async def blocked_get(*args, **kwargs):
        entered.set()
        await asyncio.Future()

    if stage == 'startup':
        monkeypatch.setattr(h.context.repository, 'get_run', blocked_get)
    else:
        h.manager.graph = SimpleNamespace(ainvoke=blocked_get)
    method = f'finish_{stage}_interrupted'
    interrupted = AsyncMock(wraps=getattr(h.manager.finalizer, method))
    monkeypatch.setattr(h.manager.finalizer, method, interrupted)
    command = CancelCommand(run_id=run.id, resume_time=run.resume_time)
    async with h.manager.cancellation.results(run.id, run.resume_time) as sub:
        h.manager._dispatch(run)
        execution = h.manager.executions[run.id, run.resume_time]
        await asyncio.wait_for(entered.wait(), 2)
        # Persist intent without delivering it yet, exercising the interruption race.
        await h.context.repository.request_cancellation(run.user_id, run.id, run.resume_time)
        if user_cancelled:
            h.manager._accept_cancel(command)
            await asyncio.wait_for(execution.cancel_job, 3)
        else:
            execution.task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(execution.task, 3)
        result = await asyncio.wait_for(h.manager.cancellation.wait_result(sub, command), 3)
    monkeypatch.setattr(h.context.repository, 'get_run', get_run)
    current = await get_run(run.id, run.user_id)
    assert current.status == result.status == ('cancelled' if user_cancelled else 'failed')
    assert current.error_code == (None if user_cancelled else 'execution_cancelled')
    assert (current.started_at is None) == (stage == 'startup')
    assert current.finished_at is not None
    assert not h.manager.executions
    if user_cancelled:
        interrupted.assert_not_called()
    else:
        interrupted.assert_awaited_once_with(run.id, run.resume_time)
        # An already committed interruption must not emit duplicate SSE.
        await interrupted(run.id, run.resume_time)
    replay = (await h.api.get(f'/api/v1/runs/{run.id}/events')).text
    assert replay.count('event: sse_end') == 1
    assert 'event: error' not in replay
    assert '"status": "cancelled"' in replay


@pytest.mark.anyio
@pytest.mark.parametrize('failure,code', [
    (DatabaseError('private storage detail'), 'run_storage_failed'),
    (RuntimeError('private startup detail'), 'agent_execution_failed'),
])
@pytest.mark.parametrize('storage_fails', [False, True])
async def test_startup_failure_preserves_error_and_confirms_cancel(
    chat_api, monkeypatch, failure, code, storage_fails,
):
    h = chat_api
    run = await h.context.repository.create_run(
        user_id=h.conversation['user_id'], session_id=h.conversation['session_id'],
        user_query='hello', model='example:model-name', context_limit=100, output_limit=10,
    )
    await h.context.events.create_run(run.id)
    await h.context.repository.request_cancellation(run.user_id, run.id, run.resume_time)
    get_run = h.context.repository.get_run
    monkeypatch.setattr(h.context.repository, 'get_run', AsyncMock(side_effect=failure))
    if storage_fails:
        monkeypatch.setattr(h.context.repository, 'finish_run', AsyncMock(side_effect=DatabaseError()))
    command = CancelCommand(run_id=run.id, resume_time=run.resume_time)
    async with h.manager.cancellation.results(run.id, run.resume_time) as sub:
        h.manager._dispatch(run)
        execution = h.manager.executions[run.id, run.resume_time]
        with pytest.raises(type(failure)) as caught:
            await asyncio.wait_for(execution.task, 3)
        assert caught.value is failure
        if storage_fails:
            with pytest.raises(EventStoreError) as reply_error:
                await asyncio.wait_for(h.manager.cancellation.wait_result(sub, command), 3)
            assert reply_error.value.status == 503
        else:
            result = await asyncio.wait_for(h.manager.cancellation.wait_result(sub, command), 3)
            assert result.status == 'failed' and result.error_code == code
    monkeypatch.setattr(h.context.repository, 'get_run', get_run)
    assert not h.manager.executions
    current = await get_run(run.id, run.user_id)
    assert current.started_at is None
    if storage_fails:
        assert current.status == 'waiting' and current.finished_at is None
    else:
        assert current.status == 'failed' and current.error_code == code
        assert current.finished_at is not None
        await h.manager.finalizer.finish_startup_failed(run.id, run.resume_time, classify_run_error(failure))
        replay = (await h.api.get(f'/api/v1/runs/{run.id}/events')).text
        assert replay.count('event: error') == replay.count('event: sse_end') == 1
        assert '"status": "failed"' in replay
        assert 'private' not in replay


@pytest.mark.anyio
@pytest.mark.parametrize('method', [
    'finish_submission_failed', 'finish_startup_interrupted', 'finish_execution_interrupted',
    'finish_startup_failed', 'finish_execution_failed',
])
@pytest.mark.parametrize('failure,status', [(DatabaseError(), 503), (DatabaseError('stale', 409), 409),
                                          (TimeoutError(), 503), (RuntimeError('private detail'), 503)])
async def test_failure_cleanup_storage_failure_notifies_waiters(failure, status, method):
    repository = SimpleNamespace(finish_run=AsyncMock(side_effect=failure))
    events = SimpleNamespace(publish=AsyncMock())
    cancellation = SimpleNamespace(reply=AsyncMock())
    finalizer = RunFinalizer(repository, events, cancellation)

    args = (make_run_error('agent_execution_failed'),) if method in ('finish_startup_failed', 'finish_execution_failed') else ()
    assert await getattr(finalizer, method)(1, 0, *args) is None
    events.publish.assert_not_called()
    result = cancellation.reply.call_args.args[0]
    assert result.status == status and result.run_id == 1 and result.resume_time == 0


@pytest.mark.anyio
@pytest.mark.parametrize('method', [
    'finish_submission_failed', 'finish_startup_interrupted', 'finish_execution_interrupted',
    'finish_startup_failed', 'finish_execution_failed',
    'finish_user_cancelled', 'finish_succeeded',
])
async def test_failure_cleanup_survives_host_cancellation(method):
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked_finish(*args, **kwargs):
        entered.set()
        await release.wait()
        raise DatabaseError()

    cancellation = SimpleNamespace(reply=AsyncMock())
    finalizer = RunFinalizer(SimpleNamespace(finish_run=blocked_finish), None, cancellation)
    args = (make_run_error('agent_execution_failed'),) if method in ('finish_startup_failed', 'finish_execution_failed') else ()
    finishing = asyncio.create_task(getattr(finalizer, method)(1, 0, *args))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        finishing.cancel()
        await asyncio.sleep(0)
        assert not finishing.done()
    finally:
        release.set()
        if method in ('finish_user_cancelled', 'finish_succeeded'):
            with pytest.raises(DatabaseError):
                await asyncio.wait_for(finishing, 2)
        else:
            await asyncio.wait_for(finishing, 2)
    cancellation.reply.assert_awaited_once()


@pytest.mark.anyio
@pytest.mark.parametrize('method', ['finish_user_cancelled', 'finish_succeeded'])
@pytest.mark.parametrize('initial_status', ['waiting', 'running', 'success', 'failed', 'cancelled'])
async def test_terminal_commit_preserves_state_and_replies(chat_api, monkeypatch, initial_status, method):
    h = chat_api
    run = await h.context.repository.create_run(
        user_id=h.conversation['user_id'], session_id=h.conversation['session_id'],
        user_query='hello', model='example:model-name', context_limit=100, output_limit=10,
    )
    await h.context.events.create_run(run.id)
    if method == 'finish_succeeded':
        await h.context.repository.request_cancellation(run.user_id, run.id, run.resume_time)
    if initial_status == 'running':
        await h.context.repository.claim_run(run.id, run.resume_time)
    elif initial_status in ('success', 'failed', 'cancelled'):
        await h.context.repository.finish_run(
            run.id, resume_time=run.resume_time, cancelled=initial_status == 'cancelled',
            error=make_run_error('agent_execution_failed') if initial_status == 'failed' else None,
        )
    publish = AsyncMock(wraps=h.context.events.publish)
    monkeypatch.setattr(h.context.events, 'publish', publish)
    command = CancelCommand(run_id=run.id, resume_time=run.resume_time)
    # Cancellation replies even without intent; success replies to persisted intent.
    async with h.manager.cancellation.results(run.id, run.resume_time) as sub:
        first, second = await asyncio.gather(*[
            getattr(h.manager.finalizer, method)(run.id, run.resume_time) for _ in range(2)
        ])
        results = [await asyncio.wait_for(h.manager.cancellation.wait_result(sub, command), 3) for _ in range(2)]
    target_status = 'cancelled' if method == 'finish_user_cancelled' else 'success'
    expected = target_status if initial_status in ('waiting', 'running') else initial_status
    assert first.status == second.status == expected
    assert all(result.status == expected and result.finished_at is not None for result in results)
    assert results[0] == results[1]
    assert results[0].error_code == ('agent_execution_failed' if initial_status == 'failed' else None)
    if initial_status in ('waiting', 'running'):
        publish.assert_awaited_once()
        event = publish.call_args.args[0]
        assert event['type'] == 'sse_end'
        assert event['content']['status'] == ('cancelled' if method == 'finish_user_cancelled' else 'completed')
    else:
        publish.assert_not_called()


@pytest.mark.anyio
@pytest.mark.parametrize('method', ['finish_user_cancelled', 'finish_succeeded'])
@pytest.mark.parametrize('failure,status', [
    (DatabaseError(), 503), (DatabaseError('stale', 409), 409),
    (TimeoutError(), 503), (RuntimeError('private detail'), 503),
])
async def test_terminal_commit_replies_and_propagates_storage_errors(failure, status, method):
    repository = SimpleNamespace(finish_run=AsyncMock(side_effect=failure))
    events = SimpleNamespace(publish=AsyncMock())
    cancellation = SimpleNamespace(reply=AsyncMock())
    finalizer = RunFinalizer(repository, events, cancellation)
    with pytest.raises(type(failure)) as caught:
        await getattr(finalizer, method)(1, 2)
    assert caught.value is failure
    events.publish.assert_not_called()
    result = cancellation.reply.call_args.args[0]
    assert (result.run_id, result.resume_time, result.status) == (1, 2, status)
    assert result.run is None and 'private' not in result.error


@pytest.mark.anyio
@pytest.mark.parametrize('storage_fails', [False, True])
async def test_runner_success_commit_failure_uses_execution_failure_boundary(chat_api, monkeypatch, storage_fails):
    h = chat_api
    run = await h.context.repository.create_run(
        user_id=h.conversation['user_id'], session_id=h.conversation['session_id'],
        user_query='hello', model='example:model-name', context_limit=100, output_limit=10,
    )
    await h.context.events.create_run(run.id)
    h.manager.graph = SimpleNamespace(ainvoke=AsyncMock(return_value={}))
    succeeded = AsyncMock(wraps=h.manager.finalizer.finish_succeeded)
    failed = AsyncMock(wraps=h.manager.finalizer.finish_execution_failed)
    monkeypatch.setattr(h.manager.finalizer, 'finish_succeeded', succeeded)
    monkeypatch.setattr(h.manager.finalizer, 'finish_execution_failed', failed)
    finish = h.context.repository.finish_run

    async def fail_success_commit(*args, **kwargs):
        if kwargs.get('error') is None:
            raise DatabaseError()
        return await finish(*args, **kwargs)

    if storage_fails:
        monkeypatch.setattr(h.context.repository, 'finish_run', fail_success_commit)
    h.manager._dispatch(run)
    task = h.manager.executions[run.id, run.resume_time].task
    if storage_fails:
        with pytest.raises(RunError) as caught:
            await asyncio.wait_for(task, 3)
        assert caught.value.code == 'run_storage_failed'
        failed.assert_awaited_once()
    else:
        await asyncio.wait_for(task, 3)
        failed.assert_not_called()
    succeeded.assert_awaited_once_with(run.id, run.resume_time)
    current = await h.context.repository.get_run(run.id, run.user_id)
    assert current.status == ('failed' if storage_fails else 'success')
    assert current.error_code == ('run_storage_failed' if storage_fails else None)
    replay = (await h.api.get(f'/api/v1/runs/{run.id}/events')).text
    assert replay.count('event: sse_end') == 1
    assert replay.count('event: error') == int(storage_fails)
    assert f'"status": "{"failed" if storage_fails else "completed"}"' in replay
