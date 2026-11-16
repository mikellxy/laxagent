from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy

from laxagent.agent.context import AgentContext
from laxagent.agent.nodes.loop import call_model, save_model, tool_node
from laxagent.agent.nodes.preparation import prepare
from laxagent.agent.state import AgentState
from laxagent.models.errors import retryable_model_error


def build_graph(checkpointer=None):
    graph = StateGraph(AgentState, context_schema=AgentContext)
    graph.add_node("prepare", prepare)
    graph.add_node("call_model", call_model, retry_policy=RetryPolicy(
        max_attempts=3, initial_interval=0.5, backoff_factor=2, jitter=True,
        retry_on=retryable_model_error,
    ))
    graph.add_node("save_model", save_model)
    graph.add_node("tool_node", tool_node)
    graph.add_edge(START, "prepare")
    graph.add_edge("prepare", "call_model")
    graph.add_edge("call_model", "save_model")
    graph.add_conditional_edges("save_model", lambda state: "tool_node" if state["pending_tool_calls"] else END)
    graph.add_conditional_edges("tool_node", lambda state: "tool_node" if state["pending_tool_calls"] else "call_model")
    return graph.compile(checkpointer=checkpointer)
