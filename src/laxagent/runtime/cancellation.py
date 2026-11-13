"""JetStream control messages; execution state remains in PostgreSQL."""
import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from typing import Literal
from uuid import uuid4

import anyio
import nats
from nats.js import api
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from laxagent.runtime.events import EventStoreError

logger = logging.getLogger(__name__)


class CancelCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    run_id: int = Field(gt=0)
    resume_time: int = Field(ge=0)


class RunSnapshot(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    session_id: int
    status: Literal["success", "failed", "cancelled"]
    model: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error: str | None
    error_code: str | None
    resume_time: int


class CancelResult(BaseModel):
    run_id: int
    resume_time: int
    run: RunSnapshot | None = None
    error: str | None = None
    status: Literal[200, 409, 503] = 200


class CancellationBus:
    COMMAND_STREAM = "to_cancel"
    RESULT_STREAM = "cancel_result"
    COMMAND_SUBJECT = "laxcode.cancel.commands"
    RESULT_SUBJECT = "laxcode.cancel.results"

    def __init__(self, events):
        self.events = events

    @property
    def js(self):
        return self.events.js

    async def initialize(self):
        try:
            # Identical add_stream calls are idempotent across starting API instances.
            for stream, subject in ((self.COMMAND_STREAM, self.COMMAND_SUBJECT),
                                    (self.RESULT_STREAM, self.RESULT_SUBJECT)):
                await self.js.add_stream(
                    name=stream, subjects=[subject + ".>"], storage=api.StorageType.FILE,
                    retention=api.RetentionPolicy.LIMITS, max_age=3600, max_bytes=8 * 1024 * 1024,
                )
        except nats.errors.Error:
            raise EventStoreError("取消通信初始化失败") from None

    @asynccontextmanager
    async def subscribe(self, stream, subject, *, commands=False):
        name, sub, created = uuid4().hex, None, False
        try:
            # NEW starts when the server creates this consumer, before publishing a command.
            await self.js.add_consumer(stream, config=api.ConsumerConfig(
                name=name, deliver_policy=api.DeliverPolicy.NEW, filter_subject=subject,
                ack_policy=api.AckPolicy.EXPLICIT if commands else api.AckPolicy.NONE,
                ack_wait=30, inactive_threshold=60,
            ))
            created = True
            sub = await self.js.pull_subscribe_bind(stream=stream, name=name)
            yield sub
        except nats.errors.Error:
            raise EventStoreError("取消通信暂不可用，请重试") from None
        finally:
            with anyio.move_on_after(5, shield=True):
                if sub is not None:
                    with suppress(nats.errors.Error):
                        await sub.unsubscribe()
                if created:
                    with suppress(nats.errors.Error):
                        await self.js.delete_consumer(stream, name)

    @staticmethod
    def attempt_subject(prefix, run_id, resume_time):
        return f"{prefix}.{run_id}.{resume_time}"

    def results(self, run_id, resume_time):
        return self.subscribe(self.RESULT_STREAM, self.attempt_subject(self.RESULT_SUBJECT, run_id, resume_time))

    async def request(self, command: CancelCommand):
        await self._publish(self.COMMAND_STREAM, self.COMMAND_SUBJECT, command)

    async def reply(self, result: CancelResult):
        await self._publish(self.RESULT_STREAM, self.RESULT_SUBJECT, result)

    async def _publish(self, stream, prefix, message):
        try:
            await self.js.publish(
                self.attempt_subject(prefix, message.run_id, message.resume_time),
                message.model_dump_json().encode(), stream=stream,
            )
        except nats.errors.Error:
            raise EventStoreError("取消通信暂不可用，请重试") from None

    async def wait_result(self, sub, command):
        while True:
            try:
                messages = await sub.fetch(batch=1, timeout=1)
            except nats.errors.TimeoutError:
                continue
            except nats.errors.Error:
                raise EventStoreError("取消结果接收失败，请重试") from None
            for message in messages:
                try:
                    result = CancelResult.model_validate_json(message.data)
                except ValidationError:
                    logger.warning("Invalid cancellation result")
                    continue
                if (result.run_id, result.resume_time) != (command.run_id, command.resume_time):
                    continue
                if result.error and result.status != 200:
                    raise EventStoreError(result.error, result.status)
                if (result.status == 200 and result.run is not None
                        and (result.run.id, result.run.resume_time) == (command.run_id, command.resume_time)):
                    return result.run

    @asynccontextmanager
    async def listen(self, handler):
        await self.initialize()
        async with self.subscribe(self.COMMAND_STREAM, self.COMMAND_SUBJECT + ".>", commands=True) as sub:
            task = asyncio.create_task(self._consume(sub, handler), name="cancel-listener")
            try:
                yield
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def _consume(self, sub, handler):
        while True:
            try:
                messages = await sub.fetch(batch=32, timeout=1)
                for message in messages:
                    try:
                        command = CancelCommand.model_validate_json(message.data)
                    except ValidationError:
                        logger.warning("Invalid cancellation command")
                    else:
                        # Scheduling is immediate; do not block other runs on one tool's cleanup.
                        handler(command)
                    await message.ack()
            except nats.errors.TimeoutError:
                continue
            except nats.errors.Error as exc:
                logger.warning("Cancellation subscription error (%s)", type(exc).__name__)
                await asyncio.sleep(1)
