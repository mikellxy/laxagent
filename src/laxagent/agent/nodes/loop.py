from contextlib import aclosing
from dataclasses import asdict

from langgraph.runtime import Runtime

from laxagent.agent.context import AgentContext
from laxagent.agent.state import AgentState
from laxagent.run_errors import make_run_error
from laxagent.appendices import materialize_input
from laxagent.models.responses import ModelEvent, append_model_response, append_tool_message, append_reasoning, completed_reasoning, stream_response
from laxagent.runtime.events import make_event
from laxagent.tools.executor import execute_tool
from laxagent.tools.context import ToolContext
from laxagent.tools.registry import tool_definitions
from laxagent.tools.results import format_result


WEB_SEARCH_REMINDER_THRESHOLD = 20
WEB_SEARCH_LIMIT = 30


async def call_model(state: AgentState, runtime: Runtime[AgentContext]):
    # Reaching the model means the previous tool turn has been accounted for.
    # Do not mutate the checkpoint input; persist this reset with the new response.
    state = {**state, "tool_node_state_uncertain": False}
    reasoning: list[dict] = []
    async def publish(kind, content, **extra):
        await runtime.context.events.publish(make_event(
            state["run_id"], kind, content, msg_seq=state["next_msg_seq"],
            resume_time=runtime.context.resume_time, **extra,
        ))

    wire_input = state["model_input"]
    if any(message.get("appendix") for message in wire_input):
        wire_input = await materialize_input(wire_input, state["user_id"], runtime.context.repository)
    # Closing explicitly also releases the SDK stream if publishing fails or execution is cancelled.
    async with aclosing(stream_response(
        runtime.context.client, model=state["model"], messages=wire_input,
        tools=tool_definitions(state.get("user_configs"), web_search_enabled=runtime.context.web_search is not None),
    )) as stream:
        async for event in stream:
            if event.type == "started":
                await publish("message_start", {})
            elif event.type == "text_delta":
                await publish("message_chunk", event.text)
            elif event.type == "reasoning_delta":
                append_reasoning(reasoning, event.channel or "text", event.text)
                await publish("reasoning_chunk", event.text, channel=event.channel)
            elif event.type == "refusal_delta":
                await publish("refusal_chunk", event.text)
            elif event.type == "tool_call_start":
                await publish("tool_call_start", {"tool_call_id": event.tool_call_id,
                                                  "tool_name": event.tool_name}, role="tool")
            elif event.type == "tool_call_delta":
                await publish("tool_call_delta", {"tool_call_id": event.tool_call_id,
                                                  "arguments_delta": event.text}, role="tool")
            elif event.type == "completed":
                reasoning = reasoning or completed_reasoning(event.output or [])
                return {"tool_node_state_uncertain": False,
                        "pending_model_response": {"event": asdict(event), "reasoning": reasoning}}
    raise make_run_error("model_stream_ended")


async def save_model(state: AgentState, runtime: Runtime[AgentContext]):
    """Checkpoint the complete provider output before retryable DB/event writes."""
    pending = state["pending_model_response"]
    event = ModelEvent(**pending["event"])
    reasoning = pending["reasoning"]
    calls = event.tool_calls or []
    metadata = {"tool_call": calls} if calls else {}
    next_seq, _ = await runtime.context.repository.append_message(
        user_id=state["user_id"], project_id=state["project_id"], session_id=state["session_id"],
        role="assistant", content=event.text, reasoning=reasoning, expected_seq=state["next_msg_seq"],
        replay_safe=True, **metadata,
    )
    await runtime.context.events.publish(make_event(
        state["run_id"], "message_end", {"text": event.text, "reasoning": reasoning, "usage": event.usage, **metadata},
        msg_seq=state["next_msg_seq"], resume_time=runtime.context.resume_time,
    ))
    message = {"role": "assistant", "content": event.text, **metadata}
    return {"messages": [*state["messages"], message], "next_msg_seq": next_seq,
            "pending_tool_calls": calls, "pending_model_response": None,
            "model_input": append_model_response(state["model_input"], event)}


async def tool_node(state: AgentState, runtime: Runtime[AgentContext]):
    next_seq = state["next_msg_seq"]
    messages = list(state["messages"])
    model_input = state["model_input"]
    tool_context = ToolContext(
        user_id=state["user_id"], repository=runtime.context.repository, workspace=state["workspace"],
        loaded_skills=dict(state.get("loaded_skills", {})),
        written_workspace_paths=list(state.get("written_workspace_paths", [])),
        web_search=runtime.context.web_search,
    )
    call = state["pending_tool_calls"][0]
    # Commit the history with this tool's result, never mutate the input checkpoint.
    # Resumed unknown calls consume one attempt too, without re-executing them.
    tool_call_history = [*state.get("tool_call_history", []), call["name"]]
    search_count = tool_call_history.count("web_search") if call["name"] == "web_search" else 0
    msg_seq = next_seq
    details = {"tool_call_id": call["call_id"], "tool_name": call["name"]}
    await runtime.context.events.publish(make_event(
        state["run_id"], "tool_start", {**details, "arguments": call["arguments"]},
        role="tool", msg_seq=msg_seq, resume_time=runtime.context.resume_time,
    ))
    if search_count >= WEB_SEARCH_LIMIT:
        result = {"error_code": "SEARCH_LIMIT_REACHED", "result": {
            "call_count": search_count, "limit": WEB_SEARCH_LIMIT,
        }}
    elif state.get("tool_node_state_uncertain"):
        result = {"error_code": "TOOL_STATE_UNCERTAIN", "result": {}}
    else:
        result = await execute_tool(call["name"], call["arguments"],
                                    context=tool_context, user_configs=state.get("user_configs"))
    content = format_result(result)
    if WEB_SEARCH_REMINDER_THRESHOLD <= search_count < WEB_SEARCH_LIMIT:
        content = (
            f"本轮 ReAct 循环已调用 web_search {search_count} 次（含失败及恢复时的未知调用）。"
            "次数限制仅适用于本轮执行，不是整个会话的累计限制；同一轮暂停或恢复仍沿用计数，新一轮执行重新计数。"
            f"请收敛联网搜索流程，优先基于已有来源整理答案，仅补充必要证据；"
            f"第 {WEB_SEARCH_LIMIT} 次起将停止联网搜索。\n" + content
        )
    metadata = {**details, "tool_call": call}
    next_seq, _ = await runtime.context.repository.append_message(
        user_id=state["user_id"], project_id=state["project_id"], session_id=state["session_id"],
        role="tool", content=content, expected_seq=msg_seq, **metadata,
    )
    await runtime.context.events.publish(make_event(
        state["run_id"], "tool_end", {**details, **result}, role="tool", msg_seq=msg_seq,
        error=result["error_code"], resume_time=runtime.context.resume_time,
    ))
    message = {"role": "tool", "content": content, **metadata}
    messages.append(message)
    model_input = append_tool_message(model_input, message)
    return {"messages": messages, "next_msg_seq": next_seq, "tool_call_history": tool_call_history,
            "model_input": model_input, "pending_tool_calls": state["pending_tool_calls"][1:], "loaded_skills": tool_context.loaded_skills,
            "written_workspace_paths": tool_context.written_workspace_paths}
