import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Literal, TypedDict
from uuid import uuid4

import anyio
import nats
from nats.js import api
from nats.js.errors import NotFoundError


logger = logging.getLogger(__name__)

# These previews are replaced by complete messages/history, never used to execute tools.
LOSSY_EVENT_TYPES = frozenset({
    "message_start", "message_chunk", "reasoning_chunk", "refusal_chunk",
    "tool_call_start", "tool_call_delta",
})


class Event(TypedDict):
    run_id: int
    resume_time: int
    msg_seq: int | None
    role: Literal["assistant", "system", "tool"]
    type: str
    error: str | None
    content: str | dict


def make_event(run_id: int, kind: str, content: str | dict, *,
               msg_seq: int | None = None, error: str | None = None, resume_time: int = 0,
               role: Literal["assistant", "system", "tool"] | None = None, **extra) -> Event:
    return {"run_id": run_id, "resume_time": resume_time, "msg_seq": msg_seq,
            "role": role or ("assistant" if msg_seq is not None else "system"),
            "type": kind, "error": error, "content": content, **extra}


class EventStoreError(Exception):
    def __init__(self, message="Event storage unavailable", status=503):
        super().__init__(message)
        self.status = status


async def log_nats_error(exc):
    # NATS exceptions can include connection URLs; never log their payload.
    logging.getLogger(__name__).warning("NATS connection error (%s)", type(exc).__name__)


class EventStore:
    def __init__(self, connection):
        self.connection = connection
        self.js = connection.jetstream(timeout=5)
        self._create_lock = asyncio.Lock()

    @classmethod
    @asynccontextmanager
    async def connect(cls, url: str):
        connection = nats.NATS()
        try:
            await connection.connect(
                servers=[url], connect_timeout=2, max_reconnect_attempts=2,
                error_cb=log_nats_error,
            )
            store = cls(connection)
            await store.js.account_info()
            yield store
        except (nats.errors.Error, OSError):
            raise EventStoreError() from None
        finally:
            await connection.close()

    @staticmethod
    def stream_name(run_id: int) -> str:
        return f"LAXAGENT_RUN_{run_id}"

    @staticmethod
    def subject(run_id: int) -> str:
        return f"laxagent.runs.{run_id}.events"

    async def create_run(self, run_id: int, *, exist_ok: bool = False):
        # Serialize local duplicate creation. Identical add_stream calls are idempotent
        # across processes (concurrent resume prepares the same existing stream).
        async with self._create_lock:
            try:
                try:
                    await self.js.stream_info(self.stream_name(run_id))
                except NotFoundError:
                    await self.js.add_stream(
                        name=self.stream_name(run_id), subjects=[self.subject(run_id)],
                        storage=api.StorageType.FILE, retention=api.RetentionPolicy.LIMITS,
                        num_replicas=1,
                    )
                else:
                    if not exist_ok:
                        raise EventStoreError("Run already exists", 409)
            except nats.errors.Error:
                raise EventStoreError() from None

    async def publish(self, event: Event):
        payload = json.dumps(event, ensure_ascii=False).encode()
        # A lost acknowledgement must not duplicate an already stored text delta.
        headers = {"Nats-Msg-Id": uuid4().hex}
        for attempt in range(3):
            try:
                await self.js.publish(
                    self.subject(event["run_id"]), payload,
                    stream=self.stream_name(event["run_id"]), headers=headers,
                )
                return
            except nats.errors.Error:
                if attempt < 2:
                    await asyncio.sleep(0.1 * 2**attempt)
                    continue
                if event["type"] in LOSSY_EVENT_TYPES:
                    logger.warning("Dropped preview event %s for run %s after 3 publish attempts",
                                   event["type"], event["run_id"])
                    return
                raise EventStoreError() from None

    @asynccontextmanager
    async def subscribe(self, run_id: int):
        name = uuid4().hex
        stream = self.stream_name(run_id)
        sub = None
        created = False
        try:
            try:
                await self.js.stream_info(stream)
            except NotFoundError:
                raise EventStoreError("Run stream not found", 404) from None
            await self.js.add_consumer(stream, config=api.ConsumerConfig(
                name=name, deliver_policy=api.DeliverPolicy.ALL,
                ack_policy=api.AckPolicy.NONE, filter_subject=self.subject(run_id),
                inactive_threshold=60,
            ))
            created = True
            sub = await self.js.pull_subscribe_bind(stream=stream, name=name)
            yield sub
        except nats.errors.Error:
            raise EventStoreError() from None
        finally:
            with anyio.move_on_after(5, shield=True):
                if sub is not None:
                    with suppress(nats.errors.Error):
                        await sub.unsubscribe()
                if created:
                    with suppress(nats.errors.Error):
                        await self.js.delete_consumer(stream, name)


def encode_sse(event: Event, sequence: int | None = None) -> bytes:
    prefix = f"id: {sequence}\n" if sequence is not None else ""
    return (f"{prefix}event: {event['type']}\n"
            f"data: {json.dumps(event, ensure_ascii=False)}\n\n").encode()


async def stream_events(sub, run_id: int, *, resume_time=0, last_seq: int | None = None,
                        heartbeat=15, idle_timeout=330) -> AsyncIterator[bytes]:
    loop = asyncio.get_running_loop()
    last_event = loop.time()
    batch = 1
    while True:
        remaining = idle_timeout - (loop.time() - last_event)
        if remaining <= 0:
            yield encode_sse(make_event(run_id, "error", {}, error="Event consumption timed out", resume_time=resume_time))
            return
        try:
            messages = await sub.fetch(batch=batch, timeout=min(heartbeat, remaining))
        except TimeoutError:
            batch = 1
            yield b": heartbeat\n\n"
            continue
        except nats.errors.Error:
            yield encode_sse(make_event(run_id, "error", {}, error="Event storage unavailable", resume_time=resume_time))
            return
        for message in messages:
            try:
                # Batch only the known backlog; waiting for a large live batch delays tokens.
                batch = min(128, max(1, message.metadata.num_pending))
                event = json.loads(message.data)
                # Filter before forwarding terminal events or unfinished chunks.
                if event.get("resume_time", 0) != resume_time:
                    continue
                last_event = loop.time()
                # History already supplies these message bodies. Null-seq control events
                # (questions, errors and sse_end) must still reach the client.
                if last_seq is not None and event.get("msg_seq") is not None and event["msg_seq"] <= last_seq:
                    continue
                payload = encode_sse(event, message.metadata.sequence.stream)
            except (ValueError, KeyError, TypeError):
                yield encode_sse(make_event(run_id, "error", {}, error="Invalid stored event", resume_time=resume_time))
                return
            yield payload
            if event["type"] == "sse_end":
                return
