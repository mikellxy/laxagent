import asyncio
from types import SimpleNamespace

import nats
import pytest

from laxagent.runtime.cancellation import CancellationBus, CancelCommand


@pytest.mark.anyio
@pytest.mark.parametrize('timeout_type', [TimeoutError, nats.errors.TimeoutError])
async def test_idle_timeout_does_not_stop_listener_or_log_error(timeout_type, caplog):
    command = CancelCommand(run_id=1, resume_time=0)
    handled, acked = [], asyncio.Event()
    calls = 0

    async def ack():
        acked.set()

    async def fetch(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise timeout_type()
        if calls == 2:
            return [SimpleNamespace(data=command.model_dump_json().encode(), ack=ack)]
        await asyncio.Future()

    task = asyncio.create_task(CancellationBus(None)._consume(SimpleNamespace(fetch=fetch), handled.append))
    try:
        await asyncio.wait_for(acked.wait(), 2)
        assert handled == [command] and not task.done()
        assert not caplog.records
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.anyio
@pytest.mark.parametrize('stage', ['fetch', 'handler', 'ack'])
async def test_unknown_error_logs_safely_and_keeps_consuming(stage, caplog):
    first, second = (CancelCommand(run_id=i, resume_time=0) for i in (1, 2))
    handled, acked = [], []
    finished = asyncio.Event()
    calls = 0

    def fail():
        raise RuntimeError('credential-secret')

    async def ack_first():
        if stage == 'ack':
            fail()
        acked.append(first)

    async def ack_second():
        acked.append(second)
        finished.set()

    async def fetch(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            if stage == 'fetch':
                fail()
            return [SimpleNamespace(data=first.model_dump_json().encode(), ack=ack_first)]
        if calls == 2:
            return [SimpleNamespace(data=second.model_dump_json().encode(), ack=ack_second)]
        await asyncio.Future()

    def handler(command):
        if stage == 'handler' and command == first:
            fail()
        handled.append(command)

    task = asyncio.create_task(CancellationBus(None)._consume(SimpleNamespace(fetch=fetch), handler))
    try:
        await asyncio.wait_for(finished.wait(), 3)
        assert second in handled and acked == [second]
        assert not task.done()
        assert 'RuntimeError' in caplog.text and ' in fail' in caplog.text
        assert 'credential-secret' not in caplog.text
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.anyio
async def test_shutdown_interrupts_error_backoff(caplog):
    failed = asyncio.Event()
    calls = 0

    async def fetch(**kwargs):
        nonlocal calls
        calls += 1
        failed.set()
        raise RuntimeError('unavailable')

    task = asyncio.create_task(CancellationBus(None)._consume(SimpleNamespace(fetch=fetch), lambda _: None))
    await failed.wait()
    assert not task.done() and calls == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, .5)
    assert len(caplog.records) == 1
