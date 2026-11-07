from contextlib import aclosing

from langgraph.runtime import Runtime

from laxcode.agent.context import AgentContext
from laxcode.agent.state import AgentState
from laxcode.models.errors import ModelError
from laxcode.models.responses import stream_response
from laxcode.runtime.events import make_event
from laxcode.tools.executor import execute_tool
from laxcode.tools.registry import tool_definitions
from laxcode.tools.results import format_result


async def call_model(state: AgentState, runtime: Runtime[AgentContext]):
    async def publish(kind, content, **extra):
        await runtime.context.events.publish(make_event(
            state["run_id"], kind, content, msg_seq=state["next_msg_seq"], **extra,
        ))

    # Closing explicitly also releases the SDK stream if publishing fails or execution is cancelled.
    async with aclosing(stream_response(
        runtime.context.client, model=state["model"], messages=state["model_input"], tools=tool_definitions(),
    )) as stream:
        async for event in stream:
            if event.type == "started":
                await publish("message_start", {})
            elif event.type == "text_delta":
                await publish("message_chunk", event.text)
            elif event.type == "reasoning_delta":
                await publish("reasoning_chunk", event.text, channel=event.channel)
            elif event.type == "refusal_delta":
                await publish("refusal_chunk", event.text)
            elif event.type == "completed":
                calls = event.tool_calls or []
                metadata = {"tool_call": calls} if calls else {}
                next_seq, _ = await runtime.context.repository.append_message(
                    user_id=state["user_id"], project_id=state["project_id"], session_id=state["session_id"],
                    role="assistant", content=event.text, expected_seq=state["next_msg_seq"],
                    **metadata,
                )
                await publish("message_end", {"text": event.text, "usage": event.usage, **metadata})
                message = {"role": "assistant", "content": event.text, **metadata}
                return {"messages": [*state["messages"], message], "next_msg_seq": next_seq,
                        "pending_tool_calls": calls,
                        "model_input": [*state["model_input"], *event.output]}
    raise ModelError("Model stream ended without response.completed")


async def tool_node(state: AgentState, runtime: Runtime[AgentContext]):
    next_seq = state["next_msg_seq"]
    messages = list(state["messages"])
    model_input = list(state["model_input"])
    for call in state["pending_tool_calls"]:
        msg_seq = next_seq
        details = {"tool_call_id": call["call_id"], "tool_name": call["name"]}
        await runtime.context.events.publish(make_event(
            state["run_id"], "tool_start", {**details, "arguments": call["arguments"]},
            role="tool", msg_seq=msg_seq,
        ))
        result = await execute_tool(call["name"], call["arguments"], workspace=state["workspace"])
        content = format_result(result)
        metadata = {**details, "tool_call": call}
        next_seq, _ = await runtime.context.repository.append_message(
            user_id=state["user_id"], project_id=state["project_id"], session_id=state["session_id"],
            role="tool", content=content, expected_seq=msg_seq, **metadata,
        )
        await runtime.context.events.publish(make_event(
            state["run_id"], "tool_end", {**details, **result}, role="tool", msg_seq=msg_seq,
            error=result["error_code"],
        ))
        messages.append({"role": "tool", "content": content, **metadata})
        model_input.append({"type": "function_call_output", "call_id": call["call_id"], "output": content})
    return {"messages": messages, "next_msg_seq": next_seq,
            "model_input": model_input, "pending_tool_calls": []}
