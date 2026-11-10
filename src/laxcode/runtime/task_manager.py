import asyncio
import logging
from weakref import WeakValueDictionary

from laxcode.runtime.events import EventStoreError, make_event
from laxcode.runtime.runner import fail_run, run_agent
from laxcode.storage.database import DatabaseError

logger = logging.getLogger(__name__)


class TaskManager:
    """Application-owned submissions and executions, all on the API event loop."""

    def __init__(self, graph, context):
        self.graph = graph
        self.context = context
        self.tasks: dict[int, asyncio.Task] = {}
        self.submissions: set[asyncio.Task] = set()
        self.controls: set[asyncio.Task] = set()
        self.locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()
        self.user_cancellations: set[int] = set()
        self.closing = False

    def session_lock(self, session_id):
        lock = self.locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self.locks[session_id] = lock
        return lock

    @staticmethod
    def consume(task):
        if not task.cancelled() and task.exception() is not None:
            # Exception payloads may include upstream secrets; runner persists a safe reason.
            logger.warning("Background task failed (%s)", type(task.exception()).__name__)

    async def submit(self, **values):
        if self.closing:
            raise DatabaseError("Agent runtime is shutting down")
        task = asyncio.create_task(self._submit(values), name="submit-run")
        self.submissions.add(task)
        task.add_done_callback(self.submissions.discard)
        task.add_done_callback(self.consume)
        # A disconnected request cannot strand a committed waiting run between DB and NATS.
        return await asyncio.shield(task)

    async def _submit(self, values):
        async with self.session_lock(values["session_id"]):
            return await self._start(values)

    async def _start(self, values):
        run = await self.context.repository.create_run(**values)
        try:
            await self.context.events.create_run(run.id)
            if self.closing:
                raise DatabaseError("Agent runtime is shutting down")
            task = asyncio.create_task(run_agent(
                self.graph, run.id, self.context,
                user_cancelled=lambda: run.id in self.user_cancellations,
            ), name=f"run-{run.id}")
            self.tasks[run.id] = task
            task.add_done_callback(lambda completed: self.tasks.pop(run.id, None))
            task.add_done_callback(self.consume)
            return run
        except BaseException:
            await fail_run(run.id, self.context, "Run submission failed")
            raise

    async def control(self, operation, *args):
        if self.closing:
            raise DatabaseError("Agent runtime is shutting down")
        task = asyncio.create_task(operation(*args), name="run-control")
        self.controls.add(task)
        task.add_done_callback(self.controls.discard)
        task.add_done_callback(self.consume)
        return await asyncio.shield(task)

    async def cancel(self, user_id: str, run_id: int):
        return await self.control(self._cancel, user_id, run_id)

    async def _cancel(self, user_id, run_id):
        repository = self.context.repository
        run = await repository.get_run(run_id, user_id)
        async with self.session_lock(run.session_id):
            # Deletion may have completed while this operation waited for the lock.
            run = await repository.get_run(run_id, user_id)
            await self._stop(run)
            return await repository.get_run(run_id, user_id)

    async def _stop(self, run):
        task = self.tasks.get(run.id)
        self.user_cancellations.add(run.id)
        try:
            if task is not None:
                # A terminal worker may still be publishing its final event: drain it
                # without interrupting. Session serialization prevents repeated cancel().
                if run.status in ("waiting", "running") and not task.done() and not task.cancelling():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            changed = await self.context.repository.finish_run(run.id, cancelled=True)
            if changed:
                try:
                    await self.context.events.publish(make_event(run.id, "sse_end", {"status": "cancelled"}))
                except EventStoreError:
                    logger.warning("Could not publish cancellation for run %s", run.id)
        finally:
            self.user_cancellations.discard(run.id)

    async def delete_session(self, user_id: str, session_id: int):
        return await self.control(self._delete_session, user_id, session_id)

    async def _delete_session(self, user_id, session_id):
        repository = self.context.repository
        async with self.session_lock(session_id):
            session = await repository.session_for_deletion(user_id, session_id)
            if session.deleted_at is not None:
                return
            # Also drain a just-finished worker before making its session inaccessible.
            _, latest = await repository.session_detail(user_id, session_id)
            if latest is not None:
                await self._stop(latest)
            await repository.delete_session(user_id, session_id)

    async def close(self):
        self.closing = True
        # Let submission commits resolve before cancelling executions so every accepted run
        # is accounted for. Submissions only do DB/NATS setup, never model work.
        if self.submissions:
            await asyncio.gather(*self.submissions, return_exceptions=True)
        if self.controls:
            await asyncio.gather(*self.controls, return_exceptions=True)
        active = list(self.tasks.items())
        for _, task in active:
            task.cancel()
        await asyncio.gather(*(task for _, task in active), return_exceptions=True)
        for run_id, _ in active:
            # Covers a task cancelled before its coroutine entered run_agent.
            await fail_run(run_id, self.context, "Execution cancelled", cancelled=True)
