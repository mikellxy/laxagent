import asyncio
import logging

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
        self.closing = False

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
        run = await self.context.repository.create_run(**values)
        try:
            await self.context.events.create_run(run.id)
            if self.closing:
                raise DatabaseError("Agent runtime is shutting down")
            task = asyncio.create_task(run_agent(self.graph, run.id, self.context), name=f"run-{run.id}")
            self.tasks[run.id] = task
            task.add_done_callback(lambda completed: self.tasks.pop(run.id, None))
            task.add_done_callback(self.consume)
            return run
        except BaseException:
            await fail_run(run.id, self.context, "Run submission failed")
            raise

    async def close(self):
        self.closing = True
        # Let submission commits resolve before cancelling executions so every accepted run
        # is accounted for. Submissions only do DB/NATS setup, never model work.
        if self.submissions:
            await asyncio.gather(*self.submissions, return_exceptions=True)
        active = list(self.tasks.items())
        for _, task in active:
            task.cancel()
        await asyncio.gather(*(task for _, task in active), return_exceptions=True)
        for run_id, _ in active:
            # Covers a task cancelled before its coroutine entered run_agent.
            await fail_run(run_id, self.context, "Execution cancelled", cancelled=True)
