"""Scenario-specific run finalization, migrated one caller at a time."""
import asyncio
import logging

import anyio

from laxcode.run_errors import make_run_error
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

    async def finish_stream_creation_failed(self, run_id: int, resume_time: int):
        """Finish a submission whose event stream could not be prepared."""
        return await self._finish_submission_failure(run_id, resume_time)

    async def finish_submission_closed(self, run_id: int, resume_time: int):
        """Finish a submission rejected by shutdown before dispatch."""
        return await self._finish_submission_failure(run_id, resume_time)

    async def finish_dispatch_failed(self, run_id: int, resume_time: int):
        """Finish a submission whose local dispatch raised."""
        return await self._finish_submission_failure(run_id, resume_time)

    async def _finish_submission_failure(self, run_id: int, resume_time: int):
        # These submission failures share one outcome, independent of their trigger.
        # Bound cleanup and preserve the original submission exception on failure.
        try:
            return await drain(asyncio.create_task(self._commit_submission_failure(run_id, resume_time)))
        except Exception:
            logger.error("Could not persist failed run %s", run_id)

    async def _commit_submission_failure(self, run_id: int, resume_time: int) -> Run:
        try:
            with anyio.fail_after(5):
                current, changed = await self.repository.finish_run(
                    run_id, resume_time=resume_time, error=make_run_error("run_submission_failed"),
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
                    await self._submission_failure_events(current)
                if scope.cancelled_caught:
                    logger.warning("Terminal SSE timed out for run %s", run_id)
        finally:
            # Use the transaction's returned object; a later resume does not refresh it.
            if current.cancel_requested_at is not None:
                await self._reply(CancelResult(
                    run_id=run_id, resume_time=resume_time, run=RunSnapshot.model_validate(current),
                ))
        return current

    async def _submission_failure_events(self, current: Run):
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

    async def _reply(self, result: CancelResult):
        try:
            with anyio.move_on_after(5, shield=True) as scope:
                await self.cancellation.reply(result)
            if scope.cancelled_caught:
                logger.warning("Cancellation result timed out for run %s", result.run_id)
        except EventStoreError:
            logger.warning("Could not publish cancellation result for run %s", result.run_id)
