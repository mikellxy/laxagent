"""Terminal persistence and best-effort notifications, independent of local workers."""
import asyncio
import logging

import anyio

from laxcode.run_errors import RunError, registered_run_error
from laxcode.runtime.cancellation import CancellationBus, CancelResult, RunSnapshot
from laxcode.runtime.events import EventStoreError, make_event
from laxcode.storage.database import DatabaseError

logger = logging.getLogger(__name__)


async def drain(task):
    """Finish a terminal commit/notification pair despite racing host cancellation."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise
    return task.result()


class RunFinalizer:
    def __init__(self, repository, events, cancellation=None):
        self.repository = repository
        self.events = events
        self.cancellation = cancellation if cancellation is not None else CancellationBus(events)

    async def finish(self, run_id: int, resume_time: int, *, error: RunError | None = None,
                     cancelled=False, interrupted=False, reply=False, timeout=None) -> RunSnapshot:
        """Call only after Graph/tool cleanup, or after independently proving execution stopped.

        Only the winning DB transition emits SSE. A repeated call can still acknowledge
        cancellation; notifications are best effort, not an atomic DB/message transaction.
        `interrupted` preserves shutdown's failed DB state and cancelled SSE contract.
        """
        error = registered_run_error(error) if error is not None else None

        async def finish():
            try:
                with anyio.fail_after(timeout):
                    current, changed = await self.repository.finish_run(
                        run_id, resume_time=resume_time, error=error, cancelled=cancelled,
                    )
                    snapshot = RunSnapshot.model_validate(current)
            except Exception as exc:
                # Even if storage is unavailable and intent cannot be read, notify any waiters.
                conflict = isinstance(exc, DatabaseError) and exc.status == 409
                await self._reply(CancelResult(
                    run_id=run_id, resume_time=resume_time, status=409 if conflict else 503,
                    error=("任务执行次数已变化，请刷新状态" if conflict else
                           "取消收尾状态保存失败，请重试" if isinstance(exc, (DatabaseError, TimeoutError)) else
                           "取消收尾失败，请重试"),
                ))
                raise
            try:
                if changed:
                    with anyio.move_on_after(timeout) as scope:
                        await self._events(snapshot, error, interrupted=interrupted)
                    if scope.cancelled_caught:
                        logger.warning("Terminal SSE timed out for run %s", run_id)
            finally:
                # Use the transaction's snapshot: resume may already have cleared the intent.
                if reply or current.cancel_requested_at is not None:
                    await self._reply(CancelResult(run_id=run_id, resume_time=resume_time, run=snapshot))
            return snapshot

        return await drain(asyncio.create_task(finish()))

    async def fail(self, run_id: int, resume_time: int, error: RunError, *, interrupted=False):
        """Bounded best-effort failure cleanup; never replace the execution exception."""
        try:
            return await self.finish(run_id, resume_time, error=error, interrupted=interrupted, timeout=5)
        except Exception:
            logger.error("Could not persist failed run %s", run_id)

    async def _events(self, snapshot, error, *, interrupted):
        message = snapshot.error
        incomplete = error is not None and error.incomplete
        status = ("completed" if snapshot.status == "success" else
                  "cancelled" if snapshot.status == "cancelled" or interrupted else
                  "incomplete" if incomplete else "failed")
        try:
            if snapshot.status == "failed" and not interrupted:
                await self.events.publish(make_event(
                    snapshot.id, "response_incomplete" if incomplete else "error", {},
                    error=message, resume_time=snapshot.resume_time,
                ))
            await self.events.publish(make_event(
                snapshot.id, "sse_end", {"status": status}, error=message, resume_time=snapshot.resume_time,
            ))
        except EventStoreError:
            logger.warning("Could not publish terminal SSE for run %s", snapshot.id)

    async def _reply(self, result):
        try:
            with anyio.move_on_after(5, shield=True) as scope:
                await self.cancellation.reply(result)
            if scope.cancelled_caught:
                logger.warning("Cancellation result timed out for run %s", result.run_id)
        except EventStoreError:
            logger.warning("Could not publish cancellation result for run %s", result.run_id)
