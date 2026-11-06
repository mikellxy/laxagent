from contextlib import aclosing

from langgraph.runtime import Runtime

from laxcode.agent.context import AgentContext
from laxcode.agent.state import AgentState
from laxcode.models.errors import ModelError
from laxcode.models.responses import stream_response
from laxcode.runtime.events import make_event


async def call_model(state: AgentState, runtime: Runtime[AgentContext]):
    async def publish(kind, content, **extra):
        await runtime.context.events.publish(make_event(
            state["run_id"], kind, content, msg_id=state["next_msg_id"], **extra,
        ))

    # Closing explicitly also releases the SDK stream if publishing fails or execution is cancelled.
    async with aclosing(stream_response(
        runtime.context.client, model=state["model"], messages=state["messages"],
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
                await publish("message_end", {"text": event.text, "usage": event.usage})
                message = {"role": "assistant", "content": event.text}
                return {"messages": [*state["messages"], message]}
    raise ModelError("Model stream ended without response.completed")
