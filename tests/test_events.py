import asyncio
import json

import nats
import pytest

from laxagent.runtime.events import EventStoreError, make_event, stream_events


@pytest.mark.anyio
async def test_independent_consumers_replay_live_events_and_cleanup(run_store):
    store, run_id = run_store
    await store.create_run(run_id)
    await store.publish(make_event(run_id, "message_start", {}, msg_seq=0))
    async with store.subscribe(run_id) as first, store.subscribe(run_id) as second:
        assert (await store.js.stream_info(store.stream_name(run_id))).state.consumer_count == 2
        assert (await first.fetch())[0].metadata.sequence.stream == 1
        assert (await second.fetch())[0].metadata.sequence.stream == 1
        pending = asyncio.create_task(first.fetch(timeout=2))
        await asyncio.sleep(0.02)
        assert not pending.done()
        await store.publish(make_event(run_id, "message_chunk", "你好", msg_seq=0))
        assert json.loads((await pending)[0].data)["content"] == "你好"
        assert json.loads((await second.fetch())[0].data)["content"] == "你好"
    assert (await store.js.stream_info(store.stream_name(run_id))).state.consumer_count == 0
    await store.publish(make_event(run_id, "sse_end", {"status": "completed"}))
    async with store.subscribe(run_id) as replay:
        output = b"".join([chunk async for chunk in stream_events(replay, run_id)])
    assert output.count(b"id: ") == 3
    assert b"id: 3\nevent: sse_end" in output
    assert (await store.js.stream_info(store.stream_name(run_id))).state.messages == 3


@pytest.mark.anyio
async def test_concurrent_duplicate_creation_and_unknown_stream(run_store):
    store, run_id = run_store
    results = await asyncio.gather(store.create_run(run_id), store.create_run(run_id), return_exceptions=True)
    assert sum(result is None for result in results) == 1
    assert next(result for result in results if isinstance(result, EventStoreError)).status == 409
    with pytest.raises(EventStoreError) as exc:
        async with store.subscribe(run_id + 1):
            pass
    assert exc.value.status == 404


@pytest.mark.anyio
async def test_cancelled_subscription_releases_consumer(run_store):
    store, run_id = run_store
    await store.create_run(run_id)
    ready = asyncio.Event()

    async def consume():
        async with store.subscribe(run_id) as sub:
            ready.set()
            await sub.fetch(timeout=30)

    task = asyncio.create_task(consume())
    await ready.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await store.js.stream_info(store.stream_name(run_id))).state.consumer_count == 0


@pytest.mark.anyio
async def test_heartbeat_and_idle_timeout():
    class SilentSubscription:
        async def fetch(self, batch, timeout):
            await asyncio.sleep(timeout)
            raise nats.errors.TimeoutError()

    output = b"".join([chunk async for chunk in stream_events(
        SilentSubscription(), 1, heartbeat=0.01, idle_timeout=0.03,
    )])
    assert b": heartbeat" in output
    assert b"Event consumption timed out" in output
    assert b"sse_end" not in output


@pytest.mark.anyio
async def test_consumer_error_is_sanitized():
    class FailedSubscription:
        async def fetch(self, **kwargs):
            raise nats.errors.ConnectionClosedError("secret-key")

    output = b"".join([chunk async for chunk in stream_events(FailedSubscription(), 1)])
    assert b"Event storage unavailable" in output
    assert b"secret-key" not in output


@pytest.mark.anyio
async def test_cancellation_consumers_broadcast_new_messages_and_cleanup(run_store):
    from laxagent.runtime.cancellation import CancellationBus, CancelCommand
    from nats.js import api
    store, run_id = run_store
    bus = CancellationBus(store)
    # Multiple API instances initialize the same stream definitions concurrently.
    await asyncio.gather(bus.initialize(), CancellationBus(store).initialize())
    command = CancelCommand(run_id=run_id, resume_time=0)
    await bus.request(command)
    subject = bus.attempt_subject(bus.COMMAND_SUBJECT, run_id, 0)
    async with bus.subscribe(bus.COMMAND_STREAM, subject, commands=True) as first, \
            bus.subscribe(bus.COMMAND_STREAM, subject, commands=True) as second:
        with pytest.raises(nats.errors.TimeoutError):
            await first.fetch(timeout=.05)
        await bus.request(command)
        for sub in (first, second):
            message = (await sub.fetch(timeout=1))[0]
            assert CancelCommand.model_validate_json(message.data) == command
            await message.ack()
    assert (await bus.js.stream_info(bus.COMMAND_STREAM)).state.consumer_count == 0
    assert (await bus.js.stream_info(bus.COMMAND_STREAM)).config.retention == api.RetentionPolicy.LIMITS
    # A result published before a new waiter is installed is deliberately not replayed.
    result_subject = bus.attempt_subject(bus.RESULT_SUBJECT, run_id, 0)
    await bus.js.publish(result_subject, b'{}', stream=bus.RESULT_STREAM)
    async with bus.results(run_id, 0) as sub:
        with pytest.raises(nats.errors.TimeoutError):
            await sub.fetch(timeout=.05)
    assert (await bus.js.stream_info(bus.RESULT_STREAM)).state.consumer_count == 0
