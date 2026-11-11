import asyncio
import logging
from dataclasses import replace

from laxcode.run_errors import make_run_error
from laxcode.runtime.cancellation import CancellationBus, CancelCommand, CancelResult, RunSnapshot
from laxcode.runtime.events import EventStoreError, make_event
from laxcode.runtime.runner import classify_run_error, fail_run, run_agent
from laxcode.storage.database import DatabaseError

logger = logging.getLogger(__name__)
ACTIVE = ("waiting", "running")


class TaskManager:
    """Own local execution handles; coordinate admission and cancellation externally."""

    def __init__(self, graph, context, *, cancel_timeout=30):
        self.graph = graph
        self.context = context
        self.cancel_timeout = cancel_timeout
        self.cancellation = CancellationBus(context.events)
        self.tasks: dict[int, asyncio.Task] = {}
        self.task_attempts: dict[int, int] = {}
        self.workers: dict[asyncio.Task, object] = {}
        self.submissions: set[asyncio.Task] = set()
        self.controls: set[asyncio.Task] = set()
        self.cancel_jobs: dict[tuple[int, int], asyncio.Task] = {}
        self.user_cancellations: set[tuple[int, int]] = set()
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
            if self.closing:
                raise DatabaseError("Agent runtime is shutting down")
            self._dispatch(run)
            return run
        except BaseException:
            await fail_run(run.id, self.context, make_run_error("run_submission_failed"))
            raise

    def _remove_worker(self, run, task):
        self.workers.pop(task, None)
        if self.tasks.get(run.id) is task:
            self.tasks.pop(run.id)
            self.task_attempts.pop(run.id)

    def _dispatch(self, run):
        task = asyncio.create_task(self._execute(run), name=f"run-{run.id}-{run.resume_time}")
        self.tasks[run.id] = task
        self.task_attempts[run.id] = run.resume_time
        self.workers[task] = run

        def remove(completed):
            # Retain a stopped handle if committing cancellation failed, so a retry can finish it.
            if (run.id, run.resume_time) not in self.user_cancellations:
                self._remove_worker(run, completed)
        task.add_done_callback(remove)
        task.add_done_callback(self.consume)

    async def _execute(self, run):
        key = (run.id, run.resume_time)
        context = replace(self.context, resume_time=run.resume_time)
        try:
            current = await context.repository.get_run(run.id, run.user_id)
            if current.resume_time != run.resume_time or current.status not in ACTIVE:
                return
            # One startup check closes the commit-to-dispatch gap; there is no periodic scan.
            if current.cancel_requested_at is not None:
                self._accept_cancel(CancelCommand(run_id=run.id, resume_time=run.resume_time))
                return
            await run_agent(self.graph, run.id, context, user_cancelled=lambda: key in self.user_cancellations)
        except asyncio.CancelledError:
            if key not in self.user_cancellations:
                await fail_run(run.id, context, make_run_error("execution_cancelled"), cancelled=True)
            raise
        except Exception as exc:
            await fail_run(run.id, context, classify_run_error(exc))
            raise
        finally:
            # Natural completion can win before the broadcast is consumed. Notify existing
            # waiters if intent was recorded; they subscribed before committing that intent.
            if key not in self.user_cancellations:
                try:
                    current = await context.repository.get_run(run.id, run.user_id)
                    if (current.resume_time == run.resume_time and current.status not in ACTIVE
                            and current.cancel_requested_at is not None):
                        await self._reply(CancelResult(run_id=run.id, resume_time=run.resume_time,
                                                       run=RunSnapshot.model_validate(current)))
                except DatabaseError:
                    logger.warning("Could not read terminal cancellation result for run %s", run.id)

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
            await fail_run(run.id, replace(self.context, resume_time=run.resume_time),
                           make_run_error("run_submission_failed"))
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
        key = (command.run_id, command.resume_time)
        if self.task_attempts.get(command.run_id) != command.resume_time or key in self.cancel_jobs:
            return
        task = self.tasks.get(command.run_id)
        if task is None:
            return
        run = self.workers[task]
        self.user_cancellations.add(key)
        job = asyncio.create_task(self._stop_local(run, task), name=f"cancel-{run.id}-{run.resume_time}")
        self.cancel_jobs[key] = job
        job.add_done_callback(lambda done: self.cancel_jobs.pop(key, None))
        job.add_done_callback(self.consume)

    async def _reply(self, result):
        try:
            await self.cancellation.reply(result)
        except EventStoreError:
            logger.warning("Could not publish cancellation result for run %s", result.run_id)

    async def _stop_local(self, run, task):
        key = (run.id, run.resume_time)
        try:
            if not task.done() and not task.cancelling():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            current, changed = await self.context.repository.finish_cancellation(*key)
            snapshot = RunSnapshot.model_validate(current)
            self.user_cancellations.discard(key)
            self._remove_worker(run, task)
            if changed:
                try:
                    await self.context.events.publish(make_event(run.id, "sse_end", {"status": "cancelled"},
                                                                resume_time=run.resume_time))
                except EventStoreError:
                    logger.warning("Could not publish cancellation SSE for run %s", run.id)
            await self._reply(CancelResult(run_id=run.id, resume_time=run.resume_time, run=snapshot))
        except Exception as exc:
            # This is a control failure, not a run failure. Never leak exception payloads.
            logger.warning("Cancellation failed for run %s (%s)", run.id, type(exc).__name__)
            conflict = isinstance(exc, DatabaseError) and exc.status == 409
            if conflict:
                self.user_cancellations.discard(key)
                self._remove_worker(run, task)
            await self._reply(CancelResult(run_id=run.id, resume_time=run.resume_time,
                status=409 if conflict else 503,
                error=("任务执行次数已变化，请刷新状态" if conflict else
                       "取消收尾状态保存失败，请重试" if isinstance(exc, DatabaseError) else
                       "取消收尾失败，请重试")))

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
        if self.cancel_jobs:
            await asyncio.gather(*self.cancel_jobs.values(), return_exceptions=True)
        active = list(self.workers.items())
        for task, _ in active:
            if not task.done() and not task.cancelling():
                task.cancel()
        await asyncio.gather(*(task for task, _ in active), return_exceptions=True)
        for task, run in active:
            if (run.id, run.resume_time) not in self.user_cancellations:
                await fail_run(run.id, replace(self.context, resume_time=run.resume_time),
                               make_run_error("execution_cancelled"), cancelled=True)
            self._remove_worker(run, task)
        if self._listener is not None:
            await self._listener.__aexit__(None, None, None)
            self._listener = None
        if self.cancel_jobs:
            await asyncio.gather(*self.cancel_jobs.values(), return_exceptions=True)
