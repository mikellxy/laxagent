"""Scenario-specific run finalization and best-effort terminal notifications."""
import asyncio
import logging

import anyio

from laxagent.run_errors import RunError, make_run_error, registered_run_error
from laxagent.runtime.cancellation import CancellationBus, CancelResult, RunSnapshot
from laxagent.runtime.events import EventStoreError, make_event
from laxagent.storage.database import DatabaseError
from laxagent.storage.models import Run

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

    async def finish_submission_failed(self, run_id: int, resume_time: int):
        """Finish a submission rejected during stream creation, shutdown, or dispatch."""
        return await self._finish_failure(run_id, resume_time, make_run_error("run_submission_failed"))

    async def finish_startup_interrupted(self, run_id: int, resume_time: int):
        """Finish a non-user interruption before execution starts or during startup checks."""
        return await self._finish_interrupted(run_id, resume_time)

    async def finish_execution_interrupted(self, run_id: int, resume_time: int):
        """Finish a non-user interruption after the runner's execution cleanup exits."""
        return await self._finish_interrupted(run_id, resume_time)

    async def finish_startup_failed(self, run_id: int, resume_time: int, error: RunError):
        """Finish a startup check that failed with a classified run error."""
        return await self._finish_failure(run_id, resume_time, error)

    async def finish_execution_failed(self, run_id: int, resume_time: int, error: RunError):
        """Finish an execution failure without replacing the runner's original error."""
        return await self._finish_failure(run_id, resume_time, error)

    async def finish_user_cancelled(self, run_id: int, resume_time: int) -> Run:
        """Finalize user cancellation after execution and resource cleanup have stopped.

        Preserve an existing terminal state and always acknowledge the request.
        Persistence errors propagate so the caller can retain the execution for retry.
        """
        return await drain(asyncio.create_task(self._commit(
            run_id, resume_time, cancelled=True, terminal_status="cancelled",
        )))

    async def finish_paused(self, run_id: int, resume_time: int, question: dict):
        """Commit a checkpointed pause before advertising an answerable question."""
        return await drain(asyncio.create_task(self._pause(run_id, resume_time, question)))

    async def _pause(self, run_id: int, resume_time: int, question: dict):
        current = await self.repository.pause_run(run_id, resume_time)
        if current.status != "paused":
            return current
        if current.cancel_requested_at is not None:
            return await self.finish_user_cancelled(run_id, resume_time)
        await self.events.publish(make_event(run_id, "ask_user_question", question, resume_time=resume_time))
        await self.events.publish(make_event(run_id, "sse_end", {"status": "paused"}, resume_time=resume_time))
        return current

    async def finish_succeeded(self, run_id: int, resume_time: int) -> Run:
        """Finalize a normally returned Graph; propagate persistence errors to the runner."""
        return await drain(asyncio.create_task(self._commit(
            run_id, resume_time, terminal_status="completed",
        )))

    async def _finish_interrupted(self, run_id: int, resume_time: int):
        # Both phases deliberately share a failed DB state and cancelled SSE status.
        return await self._finish_failure(
            run_id, resume_time, make_run_error("execution_cancelled"), terminal_status="cancelled",
        )

    async def _finish_failure(
        self, run_id: int, resume_time: int, error: RunError, *, terminal_status: str | None = None,
    ):
        error = registered_run_error(error)
        if terminal_status is None:
            terminal_status = "incomplete" if error.incomplete else "failed"
        # Bound cleanup and preserve the caller's original exception on failure.
        try:
            return await drain(asyncio.create_task(self._commit(
                run_id, resume_time, error=error, terminal_status=terminal_status, timeout=5,
            )))
        except Exception:
            logger.error("Could not persist %s run %s",
                         "interrupted" if terminal_status == "cancelled" else "failed", run_id)

    async def _commit(
        self, run_id: int, resume_time: int, *, terminal_status: str,
        error: RunError | None = None, cancelled: bool = False, timeout: float | None = None,
    ) -> Run:
        """Persist once, publish only on transition, and reply with the committed snapshot."""
        try:
            with anyio.fail_after(timeout):
                current, changed = await self.repository.finish_run(
                    run_id, resume_time=resume_time, error=error, cancelled=cancelled,
                )
        except Exception as exc:
            # Storage failure can prevent reading a concurrent cancellation intent.
            await self._reply_storage_error(run_id, resume_time, exc)
            raise
        try:
            if changed:
                with anyio.move_on_after(timeout) as scope:
                    await self._terminal_events(current, terminal_status)
                if scope.cancelled_caught:
                    logger.warning("Terminal SSE timed out for run %s", run_id)
        finally:
            # Use the transaction's returned object; a later resume does not refresh it.
            if cancelled or current.cancel_requested_at is not None:
                await self._reply(CancelResult(
                    run_id=run_id, resume_time=resume_time, run=RunSnapshot.model_validate(current),
                ))
        return current

    async def _reply_storage_error(self, run_id: int, resume_time: int, exc: Exception):
        conflict = isinstance(exc, DatabaseError) and exc.status == 409
        await self._reply(CancelResult(
            run_id=run_id, resume_time=resume_time, status=409 if conflict else 503,
            error=("任务执行次数已变化，请刷新状态" if conflict else
                   "取消收尾状态保存失败，请重试" if isinstance(exc, (DatabaseError, TimeoutError)) else
                   "取消收尾失败，请重试"),
        ))

    async def _terminal_events(self, current: Run, status: str):
        # User cancellation and service interruption share the cancelled SSE status.
        event_type = {"failed": "error", "incomplete": "response_incomplete"}.get(status)
        try:
            if event_type is not None:
                await self.events.publish(make_event(
                    current.id, event_type, {}, error=current.error, resume_time=current.resume_time,
                ))
            await self.events.publish(make_event(
                current.id, "sse_end", {"status": status},
                error=current.error, resume_time=current.resume_time,
            ))
        except EventStoreError:
            logger.warning("Could not publish terminal SSE for run %s", current.id)

    async def _reply(self, result: CancelResult):
        try:
            with anyio.move_on_after(5, shield=True) as scope:
                await self.cancellation.reply(result)
            if scope.cancelled_caught:
                logger.warning("Cancellation result timed out for run %s", result.run_id)
        except EventStoreError:
            logger.warning("Could not publish cancellation result for run %s", result.run_id)
