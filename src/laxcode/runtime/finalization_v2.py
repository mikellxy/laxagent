"""Scenario-specific run finalization, migrated one caller at a time."""
import asyncio
import logging
from collections.abc import Awaitable, Callable

import anyio

from laxcode.run_errors import RunError, make_run_error
from laxcode.runtime.cancellation import CancellationBus, CancelResult, RunSnapshot
from laxcode.runtime.events import EventStoreError, make_event
from laxcode.runtime.finalization import drain
from laxcode.storage.database import DatabaseError
from laxcode.storage.models import Run

logger = logging.getLogger(__name__)


class RunFinalizerV2:
    def __init__(self, repository, events, cancellation=None):
        self.repository = repository
        self.events = events
        self.cancellation = cancellation if cancellation is not None else CancellationBus(events)

    async def finish_submission_failed(self, run_id: int, resume_time: int):
        """Finish a submission rejected during stream creation, shutdown, or dispatch."""
        # Bound cleanup and preserve the original submission exception on failure.
        try:
            return await drain(asyncio.create_task(self._commit_failure(
                run_id, resume_time, error=make_run_error("run_submission_failed"),
                publish_events=self._failure_events,
            )))
        except Exception:
            logger.error("Could not persist failed run %s", run_id)

    async def finish_startup_interrupted(self, run_id: int, resume_time: int):
        """Finish a startup check interrupted independently of user cancellation."""
        try:
            return await drain(asyncio.create_task(self._commit_failure(
                run_id, resume_time, error=make_run_error("execution_cancelled"),
                publish_events=self._cancelled_events,
            )))
        except Exception:
            logger.error("Could not persist interrupted run %s", run_id)

    async def finish_startup_failed(self, run_id: int, resume_time: int, error: RunError):
        """Finish a startup check that failed with a classified run error."""
        try:
            return await drain(asyncio.create_task(self._commit_failure(
                run_id, resume_time, error=error, publish_events=self._failure_events,
            )))
        except Exception:
            logger.error("Could not persist failed run %s", run_id)

    async def finish_user_cancelled(self, run_id: int, resume_time: int) -> Run:
        """Finalize user cancellation after execution and resource cleanup have stopped.

        Preserve an existing terminal state and always acknowledge the request.
        Persistence errors propagate so the caller can retain the execution for retry.
        """
        return await drain(asyncio.create_task(self._commit_user_cancellation(run_id, resume_time)))

    async def _commit_user_cancellation(self, run_id: int, resume_time: int) -> Run:
        try:
            current, changed = await self.repository.finish_run(
                run_id, resume_time=resume_time, cancelled=True,
            )
        except Exception as exc:
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
                await self._cancelled_events(current)
        finally:
            await self._reply(CancelResult(
                run_id=run_id, resume_time=resume_time, run=RunSnapshot.model_validate(current),
            ))
        return current

    async def _failure_events(self, current: Run):
        try:
            await self.events.publish(make_event(
                current.id, "error", {}, error=current.error, resume_time=current.resume_time,
            ))
            await self.events.publish(make_event(
                current.id, "sse_end", {"status": "failed"},
                error=current.error, resume_time=current.resume_time,
            ))
        except EventStoreError:
            logger.warning("Could not publish terminal SSE for run %s", current.id)

    async def _cancelled_events(self, current: Run):
        # User cancellation and service interruption share this SSE status.
        try:
            await self.events.publish(make_event(
                current.id, "sse_end", {"status": "cancelled"},
                error=current.error, resume_time=current.resume_time,
            ))
        except EventStoreError:
            logger.warning("Could not publish terminal SSE for run %s", current.id)

    async def _commit_failure(
        self, run_id: int, resume_time: int, *,
        error: RunError, publish_events: Callable[[Run], Awaitable[None]],
    ) -> Run:
        """Commit a failure, publish its scenario's events, and acknowledge cancellation."""
        try:
            with anyio.fail_after(5):
                current, changed = await self.repository.finish_run(
                    run_id, resume_time=resume_time, error=error,
                )
        except Exception as exc:
            # Storage failure can prevent reading a concurrent cancellation intent.
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
                with anyio.move_on_after(5) as scope:
                    await publish_events(current)
                if scope.cancelled_caught:
                    logger.warning("Terminal SSE timed out for run %s", run_id)
        finally:
            # Use the transaction's returned object; a later resume does not refresh it.
            if current.cancel_requested_at is not None:
                await self._reply(CancelResult(
                    run_id=run_id, resume_time=resume_time, run=RunSnapshot.model_validate(current),
                ))
        return current

    async def _reply(self, result: CancelResult):
        try:
            with anyio.move_on_after(5, shield=True) as scope:
                await self.cancellation.reply(result)
            if scope.cancelled_caught:
                logger.warning("Cancellation result timed out for run %s", result.run_id)
        except EventStoreError:
            logger.warning("Could not publish cancellation result for run %s", result.run_id)
