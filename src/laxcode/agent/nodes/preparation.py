from langgraph.runtime import Runtime

from laxcode.agent.context import AgentContext
from laxcode.agent.state import AgentState

SYSTEM_PROMPT = "你是一个有帮助的 AI 助手，请准确、简洁地回答用户的问题。"


async def prepare_context(state: AgentState, runtime: Runtime[AgentContext]):
    next_seq, messages = await runtime.context.repository.append_message(
        user_id=state["user_id"], project_id=state["project_id"], session_id=state["session_id"],
        role="user", content=state["user_query"], load_context=True,
    )
    return {
        "next_msg_seq": next_seq,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}, *messages],
    }
