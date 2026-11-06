from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal

import httpx2
from openai import APIError, AsyncOpenAI

from laxcode.models.errors import ModelError


@dataclass(frozen=True)
class ModelEvent:
    type: Literal["started", "text_delta", "reasoning_delta", "refusal_delta", "completed"]
    text: str = ""
    channel: Literal["text", "summary"] | None = None
    usage: dict | None = None


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


async def stream_response(
    client: AsyncOpenAI, *, model: str, messages: list[dict],
) -> AsyncIterator[ModelEvent]:
    started = False
    try:
        stream = await client.responses.create(
            model=model, input=messages, stream=True,
        )
        async with stream:
            async for chunk in stream:
                event = chunk.model_dump(mode="json")
                kind = event["type"]
                if kind == "response.created":
                    if not started:
                        yield ModelEvent("started")
                        started = True
                elif kind in ("response.output_item.added", "response.output_item.done"):
                    if event["item"]["type"] not in ("message", "reasoning"):
                        raise ModelError("Unsupported model output; tool execution is not enabled")
                elif kind == "response.output_text.delta":
                    yield ModelEvent("text_delta", text=delta_text(event))
                elif kind in ("response.reasoning_text.delta", "response.reasoning_summary_text.delta"):
                    channel = "summary" if kind == "response.reasoning_summary_text.delta" else "text"
                    yield ModelEvent("reasoning_delta", text=delta_text(event), channel=channel)
                elif kind == "response.refusal.delta":
                    yield ModelEvent("refusal_delta", text=delta_text(event))
                elif kind == "response.completed":
                    response = event["response"]
                    text = completed_content(response)
                    yield ModelEvent("completed", text=text, usage=response.get("usage"))
                    return
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


def delta_text(event: dict) -> str:
    text = event["delta"]
    if not isinstance(text, str):
        raise ModelError("Invalid model stream")
    return text
