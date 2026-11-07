from contextlib import aclosing

import pytest

from laxcode.models.errors import ModelError
from laxcode.models.responses import ModelEvent, stream_response
from model_helpers import ModelStream, client_for, completed


@pytest.mark.anyio
async def test_protocol_events_and_authoritative_completed_text():
    final = completed("final text")
    source = ModelStream([
        {"type": "response.created", "response": {}},
        {"type": "response.created", "response": {}},
        {"type": "response.reasoning_summary_text.delta", "delta": "summary"},
        {"type": "response.reasoning_text.delta", "delta": "thinking"},
        {"type": "response.output_text.delta", "delta": "partial text"},
        {"type": "response.refusal.delta", "delta": "refusal"},
        final,
    ])
    requests = []
    messages = [{"role": "user", "content": "hi"}]
    async with client_for(source, requests) as client:
        async with aclosing(stream_response(client, model="chosen:model", messages=messages)) as stream:
            events = [event async for event in stream]
    assert events[:-1] == [
        ModelEvent("started"),
        ModelEvent("reasoning_delta", text="summary", channel="summary"),
        ModelEvent("reasoning_delta", text="thinking", channel="text"),
        ModelEvent("text_delta", text="partial text"),
        ModelEvent("refusal_delta", text="refusal"),
    ]
    assert events[-1].type == "completed"
    assert events[-1].text == "final text"
    assert events[-1].usage["total_tokens"] == 3
    assert requests == [{"input": messages, "model": "chosen:model", "stream": True}]
    assert source.closed


@pytest.mark.anyio
@pytest.mark.parametrize("upstream,incomplete", [
    ([], False),
    ([{"type": "response.incomplete", "response": {"incomplete_details": {"reason": "max_output_tokens"}}}], True),
    ([{"type": "response.incomplete", "response": {"incomplete_details": {"reason": "secret-key"}}}], True),
    ([{"type": "response.failed", "response": {"error": {"message": "secret-key"}}}], False),
    ([{"type": "response.output_text.delta", "delta": None}], False),
    ([{"type": "response.output_item.added", "item": {"type": "function_call"}}], False),
])
async def test_protocol_failures_need_no_graph_or_event_store(upstream, incomplete):
    source = ModelStream(upstream)
    async with client_for(source, []) as client:
        with pytest.raises(ModelError) as exc:
            async with aclosing(stream_response(client, model="chosen:model", messages=[])) as stream:
                async for _ in stream:
                    pass
    assert exc.value.incomplete == incomplete
    assert "secret-key" not in str(exc.value)
    assert source.closed


@pytest.mark.anyio
async def test_early_consumer_exit_closes_sdk_stream():
    source = ModelStream([{"type": "response.created", "response": {}}, completed()])
    async with client_for(source, []) as client:
        async with aclosing(stream_response(client, model="chosen:model", messages=[])) as stream:
            async for event in stream:
                assert event.type == "started"
                break
        assert source.closed


@pytest.mark.anyio
async def test_completed_calls_are_authoritative_and_keep_text_and_reasoning():
    final = completed("准备读取")
    calls = [{"type": "function_call", "call_id": f"c{i}", "name": "read_file", "arguments": "{}"}
             for i in range(2)]
    final["response"]["output"] += [{"type": "reasoning", "summary": [], "encrypted_content": "opaque"}, *calls]
    source = ModelStream([
        {"type": "response.output_item.added", "item": {"type": "function_call", "call_id": "c0", "arguments": ""}},
        {"type": "response.function_call_arguments.delta", "delta": "partial"}, final,
    ])
    async with client_for(source, []) as client:
        events = [event async for event in stream_response(client, model="chosen:model", messages=[])]
    assert events[-1].text == "准备读取"
    assert events[-1].tool_calls == [{k: call[k] for k in ("call_id", "name", "arguments")} for call in calls]
    assert events[-1].output == final["response"]["output"]


@pytest.mark.anyio
@pytest.mark.parametrize("change", [
    lambda calls: calls.append(dict(calls[0])),
    lambda calls: calls[0].pop("call_id"),
    lambda calls: calls[0].update(arguments={}),
])
async def test_invalid_completed_calls_are_rejected(change):
    final = completed()
    calls = [{"type": "function_call", "call_id": "c1", "name": "read_file", "arguments": "{}"}]
    change(calls)
    final["response"]["output"] = calls
    async with client_for(ModelStream([final]), []) as client:
        with pytest.raises(ModelError):
            async for _ in stream_response(client, model="chosen:model", messages=[]):
                pass
