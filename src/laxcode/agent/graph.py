from langgraph.graph import END, START, StateGraph

from laxcode.agent.context import AgentContext
from laxcode.agent.nodes.loop import call_model
from laxcode.agent.nodes.preparation import prepare_context
from laxcode.agent.state import AgentState


def build_graph():
    graph = StateGraph(AgentState, context_schema=AgentContext)
    graph.add_node("prepare_context", prepare_context)
    graph.add_node("call_model", call_model)
    graph.add_edge(START, "prepare_context")
    graph.add_edge("prepare_context", "call_model")
    graph.add_edge("call_model", END)
    return graph.compile()
