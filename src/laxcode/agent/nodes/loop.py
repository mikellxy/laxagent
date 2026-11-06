from langgraph.runtime import Runtime

from laxcode.agent.context import AgentContext
from laxcode.agent.state import AgentState
from laxcode.models.responses import generate_message


async def call_model(state: AgentState, runtime: Runtime[AgentContext]):
    message = await generate_message(state, runtime.context.client, runtime.context.events)
    return {"messages": [*state["messages"], message]}
