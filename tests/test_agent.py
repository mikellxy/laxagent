import asyncio
import copy
import json

import httpx2
import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError

from laxagent.agent.context import AgentContext
from laxagent.agent.graph import build_graph
from laxagent.context.prompts import system_prompt
from laxagent.models.errors import ModelError
from laxagent.run_errors import RunError
from laxagent.runtime.events import EventStoreError
from storage_helpers import run_agent
from laxagent.storage.models import Message
from model_helpers import ModelStream, client_for, completed
from storage_helpers import message_snapshot
from laxagent.tools.registry import tool_definitions


def state(conversation):
    return {
        "messages": [], "user_query": "Hi",
        "model": "chosen:model", "run_id": 1, "next_msg_seq": 1,
        **conversation,
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
async def test_graph_aggregates_final_output_and_keeps_state_immutable(repository, conversation):
    class CommitCheckingEvents(MemoryEvents):
        async def publish(self, item):
            if item["type"] == "message_end":
                next_seq, rows = await message_snapshot(repository, conversation["session_id"])
                assert next_seq == 3
                assert len(rows[0]) == 2 and rows[0] == rows[1]
                assert rows[0][-1].content == item["content"]["text"]
                assert rows[0][-1].reasoning == item["content"]["reasoning"] == [
                    {"channel": "text", "text": "thinking"}, {"channel": "summary", "text": "summary"},
                ]
            await super().publish(item)

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
    captured, events = [], CommitCheckingEvents()
    original = state(conversation)
    snapshot = copy.deepcopy(original)
    async with client_for(stream, captured) as client:
        result = await run_agent(build_graph(), original, AgentContext(client, events, repository))
    assert original == snapshot
    expected_input = [{"role": "system", "content": system_prompt(result["workspace"])}, {"role": "user", "content": "Hi"}]
    assert result["messages"] == [*expected_input, {"role": "assistant", "content": "actual final refusal"}]
    assert result["next_msg_seq"] == 3
    assert captured == [{"input": expected_input, "model": "chosen:model", "stream": True, "tools": tool_definitions()}]
    assert [e["type"] for e in events.items] == [
        "message_start", "reasoning_chunk", "reasoning_chunk", "message_chunk",
        "refusal_chunk", "message_end", "sse_end",
    ]
    assert [e["channel"] for e in events.items if e["type"] == "reasoning_chunk"] == ["text", "summary"]
    assert events.items[-2]["content"]["usage"]["total_tokens"] == 3
    assert events.items[-1]["content"] == {"status": "completed"}
    assert events.items[-1]["msg_seq"] is None
    assert all(e["msg_seq"] == 2 for e in events.items[:-1])
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
async def test_model_failure_never_commits_partial_message(tail, status, repository, conversation):
    stream = ModelStream([{"type": "response.reasoning_text.delta", "delta": "unfinished thought"},
                         {"type": "response.output_text.delta", "delta": "partial"}, *tail])
    events, original = MemoryEvents(), state(conversation)
    async with client_for(stream, []) as client:
        with pytest.raises(ModelError) as exc:
            await run_agent(build_graph(), original, AgentContext(client, events, repository))
    assert original["messages"] == []
    assert not any(e["type"] == "message_end" for e in events.items)
    assert events.items[-1]["content"] == {"status": status}
    assert "secret-key" not in str(exc.value) + json.dumps(events.items)
    assert stream.closed
    next_seq, rows = await message_snapshot(repository, conversation["session_id"])
    assert next_seq == 2 and len(rows[0]) == 1 and rows[0] == rows[1]
    assert rows[0][0].role == "user" and rows[0][0].content == "Hi"


@pytest.mark.anyio
async def test_request_error_is_sanitized_and_not_retried(repository, conversation):
    captured, events = [], MemoryEvents()
    async with client_for(None, captured, status=401) as client:
        with pytest.raises(ModelError, match="Model request failed"):
            await run_agent(build_graph(), state(conversation), AgentContext(client, events, repository))
    assert len(captured) == 1
    assert events.items[-1]["content"] == {"status": "failed"}
    assert "secret-key" not in json.dumps(events.items)


@pytest.mark.anyio
@pytest.mark.parametrize("fail_at", ["message_chunk", "message_end"])
async def test_publish_failure_closes_model_stream_without_success(fail_at, repository, conversation):
    stream = ModelStream([{"type": "response.output_text.delta", "delta": "partial"}, completed()])
    events = MemoryEvents(fail_at=fail_at)
    async with client_for(stream, []) as client:
        with pytest.raises(RunError, match="Event storage unavailable"):
            await run_agent(build_graph(), state(conversation), AgentContext(client, events, repository))
    assert stream.closed
    assert not any(e["type"] == "message_end" for e in events.items)
    assert events.items[-1]["content"] == {"status": "failed"}
    next_seq, rows = await message_snapshot(repository, conversation["session_id"])
    assert next_seq == (3 if fail_at == "message_end" else 2)
    assert len(rows[0]) == next_seq - 1 and rows[0] == rows[1]


@pytest.mark.anyio
async def test_cancellation_closes_stream_and_emits_cancelled(repository, conversation):
    waiting = asyncio.Event()
    stream = ModelStream([{"type": "response.reasoning_text.delta", "delta": "unfinished thought"}], wait=waiting)
    events = MemoryEvents()
    async with client_for(stream, []) as client:
        task = asyncio.create_task(run_agent(build_graph(), state(conversation), AgentContext(client, events, repository)))
        await asyncio.wait_for(waiting.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert stream.closed
    assert events.items[-1]["content"] == {"status": "cancelled"}
    _, rows = await message_snapshot(repository, conversation["session_id"])
    assert [row.role for row in rows[0]] == ["user"]
    assert rows[0] == rows[1]


@pytest.mark.anyio
async def test_assistant_database_failure_never_publishes_message_end(repository, conversation):
    def fail_assistant(mapper, connection, message):
        if message.role == "assistant":
            raise IntegrityError("forced failure", {}, RuntimeError("test"))

    event.listen(Message, "before_insert", fail_assistant)
    stream = ModelStream([completed()])
    events = MemoryEvents()
    try:
        async with client_for(stream, []) as client:
            with pytest.raises(RunError, match="Run data operation failed"):
                await run_agent(build_graph(), state(conversation), AgentContext(client, events, repository))
    finally:
        event.remove(Message, "before_insert", fail_assistant)
    next_seq, rows = await message_snapshot(repository, conversation["session_id"])
    assert next_seq == 2 and len(rows[0]) == 1 and rows[0] == rows[1]
    assert not any(item["type"] == "message_end" for item in events.items)
    assert events.items[-1]["content"] == {"status": "failed"}
    assert stream.closed


@pytest.mark.anyio
async def test_cancellation_during_publish_closes_suspended_generator(repository, conversation):
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
        task = asyncio.create_task(run_agent(build_graph(), state(conversation), AgentContext(client, events, repository)))
        await asyncio.wait_for(publishing.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stream.closed
    assert events.items[-1]["content"] == {"status": "cancelled"}
