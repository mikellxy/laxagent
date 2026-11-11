from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal

import httpx2
from openai import APIError, AsyncOpenAI

from laxcode.run_errors import make_run_error


@dataclass(frozen=True)
class ModelEvent:
    type: Literal["started", "text_delta", "reasoning_delta", "refusal_delta", "tool_call_start", "tool_call_delta", "completed"]
    text: str = ""
    channel: Literal["text", "summary"] | None = None
    usage: dict | None = None
    tool_calls: list[dict] | None = None
    output: list[dict] | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None


def completed_content(response: dict) -> str:
    if response.get("status") != "completed":
        raise make_run_error("invalid_completed_response")
    parts = []
    found_message = False
    for item in response["output"]:
        if item["type"] == "reasoning":
            continue
        if item["type"] == "function_call":
            found_message = True
            continue
        if item["type"] != "message" or item["role"] != "assistant":
            raise make_run_error("unsupported_model_output")
        found_message = True
        for part in item["content"]:
            if part["type"] == "output_text":
                parts.append(part["text"])
            elif part["type"] == "refusal":
                parts.append(part["refusal"])
            else:
                raise make_run_error("unsupported_assistant_content")
    if not found_message:
        raise make_run_error("missing_assistant_message")
    return "".join(parts)


def completed_tool_calls(response: dict) -> list[dict]:
    calls = []
    ids = set()
    for item in response["output"]:
        if item["type"] != "function_call":
            continue
        call = {key: item[key] for key in ("call_id", "name", "arguments")}
        if (not all(isinstance(value, str) for value in call.values())
                or not call["call_id"] or not call["name"] or call["call_id"] in ids):
            raise make_run_error("invalid_tool_call")
        ids.add(call["call_id"])
        calls.append(call)
    return calls


def append_reasoning(parts: list[dict], channel: str, text: str) -> None:
    if not text:
        return
    if parts and parts[-1]["channel"] == channel:
        parts[-1]["text"] += text
    else:
        parts.append({"channel": channel, "text": text})


def completed_reasoning(output: list[dict]) -> list[dict]:
    """Read only public reasoning text, never opaque provider payloads."""
    parts: list[dict] = []
    for item in output:
        if item.get("type") != "reasoning":
            continue
        for field, channel, kind in (("content", "text", "reasoning_text"),
                                      ("summary", "summary", "summary_text")):
            for part in item.get(field) or []:
                if part.get("type") == kind and isinstance(part.get("text"), str):
                    append_reasoning(parts, channel, part["text"])
    return parts


def messages_to_input(messages: list[dict]) -> list[dict]:
    # Interrupted tool turns can leave calls without outputs. Keep completed
    # pairs and assistant text without fabricating results or rewriting history.
    result = []
    for index, message in enumerate(messages):
        if message["role"] == "tool":
            result.append({"type": "function_call_output", "call_id": message["tool_call"]["call_id"],
                           "output": message["content"]})
        else:
            if message["content"] or not message.get("tool_call"):
                result.append({"role": message["role"], "content": message["content"]})
            if message.get("tool_call"):
                completed_ids = set()
                following = index + 1
                while following < len(messages) and messages[following]["role"] == "tool":
                    completed_ids.add(messages[following]["tool_call"]["call_id"])
                    following += 1
                result.extend({"type": "function_call", **call} for call in message["tool_call"]
                              if call["call_id"] in completed_ids)
    return result


def append_model_response(model_input: list[dict], event: ModelEvent) -> list[dict]:
    if event.type != "completed" or event.output is None:
        raise make_run_error("missing_completed_model_output")
    return [*model_input, *event.output]


def append_tool_message(model_input: list[dict], message: dict) -> list[dict]:
    return [*model_input, *messages_to_input([message])]


async def stream_response(
    client: AsyncOpenAI, *, model: str, messages: list[dict],
    tools: list[dict] | None = None,
) -> AsyncIterator[ModelEvent]:
    started = False
    # Argument deltas identify the output item, not its function call ID.
    calls_by_item: dict[str, str] = {}
    try:
        stream = await client.responses.create(
            model=model, input=messages, stream=True, **({"tools": tools} if tools is not None else {}),
        )
        async with stream:
            async for chunk in stream:
                # SDK serialization warnings can echo untrusted upstream values into logs.
                event = chunk.model_dump(mode="json", exclude_none=True, warnings=False)
                kind = event["type"]
                if kind == "response.created":
                    if not started:
                        yield ModelEvent("started")
                        started = True
                elif kind in ("response.output_item.added", "response.output_item.done"):
                    if event["item"]["type"] not in ("message", "reasoning", "function_call"):
                        raise make_run_error("unsupported_model_output")
                    if kind == "response.output_item.added" and event["item"]["type"] == "function_call":
                        item = event["item"]
                        item_id, call_id, name = (item[key] for key in ("id", "call_id", "name"))
                        if not all(isinstance(value, str) and value for value in (item_id, call_id, name)):
                            raise make_run_error("invalid_model_stream")
                        if item_id in calls_by_item or call_id in calls_by_item.values():
                            raise make_run_error("invalid_model_stream")
                        calls_by_item[item_id] = call_id
                        yield ModelEvent("tool_call_start", tool_call_id=call_id, tool_name=name)
                        initial = item.get("arguments", "")
                        if not isinstance(initial, str):
                            raise make_run_error("invalid_model_stream")
                        if initial:
                            yield ModelEvent("tool_call_delta", tool_call_id=call_id, text=initial)
                elif kind == "response.function_call_arguments.delta":
                    yield ModelEvent("tool_call_delta", tool_call_id=calls_by_item[event["item_id"]],
                                     text=delta_text(event))
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
                    calls = completed_tool_calls(response)
                    yield ModelEvent("completed", text=text, usage=response.get("usage"),
                                     tool_calls=calls, output=response["output"])
                    return
                elif kind == "response.incomplete":
                    details = event["response"].get("incomplete_details") or {}
                    reason = details.get("reason")
                    if reason == "max_output_tokens":
                        raise make_run_error("model_incomplete_max_output_tokens")
                    if reason == "content_filter":
                        raise make_run_error("model_incomplete_content_filter")
                    raise make_run_error("model_incomplete_unspecified")
                elif kind in ("response.failed", "error"):
                    raise make_run_error("model_stream_failed")
        raise make_run_error("model_stream_ended")
    except (APIError, httpx2.HTTPError):
        raise make_run_error("model_request_failed") from None
    except (KeyError, TypeError, ValueError, AttributeError):
        raise make_run_error("invalid_model_stream") from None


def delta_text(event: dict) -> str:
    text = event["delta"]
    if not isinstance(text, str):
        raise make_run_error("invalid_model_stream")
    return text
