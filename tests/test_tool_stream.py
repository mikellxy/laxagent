import asyncio
from contextlib import suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock

import nats
import pytest

from laxagent.agent.nodes.loop import call_model
from laxagent.runtime.profile import StandardProfile
from laxagent.runtime.events import EventStore
from model_helpers import ModelStream, client_for, completed


@pytest.mark.anyio
async def test_model_completes_with_full_output_when_all_previews_are_dropped(monkeypatch):
    publish = AsyncMock(side_effect=nats.errors.TimeoutError())
    store = EventStore(SimpleNamespace(jetstream=lambda **kwargs: SimpleNamespace(publish=publish)))
    monkeypatch.setattr("laxagent.runtime.events.asyncio.sleep", AsyncMock())
    final = completed("Complete answer")
    final["response"]["output"].append({
        "type": "function_call", "call_id": "call-a", "name": "write_file", "arguments": '{"content":"final"}',
    })
    source = ModelStream([
        {"type": "response.created", "response": {}},
        {"type": "response.output_text.delta", "delta": "Complete"},
        {"type": "response.output_item.added", "item": {
            "type": "function_call", "id": "item-a", "call_id": "call-a", "name": "write_file", "arguments": "",
        }},
        {"type": "response.function_call_arguments.delta", "item_id": "item-a", "delta": "partial"},
        final,
    ])
    async with client_for(source, []) as client:
        result = await call_model(
            {"user_id": "test-user", "session_id": 40, "run_id": 49, "next_msg_seq": 8,
             "model": "chosen:model", "model_input": []},
            SimpleNamespace(context=SimpleNamespace(profile=StandardProfile(), client=client, resume_time=0, web_search=None,
                                                    diagnostic_secrets=(), events=store)),
        )
    event = result["pending_model_response"]["event"]
    assert event["text"] == "Complete answer"
    assert event["tool_calls"][0]["arguments"] == '{"content":"final"}'
    assert publish.await_count == 12
    assert source.closed


@pytest.mark.anyio
async def test_tool_progress_is_published_before_completion_without_executing_or_persisting():
    waiting = asyncio.Event()
    source = ModelStream([
        {"type": "response.output_item.added", "item": {
            "type": "function_call", "id": "item-a", "call_id": "call-a", "name": "write_file", "arguments": "",
        }},
        {"type": "response.function_call_arguments.delta", "item_id": "item-a", "delta": '{"content":"half'},
    ], wait=waiting)
    events = []

    async def publish(event):
        events.append(event)

    state = {"user_id": "test-user", "session_id": 40, "run_id": 49, "next_msg_seq": 8, "model": "chosen:model", "model_input": []}
    async with client_for(source, []) as client:
        # No repository/workspace/tool context: these progress events must be display-only.
        runtime = SimpleNamespace(context=SimpleNamespace(profile=StandardProfile(), client=client, resume_time=3, web_search=None, diagnostic_secrets=(),
                                                          events=SimpleNamespace(publish=publish)))
        task = asyncio.create_task(call_model(state, runtime))
        try:
            await asyncio.wait_for(waiting.wait(), 2)
            assert not task.done()
            assert [(e["type"], e["content"]) for e in events] == [
                ("tool_call_start", {"tool_call_id": "call-a", "tool_name": "write_file"}),
                ("tool_call_delta", {"tool_call_id": "call-a", "arguments_delta": '{"content":"half'}),
            ]
            assert all((e["run_id"], e["resume_time"], e["msg_seq"], e["role"]) == (49, 3, 8, "tool") for e in events)
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        assert source.closed


@pytest.mark.anyio
async def test_complete_response_remains_authoritative_for_tool_execution():
    final = completed("Writing")
    final["response"]["output"].append({"type": "function_call", "call_id": "call-a", "name": "write_file", "arguments": '{"content":"final"}'})
    source = ModelStream([
        {"type": "response.output_item.added", "item": {
            "type": "function_call", "id": "item-a", "call_id": "call-a", "name": "write_file", "arguments": "",
        }},
        {"type": "response.function_call_arguments.delta", "item_id": "item-a", "delta": "partial"},
        final,
    ])
    events = []

    async def publish(event):
        events.append(event)

    async with client_for(source, []) as client:
        result = await call_model({"user_id": "test-user", "session_id": 40, "run_id": 49, "next_msg_seq": 8, "model": "chosen:model", "model_input": []},
                                  SimpleNamespace(context=SimpleNamespace(profile=StandardProfile(), client=client, resume_time=0, web_search=None, diagnostic_secrets=(),
                                                                          events=SimpleNamespace(publish=publish))))
    assert result["pending_model_response"]["event"]["tool_calls"] == [
        {"call_id": "call-a", "name": "write_file", "arguments": '{"content":"final"}'},
    ]
    assert [e["type"] for e in events] == ["tool_call_start", "tool_call_delta"]
