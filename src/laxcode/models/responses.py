import httpx2
from openai import APIError, AsyncOpenAI

from laxcode.agent.state import AgentState, Message
from laxcode.runtime.events import EventStore, make_event


class ModelError(Exception):
    def __init__(self, message: str, *, incomplete=False):
        super().__init__(message)
        self.incomplete = incomplete


def completed_content(response: dict) -> str:
    if response.get("status") != "completed":
        raise ModelError("Invalid completed response")
    parts = []
    found_message = False
    for item in response["output"]:
        if item["type"] == "reasoning":
            continue
        if item["type"] != "message" or item["role"] != "assistant":
            raise ModelError("Unsupported model output; tool execution is not enabled")
        found_message = True
        for part in item["content"]:
            if part["type"] == "output_text":
                parts.append(part["text"])
            elif part["type"] == "refusal":
                parts.append(part["refusal"])
            else:
                raise ModelError("Unsupported assistant content")
    if not found_message:
        raise ModelError("Completed response contains no assistant message")
    return "".join(parts)


async def generate_message(state: AgentState, client: AsyncOpenAI, events: EventStore) -> Message:
    async def emit(kind, content, **extra):
        if kind.endswith("_chunk") and not isinstance(content, str):
            raise ModelError("Invalid model stream")
        await events.publish(make_event(
            state["run_id"], kind, content, msg_id=state["next_msg_id"], **extra,
        ))

    started = False
    try:
        stream = await client.responses.create(
            model=state["model"], input=state["messages"], stream=True,
        )
        async with stream:
            async for chunk in stream:
                event = chunk.model_dump(mode="json")
                kind = event["type"]
                if kind == "response.created":
                    if not started:
                        await emit("message_start", {})
                        started = True
                elif kind in ("response.output_item.added", "response.output_item.done"):
                    if event["item"]["type"] not in ("message", "reasoning"):
                        raise ModelError("Unsupported model output; tool execution is not enabled")
                elif kind == "response.output_text.delta":
                    await emit("message_chunk", event["delta"])
                elif kind in ("response.reasoning_text.delta", "response.reasoning_summary_text.delta"):
                    channel = "summary" if kind == "response.reasoning_summary_text.delta" else "text"
                    await emit("reasoning_chunk", event["delta"], channel=channel)
                elif kind == "response.refusal.delta":
                    await emit("refusal_chunk", event["delta"])
                elif kind == "response.completed":
                    response = event["response"]
                    text = completed_content(response)
                    await emit("message_end", {"text": text, "usage": response.get("usage")})
                    return {"role": "assistant", "content": text}
                elif kind == "response.incomplete":
                    details = event["response"].get("incomplete_details") or {}
                    reason = details.get("reason")
                    if reason not in ("max_output_tokens", "content_filter"):
                        reason = "unspecified"
                    raise ModelError(f"Model response incomplete: {reason}", incomplete=True)
                elif kind in ("response.failed", "error"):
                    raise ModelError("Model stream failed")
        raise ModelError("Model stream ended without response.completed")
    except (APIError, httpx2.HTTPError):
        raise ModelError("Model request failed") from None
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ModelError("Invalid model stream") from None
