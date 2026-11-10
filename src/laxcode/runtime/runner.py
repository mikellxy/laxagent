import asyncio
import logging
from math import inf

import anyio
from langgraph.errors import GraphRecursionError

from laxcode.agent.context import AgentContext
from laxcode.agent.state import AgentState
from laxcode.models.errors import ModelError
from laxcode.runtime.events import EventStoreError, make_event
from laxcode.storage.database import DatabaseError

logger = logging.getLogger(__name__)


async def fail_run(run_id: int, context: AgentContext, error: str, *, incomplete=False, cancelled=False):
    """Best-effort bounded cleanup; never replace the original execution error."""
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
                    run_id, "response_incomplete" if incomplete else "error", {}, error=error,
                ))
            status = "cancelled" if cancelled else "incomplete" if incomplete else "failed"
            await context.events.publish(make_event(run_id, "sse_end", {"status": status}, error=error))
        except EventStoreError:
            logger.warning("Could not publish failure for run %s", run_id)


async def run_agent(graph, run_id: int, context: AgentContext) -> AgentState | None:
    try:
        run = await context.repository.claim_run(run_id)
        if run is None:
            return None
        # Recheck ownership after dispatch; API validation is not execution authorization.
        await context.repository.get_run(run_id, run.user_id)
        state: AgentState = {
            "user_id": run.user_id, "project_id": run.project_id, "session_id": run.session_id,
            "run_id": run.id, "user_query": run.user_query, "model": run.model,
            "messages": [], "model_input": [], "pending_tool_calls": [],
        }
        # LangGraph rejects -1; infinity disables its step cutoff without a finite substitute.
        recursion_limit = inf if context.recursion_limit == -1 else context.recursion_limit
        result = await graph.ainvoke(state, config={
            "recursion_limit": recursion_limit, "configurable": {"thread_id": run.thread_id},
        }, context=context)
        await context.repository.finish_run(run_id)
    except asyncio.CancelledError:
        await fail_run(run_id, context, "Execution cancelled", cancelled=True)
        raise
    except Exception as exc:
        if isinstance(exc, GraphRecursionError):
            error = ModelError("Agent graph step limit reached")
        elif isinstance(exc, (ModelError, DatabaseError, EventStoreError)):
            error = exc
        else:
            error = ModelError("Agent execution failed")
        await fail_run(run_id, context, str(error),
                       incomplete=isinstance(error, ModelError) and error.incomplete)
        raise error from None

    try:
        await context.events.publish(make_event(run_id, "sse_end", {"status": "completed"}))
    except EventStoreError:
        logger.warning("Run %s succeeded but its completion event could not be published", run_id)
    return result
