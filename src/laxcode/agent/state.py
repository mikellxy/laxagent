from typing import Literal, NotRequired, TypedDict


class Message(TypedDict):
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    appendix: NotRequired[list[dict]]
    tool_call_id: NotRequired[str]
    tool_name: NotRequired[str]
    tool_call: NotRequired[dict | list[dict]]


class AgentState(TypedDict):
    written_workspace_paths: NotRequired[list[str]]
    loaded_skills: NotRequired[dict[str, dict]]
    tool_node_state_uncertain: NotRequired[bool]
    pending_model_response: NotRequired[dict | None]
    user_configs: dict[str, str]
    workspace: str
    model_input: list[dict]
    pending_tool_calls: list[dict]
    user_query: str
    appendix: NotRequired[list[dict]]
    messages: list[Message]
    model: str
    run_id: int
    next_msg_seq: int
    user_id: str
    project_id: int
    session_id: int
