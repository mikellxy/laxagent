import asyncio
import logging
from dataclasses import dataclass, field, replace

from laxcode.runtime.cancellation import CancellationBus, CancelCommand, RunSnapshot
from laxcode.runtime.events import EventStoreError
from laxcode.runtime.finalization_v2 import RunFinalizerV2
from laxcode.runtime.runner import classify_run_error, run_agent
from laxcode.storage.database import DatabaseError
from laxcode.storage.models import Run

logger = logging.getLogger(__name__)
ACTIVE = ("waiting", "running")


@dataclass
class LocalExecution:
    run: Run
    task: asyncio.Task = field(init=False)
    cancel_job: asyncio.Task | None = None
    user_cancelled: bool = False
    started: bool = False

    @property
    def key(self) -> tuple[int, int]:
        return self.run.id, self.run.resume_time


class TaskManager:
    """Own local execution handles; coordinate admission and cancellation externally."""

    def __init__(self, graph, context, *, cancel_timeout=30):
        self.graph = graph
        self.context = context
        self.cancel_timeout = cancel_timeout
        self.cancellation = CancellationBus(context.events)
        self.finalizer_v2 = RunFinalizerV2(context.repository, context.events, self.cancellation)
        self.executions: dict[tuple[int, int], LocalExecution] = {}
        self.submissions: set[asyncio.Task] = set()
        self.controls: set[asyncio.Task] = set()
        self.closing = False
        self._listener = None

    async def start(self):
        self._listener = self.cancellation.listen(self._accept_cancel)
        await self._listener.__aenter__()

    @staticmethod
    def consume(task):
        if not task.cancelled() and task.exception() is not None:
            logger.warning("Background task failed (%s)", type(task.exception()).__name__)

    async def submit(self, **values):
        if self.closing:
            raise DatabaseError("Agent runtime is shutting down")
        task = asyncio.create_task(self._submit(values), name="submit-run")
        self.submissions.add(task)
        task.add_done_callback(self.submissions.discard)
        task.add_done_callback(self.consume)
        return await asyncio.shield(task)

    async def _submit(self, values):
        run = await self.context.repository.create_run(**values)
        try:
            await self.context.events.create_run(run.id)
        except BaseException:
            await self.finalizer_v2.finish_submission_failed(run.id, run.resume_time)
            raise
        if self.closing:
            await self.finalizer_v2.finish_submission_failed(run.id, run.resume_time)
            raise DatabaseError("Agent runtime is shutting down")
        try:
            self._dispatch(run)
            return run
        except BaseException:
            await self.finalizer_v2.finish_submission_failed(run.id, run.resume_time)
            raise

    def _remove_execution(self, execution):
        if self.executions.get(execution.key) is execution:
            self.executions.pop(execution.key)

    def _dispatch(self, run):
        execution = LocalExecution(run)
        if execution.key in self.executions:
            raise RuntimeError("Execution already registered")
        task = asyncio.create_task(self._execute(execution), name=f"run-{run.id}-{run.resume_time}")
        execution.task = task
        self.executions[execution.key] = execution

        def remove(completed):
            # Keep stopped executions whose cancellation commit needs a user retry.
            if not execution.user_cancelled:
                self._remove_execution(execution)
        task.add_done_callback(remove)
        task.add_done_callback(self.consume)

    async def _execute(self, execution):
        execution.started = True
        run = execution.run
        context = replace(self.context, resume_time=run.resume_time)
        try:
            current = await context.repository.get_run(run.id, run.user_id)
            if current.resume_time != run.resume_time or current.status not in ACTIVE:
                return
            # One startup check closes the commit-to-dispatch gap; there is no periodic scan.
            if current.cancel_requested_at is not None:
                self._accept_cancel(CancelCommand(run_id=run.id, resume_time=run.resume_time))
                return
        except asyncio.CancelledError:
            if not execution.user_cancelled:
                await self.finalizer_v2.finish_startup_interrupted(*execution.key)
            raise
        except Exception as exc:
            await self.finalizer_v2.finish_startup_failed(*execution.key, classify_run_error(exc))
            raise
        # run_agent alone owns execution errors; the block above handles startup only.
        await run_agent(self.graph, run.id, context, user_cancelled=lambda: execution.user_cancelled,
                        finalizer_v2=self.finalizer_v2)

    async def resume(self, user_id: str, run_id: int, resume_time: int):
        return await self.control(self._resume, user_id, run_id, resume_time)

    async def _resume(self, user_id, run_id, resume_time):
        repository = self.context.repository
        run = await repository.get_run(run_id, user_id)
        _, latest = await repository.session_detail(user_id, run.session_id)
        if latest is None or latest.id != run.id or run.status not in ("failed", "cancelled") or run.resume_time != resume_time:
            raise DatabaseError("任务状态已变化，请刷新后重试", 409)
        snapshot = await self.graph.aget_state({"configurable": {"thread_id": run.thread_id}})
        if not snapshot.values or snapshot.values.get("run_id") != run.id:
            raise DatabaseError("恢复点不存在或已失效，请发送新消息重新开始", 409)
        if self.closing:
            raise DatabaseError("Agent runtime is shutting down")
        await self.context.events.create_run(run.id, exist_ok=True)
        # The repository repeats admission checks inside the session row lock.
        run = await repository.resume_run(user_id, run_id, resume_time)
        try:
            self._dispatch(run)
        except BaseException:
            await self.finalizer_v2.finish_submission_failed(run.id, run.resume_time)
            raise
        return run

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
        run = await self.context.repository.get_run(run_id, user_id)
        return await self._request_cancel(run)

    async def _request_cancel(self, run):
        if run.status not in ACTIVE:
            return RunSnapshot.model_validate(run)
        command = CancelCommand(run_id=run.id, resume_time=run.resume_time)
        try:
            async with asyncio.timeout(self.cancel_timeout):
                async with self.cancellation.results(run.id, run.resume_time) as sub:
                    current = await self.context.repository.request_cancellation(run.user_id, run.id, run.resume_time)
                    if current.status not in ACTIVE:
                        return RunSnapshot.model_validate(current)
                    await self.cancellation.request(command)
                    return await self.cancellation.wait_result(sub, command)
        except TimeoutError:
            raise EventStoreError("取消确认等待超时；已提交的取消意图会保留，请刷新状态或重试", 504) from None

    def _accept_cancel(self, command):
        execution = self.executions.get((command.run_id, command.resume_time))
        if execution is None or execution.cancel_job is not None:
            return
        execution.user_cancelled = True
        job = asyncio.create_task(self._stop_local(execution),
                                  name=f"cancel-{command.run_id}-{command.resume_time}")
        execution.cancel_job = job
        job.add_done_callback(lambda done: setattr(execution, "cancel_job", None))
        job.add_done_callback(self.consume)

    async def _stop_local(self, execution):
        task = execution.task
        if not task.done() and not task.cancelling():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        try:
            await self.finalizer_v2.finish_user_cancelled(*execution.key)
        except Exception as exc:
            logger.warning("Cancellation failed for run %s (%s)", execution.run.id, type(exc).__name__)
            if isinstance(exc, DatabaseError) and exc.status == 409:
                self._remove_execution(execution)
            # Other persistence failures retain the stopped record for cancellation retry.
        else:
            self._remove_execution(execution)

    async def delete_session(self, user_id: str, session_id: int):
        return await self.control(self._delete_session, user_id, session_id)

    async def _delete_session(self, user_id, session_id):
        repository = self.context.repository
        session = await repository.session_for_deletion(user_id, session_id)
        if session.deleted_at is not None:
            return
        _, latest = await repository.session_detail(user_id, session_id)
        if latest is not None:
            await self._request_cancel(latest)
        # A new run may have won admission while we waited. Return 409, never chase it.
        await repository.delete_session(user_id, session_id)

    async def close(self):
        self.closing = True
        if self.submissions:
            await asyncio.gather(*self.submissions, return_exceptions=True)
        if self.controls:
            await asyncio.gather(*self.controls, return_exceptions=True)
        await self._drain_cancel_jobs()
        active = list(self.executions.values())
        for execution in active:
            task = execution.task
            if not task.done() and not task.cancelling():
                task.cancel()
        await asyncio.gather(*(execution.task for execution in active), return_exceptions=True)
        for execution in active:
            if not execution.user_cancelled:
                # A task cancelled before its first step never reached either error boundary.
                if not execution.started:
                    await self.finalizer_v2.finish_startup_interrupted(*execution.key)
                self._remove_execution(execution)
        if self._listener is not None:
            await self._listener.__aexit__(None, None, None)
            self._listener = None
        await self._drain_cancel_jobs()
        self.executions.clear()

    async def _drain_cancel_jobs(self):
        jobs = [execution.cancel_job for execution in list(self.executions.values())
                if execution.cancel_job is not None]
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)
