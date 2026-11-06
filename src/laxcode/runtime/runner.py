import asyncio

import anyio

from laxcode.agent.context import AgentContext
from laxcode.agent.state import AgentState
from laxcode.models.responses import ModelError
from laxcode.runtime.events import EventStoreError, make_event


async def run_agent(graph, state: AgentState, context: AgentContext) -> AgentState:
    await context.events.create_run(state["run_id"])

    async def finish_failure(error: ModelError):
        status = "incomplete" if error.incomplete else "failed"
        kind = "response_incomplete" if error.incomplete else "error"
        await context.events.publish(make_event(state["run_id"], kind, {}, error=str(error)))
        await context.events.publish(make_event(
            state["run_id"], "sse_end", {"status": status}, error=str(error),
        ))

    try:
        result = await graph.ainvoke(state, context=context)
        await context.events.publish(make_event(state["run_id"], "sse_end", {"status": "completed"}))
        return result
    except asyncio.CancelledError:
        with anyio.move_on_after(5, shield=True):
            try:
                await context.events.publish(make_event(
                    state["run_id"], "sse_end", {"status": "cancelled"}, error="Execution cancelled",
                ))
            except EventStoreError:
                pass
        raise
    except EventStoreError:
        raise
    except ModelError as exc:
        await finish_failure(exc)
        raise
    except Exception:
        error = ModelError("Agent execution failed")
        await finish_failure(error)
        raise error from None
