import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from laxcode.runtime.events import EventStoreError
from laxcode.runtime.finalization_v2 import RunFinalizerV2
from laxcode.storage.database import DatabaseError
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
    # All three paths must have migrated off the old finalizer.
    legacy = AsyncMock(side_effect=AssertionError('legacy finalizer called'))
    monkeypatch.setattr(h.manager.finalizer, 'fail', legacy)
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
        legacy.assert_not_called()

        # Retrying cleanup must not publish a second terminal event.
        await h.manager.finalizer_v2.finish_dispatch_failed(run_id, 0)
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
@pytest.mark.parametrize('failure,status', [(DatabaseError(), 503), (DatabaseError('stale', 409), 409),
                                          (TimeoutError(), 503)])
async def test_submission_cleanup_storage_failure_notifies_waiters(failure, status):
    repository = SimpleNamespace(finish_run=AsyncMock(side_effect=failure))
    events = SimpleNamespace(publish=AsyncMock())
    cancellation = SimpleNamespace(reply=AsyncMock())
    finalizer = RunFinalizerV2(repository, events, cancellation)

    assert await finalizer.finish_stream_creation_failed(1, 0) is None
    events.publish.assert_not_called()
    result = cancellation.reply.call_args.args[0]
    assert result.status == status and result.run_id == 1 and result.resume_time == 0


@pytest.mark.anyio
async def test_submission_cleanup_survives_host_cancellation():
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked_finish(*args, **kwargs):
        entered.set()
        await release.wait()
        raise DatabaseError()

    cancellation = SimpleNamespace(reply=AsyncMock())
    finalizer = RunFinalizerV2(SimpleNamespace(finish_run=blocked_finish), None, cancellation)
    finishing = asyncio.create_task(finalizer.finish_submission_closed(1, 0))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        finishing.cancel()
        await asyncio.sleep(0)
        assert not finishing.done()
    finally:
        release.set()
        await asyncio.wait_for(finishing, 2)
    cancellation.reply.assert_awaited_once()
