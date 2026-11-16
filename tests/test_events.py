import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import nats
import pytest

from laxagent.runtime.events import EventStore, EventStoreError, make_event, stream_events


@pytest.mark.anyio
@pytest.mark.parametrize("kind", [
    "message_start", "message_chunk", "reasoning_chunk", "refusal_chunk",
    "tool_call_start", "tool_call_delta",
])
async def test_preview_publish_failure_is_dropped_after_three_attempts(kind, monkeypatch, caplog):
    publish = AsyncMock(side_effect=nats.errors.TimeoutError("secret-key"))
    store = EventStore(SimpleNamespace(jetstream=lambda **kwargs: SimpleNamespace(publish=publish)))
    monkeypatch.setattr("laxagent.runtime.events.asyncio.sleep", AsyncMock())
    await store.publish(make_event(1, kind, "private-content", msg_seq=2))
    assert publish.await_count == 3
    assert "Dropped preview event" in caplog.text
    assert "secret-key" not in caplog.text and "private-content" not in caplog.text


@pytest.mark.anyio
@pytest.mark.parametrize("kind", [
    "message_end", "tool_start", "tool_end", "ask_user_question", "sse_end", "error", "new_event",
])
async def test_non_preview_publish_failure_still_propagates(kind, monkeypatch):
    publish = AsyncMock(side_effect=nats.errors.TimeoutError("secret-key"))
    store = EventStore(SimpleNamespace(jetstream=lambda **kwargs: SimpleNamespace(publish=publish)))
    monkeypatch.setattr("laxagent.runtime.events.asyncio.sleep", AsyncMock())
    with pytest.raises(EventStoreError, match="^Event storage unavailable$"):
        await store.publish(make_event(1, kind, {}))
    assert publish.await_count == 3


@pytest.mark.anyio
async def test_publish_retry_reuses_payload_and_id_but_next_event_has_new_id(monkeypatch):
    publish = AsyncMock(side_effect=[nats.errors.TimeoutError(), nats.errors.TimeoutError(), None, None])
    store = EventStore(SimpleNamespace(jetstream=lambda **kwargs: SimpleNamespace(publish=publish)))
    monkeypatch.setattr("laxagent.runtime.events.asyncio.sleep", AsyncMock())
    event = make_event(1, "message_chunk", "hello", msg_seq=2)
    await store.publish(event)
    await store.publish(event)
    first, second, third, following = publish.await_args_list
    assert first == second == third
    assert first.kwargs["headers"]["Nats-Msg-Id"] != following.kwargs["headers"]["Nats-Msg-Id"]


@pytest.mark.anyio
async def test_publish_retry_remains_cancellable(monkeypatch):
    publish = AsyncMock(side_effect=nats.errors.TimeoutError())
    store = EventStore(SimpleNamespace(jetstream=lambda **kwargs: SimpleNamespace(publish=publish)))
    monkeypatch.setattr("laxagent.runtime.events.asyncio.sleep", AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await store.publish(make_event(1, "message_chunk", "hello", msg_seq=2))
    assert publish.await_count == 1


@pytest.mark.anyio
async def test_lost_publish_ack_does_not_duplicate_stored_preview(run_store, monkeypatch):
    store, run_id = run_store
    await store.create_run(run_id)
    publish = store.js.publish
    attempts = 0

    async def lose_first_ack(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        result = await publish(*args, **kwargs)
        if attempts == 1:
            raise nats.errors.TimeoutError()
        return result

    monkeypatch.setattr(store.js, "publish", lose_first_ack)
    await store.publish(make_event(run_id, "message_chunk", "hello", msg_seq=2))
    assert attempts == 2
    assert (await store.js.stream_info(store.stream_name(run_id))).state.messages == 1


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
@pytest.mark.parametrize("timeout_error", [TimeoutError, nats.errors.TimeoutError])
async def test_heartbeat_and_idle_timeout(timeout_error):
    class SilentSubscription:
        async def fetch(self, batch, timeout):
            await asyncio.sleep(timeout)
            raise timeout_error()

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


@pytest.mark.anyio
async def test_filtered_replay_preserves_uncommitted_messages_and_control_events(run_store):
    store, run_id = run_store
    await store.create_run(run_id)
    stored = [make_event(run_id, 'sse_end', {}, resume_time=0)]
    stored += [make_event(run_id, 'reasoning_chunk', 'committed', msg_seq=2, resume_time=1) for _ in range(260)]
    remaining = [
        make_event(run_id, 'tool_start', {'tool_name': 'ask_user_question'}, msg_seq=3, resume_time=1),
        make_event(run_id, 'message_chunk', 'unfinished', msg_seq=4, resume_time=1),
        make_event(run_id, 'error', {}, error='control error', resume_time=1),
        make_event(run_id, 'ask_user_question', {'questions': []}, resume_time=1),
        make_event(run_id, 'sse_end', {'status': 'paused'}, resume_time=1),
    ]
    for event in [*stored, *remaining]:
        await store.publish(event)
    batches = []
    async with store.subscribe(run_id) as sub:
        class RecordingSubscription:
            async def fetch(self, batch, timeout):
                batches.append(batch)
                return await sub.fetch(batch=batch, timeout=timeout)

        output = b''.join([chunk async for chunk in stream_events(
            RecordingSubscription(), run_id, resume_time=1, last_seq=2,
        )])
    received = [json.loads(line[6:]) for line in output.splitlines() if line.startswith(b'data: ')]
    assert received == remaining
    assert b'id: 262\nevent: tool_start' in output
    assert batches[0] == 1 and max(batches) == 128 and len(batches) < 10


@pytest.mark.anyio
async def test_batch_replay_returns_to_immediate_live_delivery(run_store):
    store, run_id = run_store
    await store.create_run(run_id)
    for _ in range(4):
        await store.publish(make_event(run_id, 'message_chunk', 'backlog', msg_seq=1))
    async with store.subscribe(run_id) as sub:
        stream = stream_events(sub, run_id, heartbeat=2)
        try:
            for _ in range(4):
                assert b'backlog' in await anext(stream)
            pending = asyncio.create_task(anext(stream))
            await asyncio.sleep(.05)
            assert not pending.done()
            await store.publish(make_event(run_id, 'message_chunk', 'live-token', msg_seq=1))
            assert b'live-token' in await asyncio.wait_for(pending, .5)
            await store.publish(make_event(run_id, 'sse_end', {}))
            assert b'sse_end' in await asyncio.wait_for(anext(stream), .5)
        finally:
            await stream.aclose()
