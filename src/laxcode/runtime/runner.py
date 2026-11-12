import asyncio
from collections.abc import Callable
from dataclasses import replace
from math import inf

from langgraph.errors import GraphRecursionError
from langgraph.types import Command
from psycopg import Error as PsycopgError

from laxcode.agent.context import AgentContext
from laxcode.agent.state import AgentState
from laxcode.run_errors import RunError, make_run_error, registered_run_error
from laxcode.runtime.events import EventStoreError
from laxcode.runtime.finalization import RunFinalizer
from laxcode.runtime.finalization_v2 import RunFinalizerV2
from laxcode.storage.database import DatabaseError


def classify_run_error(exc: Exception) -> RunError:
    """The single boundary mapping execution failures to the public run catalog."""
    if isinstance(exc, RunError):
        return registered_run_error(exc)
    if isinstance(exc, GraphRecursionError):
        return make_run_error("graph_step_limit_reached")
    if isinstance(exc, (DatabaseError, PsycopgError)):
        return make_run_error("run_storage_failed")
    if isinstance(exc, EventStoreError):
        return make_run_error("run_event_failed")
    return make_run_error("agent_execution_failed")


async def run_agent(graph, run_id: int, context: AgentContext, *,
                    user_cancelled: Callable[[], bool] = lambda: False,
                    finalizer: RunFinalizer | None = None,
                    finalizer_v2: RunFinalizerV2 | None = None) -> AgentState | None:
    finalizer = finalizer if finalizer is not None else RunFinalizer(context.repository, context.events)
    finalizer_v2 = finalizer_v2 if finalizer_v2 is not None else RunFinalizerV2(context.repository, context.events)
    try:
        run = await context.repository.claim_run(run_id, context.resume_time)
        if run is None:
            return None
        context = replace(context, resume_time=run.resume_time)
        # Recheck ownership after dispatch; API validation is not execution authorization.
        await context.repository.get_run(run_id, run.user_id)
        if run.resume_time:
            state = Command(update={"tool_node_state_uncertain": True})
        else:
            user_configs = await context.repository.get_user_configs(run.user_id)
            session, _ = await context.repository.session_detail(run.user_id, run.session_id)
            state = {
                "user_configs": user_configs,
                "user_id": run.user_id, "project_id": run.project_id, "session_id": run.session_id,
                "run_id": run.id, "user_query": run.user_query, "model": run.model,
                "appendix": run.appendix or [],
                "messages": [], "model_input": [], "pending_tool_calls": [],
                "next_msg_seq": session.next_msg_seq, "tool_node_state_uncertain": False,
            }
        # LangGraph rejects -1; infinity disables its step cutoff without a finite substitute.
        recursion_limit = inf if context.recursion_limit == -1 else context.recursion_limit
        result = await graph.ainvoke(state, config={
            "recursion_limit": recursion_limit, "configurable": {"thread_id": run.thread_id},
        }, context=context, durability="sync" if getattr(graph, "checkpointer", None) else None)
        await finalizer_v2.finish_succeeded(run_id, context.resume_time)
    except asyncio.CancelledError:
        # User cancellation is terminalized by the manager after this worker fully exits.
        if not user_cancelled():
            await finalizer_v2.finish_execution_interrupted(run_id, context.resume_time)
        raise
    except Exception as exc:
        error = classify_run_error(exc)
        await finalizer.fail(run_id, context.resume_time, error)
        raise error from exc

    return result
