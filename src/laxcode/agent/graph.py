from langgraph.graph import END, START, StateGraph

from laxcode.agent.context import AgentContext
from laxcode.agent.nodes.loop import call_model, tool_node
from laxcode.agent.nodes.preparation import prepare
from laxcode.agent.state import AgentState


def build_graph(checkpointer=None):
    graph = StateGraph(AgentState, context_schema=AgentContext)
    graph.add_node("prepare", prepare)
    graph.add_node("call_model", call_model)
    graph.add_node("tool_node", tool_node)
    graph.add_edge(START, "prepare")
    graph.add_edge("prepare", "call_model")
    graph.add_conditional_edges("call_model", lambda state: "tool_node" if state["pending_tool_calls"] else END)
    graph.add_edge("tool_node", "call_model")
    return graph.compile(checkpointer=checkpointer)
