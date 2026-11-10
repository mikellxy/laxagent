import asyncio
import logging
from collections.abc import Callable
from math import inf

import anyio
from langgraph.errors import GraphRecursionError

from laxcode.agent.context import AgentContext
from laxcode.agent.state import AgentState
from laxcode.run_errors import RunError, make_run_error, registered_run_error
from laxcode.runtime.events import EventStoreError, make_event
from laxcode.storage.database import DatabaseError

logger = logging.getLogger(__name__)


async def drain(task):
    """Do not let repeated host cancellation interrupt a terminal commit/event pair."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise
    return task.result()


def classify_run_error(exc: Exception) -> RunError:
    """The single boundary mapping execution failures to the public run catalog."""
    if isinstance(exc, RunError):
        return registered_run_error(exc)
    if isinstance(exc, GraphRecursionError):
        return make_run_error("graph_step_limit_reached")
    if isinstance(exc, DatabaseError):
        return make_run_error("run_storage_failed")
    if isinstance(exc, EventStoreError):
        return make_run_error("run_event_failed")
    return make_run_error("agent_execution_failed")


async def fail_run(run_id: int, context: AgentContext, error: RunError, *, cancelled=False):
    """Best-effort bounded cleanup; never replace the original execution error."""
    error = registered_run_error(error)
    message, incomplete = str(error), error.incomplete
    async def finish():
        with anyio.move_on_after(5, shield=True):
            try:
                changed = await context.repository.finish_run(run_id, error=error)
            except DatabaseError:
                logger.error("Could not persist failed run %s", run_id)
                return
            if not changed:
                return
            try:
                if not cancelled:
                    await context.events.publish(make_event(
                        run_id, "response_incomplete" if incomplete else "error", {}, error=message,
                    ))
                status = "cancelled" if cancelled else "incomplete" if incomplete else "failed"
                await context.events.publish(make_event(run_id, "sse_end", {"status": status}, error=message))
            except EventStoreError:
                logger.warning("Could not publish failure for run %s", run_id)
    await drain(asyncio.create_task(finish()))


async def complete_run(run_id: int, context: AgentContext):
    async def finish():
        changed = await context.repository.finish_run(run_id)
        if changed:
            try:
                await context.events.publish(make_event(run_id, "sse_end", {"status": "completed"}))
            except EventStoreError:
                logger.warning("Run %s succeeded but its completion event could not be published", run_id)

    # Once natural completion starts, finish the commit/event pair despite a
    # racing cancellation. The controller drains this worker and keeps its status.
    await drain(asyncio.create_task(finish()))


async def run_agent(graph, run_id: int, context: AgentContext, *,
                    user_cancelled: Callable[[], bool] = lambda: False) -> AgentState | None:
    try:
        run = await context.repository.claim_run(run_id)
        if run is None:
            return None
        # Recheck ownership after dispatch; API validation is not execution authorization.
        await context.repository.get_run(run_id, run.user_id)
        user_configs = await context.repository.get_user_configs(run.user_id)
        state: AgentState = {
            "user_configs": user_configs,
            "user_id": run.user_id, "project_id": run.project_id, "session_id": run.session_id,
            "run_id": run.id, "user_query": run.user_query, "model": run.model,
            "messages": [], "model_input": [], "pending_tool_calls": [],
        }
        # LangGraph rejects -1; infinity disables its step cutoff without a finite substitute.
        recursion_limit = inf if context.recursion_limit == -1 else context.recursion_limit
        result = await graph.ainvoke(state, config={
            "recursion_limit": recursion_limit, "configurable": {"thread_id": run.thread_id},
        }, context=context)
        await complete_run(run_id, context)
    except asyncio.CancelledError:
        # User cancellation is terminalized by the manager after this worker fully exits.
        if not user_cancelled():
            await fail_run(run_id, context, make_run_error("execution_cancelled"), cancelled=True)
        raise
    except Exception as exc:
        error = classify_run_error(exc)
        await fail_run(run_id, context, error)
        raise error from exc

    return result
