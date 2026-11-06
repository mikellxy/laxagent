import asyncio
import copy
import json

import httpx2
import pytest

from laxcode.agent.context import AgentContext
from laxcode.agent.graph import build_graph
from laxcode.models.errors import ModelError
from laxcode.runtime.events import EventStoreError
from laxcode.runtime.runner import run_agent
from model_helpers import ModelStream, client_for, completed


def state():
    return {
        "messages": [{"role": "system", "content": "Be helpful."}, {"role": "user", "content": "Hi"}],
        "model": "chosen:model", "run_id": 1, "next_msg_id": 0,
        "user_id": "debug", "project_id": 1, "session_id": 1,
    }


class MemoryEvents:
    def __init__(self, fail_at=None):
        self.items = []
        self.fail_at = fail_at
        self.created = False

    async def create_run(self, run_id):
        assert not self.created
        self.created = True

    async def publish(self, event):
        if event["type"] == self.fail_at:
            raise EventStoreError()
        self.items.append(event)


@pytest.mark.anyio
async def test_graph_aggregates_final_output_and_keeps_state_immutable():
    final = completed("actual final ")
    final["response"]["output"].append({"type": "message", "role": "assistant", "content": [
        {"type": "refusal", "refusal": "refusal"},
    ]})
    final["response"]["output"].insert(0, {"type": "reasoning", "summary": []})
    stream = ModelStream([
        {"type": "response.created", "response": {"id": "resp_test"}},
        {"type": "response.reasoning_text.delta", "delta": "thinking"},
        {"type": "response.reasoning_summary_text.delta", "delta": "summary"},
        {"type": "response.output_text.delta", "delta": "partial"},
        {"type": "response.output_text.done", "text": "partial"},
        {"type": "response.refusal.delta", "delta": "refusal"}, final,
    ])
    captured, events = [], MemoryEvents()
    original = state()
    snapshot = copy.deepcopy(original)
    async with client_for(stream, captured) as client:
        result = await run_agent(build_graph(), original, AgentContext(client, events))
    assert original == snapshot
    assert result["messages"] == [*snapshot["messages"], {"role": "assistant", "content": "actual final refusal"}]
    assert result["next_msg_id"] == 0
    assert captured == [{"input": original["messages"], "model": "chosen:model", "stream": True}]
    assert [e["type"] for e in events.items] == [
        "message_start", "reasoning_chunk", "reasoning_chunk", "message_chunk",
        "refusal_chunk", "message_end", "sse_end",
    ]
    assert [e["channel"] for e in events.items if e["type"] == "reasoning_chunk"] == ["text", "summary"]
    assert events.items[-2]["content"]["usage"]["total_tokens"] == 3
    assert events.items[-1]["content"] == {"status": "completed"}
    assert events.items[-1]["msg_id"] is None
    assert all(e["msg_id"] == 0 for e in events.items[:-1])
    assert stream.closed


@pytest.mark.anyio
@pytest.mark.parametrize("tail,status", [
    ([], "failed"),
    ([{"type": "error", "message": "secret-key"}], "failed"),
    ([{"type": "response.failed", "response": {"error": {"message": "secret-key"}}}], "failed"),
    ([{"type": "response.incomplete", "response": {"incomplete_details": {"reason": "max_output_tokens"}}}], "incomplete"),
    ([{"type": "response.incomplete", "response": {"incomplete_details": {"reason": "secret-key"}}}], "incomplete"),
    ([{"type": "response.output_item.added", "item": {"type": "function_call", "name": "test"}}], "failed"),
    ([{"type": "response.completed", "response": {"status": "completed", "output": []}}], "failed"),
    ([httpx2.ReadError("secret-key")], "failed"),
    ([{"type": "response.output_text.delta", "delta": None}], "failed"),
])
async def test_model_failure_never_commits_partial_message(tail, status):
    stream = ModelStream([{"type": "response.output_text.delta", "delta": "partial"}, *tail])
    events, original = MemoryEvents(), state()
    async with client_for(stream, []) as client:
        with pytest.raises(ModelError) as exc:
            await run_agent(build_graph(), original, AgentContext(client, events))
    assert len(original["messages"]) == 2
    assert not any(e["type"] == "message_end" for e in events.items)
    assert events.items[-1]["content"] == {"status": status}
    assert "secret-key" not in str(exc.value) + json.dumps(events.items)
    assert stream.closed


@pytest.mark.anyio
async def test_request_error_is_sanitized_and_not_retried():
    captured, events = [], MemoryEvents()
    async with client_for(None, captured, status=401) as client:
        with pytest.raises(ModelError, match="Model request failed"):
            await run_agent(build_graph(), state(), AgentContext(client, events))
    assert len(captured) == 1
    assert events.items[-1]["content"] == {"status": "failed"}
    assert "secret-key" not in json.dumps(events.items)


@pytest.mark.anyio
@pytest.mark.parametrize("fail_at", ["message_chunk", "message_end"])
async def test_publish_failure_closes_model_stream_without_success(fail_at):
    stream = ModelStream([{"type": "response.output_text.delta", "delta": "partial"}, completed()])
    events = MemoryEvents(fail_at=fail_at)
    async with client_for(stream, []) as client:
        with pytest.raises(EventStoreError):
            await run_agent(build_graph(), state(), AgentContext(client, events))
    assert stream.closed
    assert not any(e["type"] in ("message_end", "sse_end") for e in events.items)


@pytest.mark.anyio
async def test_cancellation_closes_stream_and_emits_cancelled():
    waiting = asyncio.Event()
    stream = ModelStream([], wait=waiting)
    events = MemoryEvents()
    async with client_for(stream, []) as client:
        task = asyncio.create_task(run_agent(build_graph(), state(), AgentContext(client, events)))
        await asyncio.wait_for(waiting.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert stream.closed
    assert events.items[-1]["content"] == {"status": "cancelled"}


@pytest.mark.anyio
async def test_cancellation_during_publish_closes_suspended_generator():
    publishing = asyncio.Event()

    class BlockingEvents(MemoryEvents):
        async def publish(self, event):
            if event["type"] == "message_chunk":
                publishing.set()
                await asyncio.Event().wait()
            await super().publish(event)

    stream = ModelStream([{"type": "response.output_text.delta", "delta": "partial"}, completed()])
    events = BlockingEvents()
    async with client_for(stream, []) as client:
        task = asyncio.create_task(run_agent(build_graph(), state(), AgentContext(client, events)))
        await asyncio.wait_for(publishing.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stream.closed
    assert events.items[-1]["content"] == {"status": "cancelled"}
