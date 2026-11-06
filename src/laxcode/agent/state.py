from typing import Literal, TypedDict


class Message(TypedDict):
    role: Literal["system", "user", "assistant", "tool"]
    content: str


class AgentState(TypedDict):
    messages: list[Message]
    model: str
    run_id: int
    next_msg_id: int
    user_id: str
    project_id: int
    session_id: int
