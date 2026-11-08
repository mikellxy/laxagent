import asyncio
import copy
import json
from pathlib import Path

import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError

from laxcode.layout import get_project_workdir
from laxcode.agent.context import AgentContext
from laxcode.agent.graph import build_graph
from laxcode.models.responses import messages_to_input
from laxcode.models.errors import ModelError
from laxcode.runtime.events import EventStoreError
from storage_helpers import run_agent
from laxcode.storage.database import DatabaseError
from laxcode.storage.models import SessionMessage
from model_helpers import ModelStream, client_for, completed
from storage_helpers import message_snapshot
from test_agent import MemoryEvents, state


@pytest.fixture(autouse=True)
def workspace_file(conversation):
    workspace = get_project_workdir(conversation["user_id"], conversation["project_id"])
    workspace.mkdir(parents=True)
    (workspace / "hello.txt").write_text(Path("tests/fixtures/workspace/hello.txt").read_text(), encoding="utf-8")


def tool_response(paths=("hello.txt", "hello.txt")):
    response = completed("")
    response["response"]["output"] = [
        {"type": "reasoning", "id": "rs_test", "summary": [], "encrypted_content": "opaque"},
        *[{
            "type": "function_call", "call_id": f"call_{i}", "id": f"fc_{i}", "name": "read_file",
            "arguments": json.dumps({"path": path, "start_line_no": 1, "start_byte_no": 1, "total_lines": 1}),
        } for i, path in enumerate(paths)],
    ]
    return response


@pytest.mark.anyio
async def test_multiple_calls_one_assistant_and_sequential_tool_commits(repository, conversation):
    class CheckingEvents(MemoryEvents):
        async def publish(self, item):
            if item["type"] in ("tool_start", "tool_end"):
                next_seq, tables = await message_snapshot(repository, conversation["session_id"])
                assert tables[0] == tables[1]
                assert next_seq == item["msg_seq"] + int(item["type"] == "tool_end")
                if item["type"] == "tool_end":
                    row = tables[0][-1]
                    assert row.seq == item["msg_seq"] and row.role == "tool"
                    assert row.tool_call_id == row.tool_call["call_id"] == item["content"]["tool_call_id"]
                    assert json.loads(row.content) == {k: item["content"][k] for k in ("result", "error_code")}
            await super().publish(item)

    first = tool_response()
    streams = [ModelStream([first]), ModelStream([completed("读完了")])]
    sources = list(streams)
    captured, events = [], CheckingEvents()
    original = state(conversation)
    before = copy.deepcopy(original)
    async with client_for(streams, captured) as client:
        result = await run_agent(build_graph(), original, AgentContext(client, events, repository))
    assert before == original
    assert all(source.closed for source in sources)
    next_seq, tables = await message_snapshot(repository, conversation["session_id"])
    assert next_seq == result["next_msg_seq"] == 6
    assert tables[0] == tables[1]
    rows = tables[0]
    assert [(row.seq, row.role) for row in rows] == [
        (1, "user"), (2, "assistant"), (3, "tool"), (4, "tool"), (5, "assistant"),
    ]
    calls = rows[1].tool_call
    assert len(calls) == 2 and rows[1].tool_name is None
    assert rows[1].tool_call_id is None
    assert [row.tool_call_id for row in rows[2:4]] == [call["call_id"] for call in calls]
    assert [row.tool_call for row in rows[2:4]] == calls
    assert all(row.tool_name == "read_file" for row in rows[2:4])
    assert rows[4].content == "读完了" and rows[4].tool_call is None
    tools = [e for e in events.items if e["role"] == "tool"]
    assert [(e["type"], e["msg_seq"]) for e in tools] == [
        ("tool_start", 3), ("tool_end", 3), ("tool_start", 4), ("tool_end", 4),
    ]
    assert [e["content"]["tool_call_id"] for e in tools] == ["call_0", "call_0", "call_1", "call_1"]
    assert all("call_id" not in e["content"] for e in tools)
    assert all("tool_call_id" not in e["content"] for e in events.items if e["type"] == "message_end")
    assert all("tool_call_id" not in m for m in result["messages"] if m["role"] == "assistant")
    assert events.items[-1]["content"] == {"status": "completed"}
    assert captured[1]["input"][:2] == captured[0]["input"]
    assert captured[1]["input"][2:5] == first["response"]["output"]
    assert [item["call_id"] for item in captured[1]["input"][5:]] == ["call_0", "call_1"]
    assert all(item["type"] == "function_call_output" for item in captured[1]["input"][5:])
    assert result["pending_tool_calls"] == []
    reloaded = messages_to_input(result["messages"])
    assert [item["call_id"] for item in reloaded if item.get("type") == "function_call"] == ["call_0", "call_1"]
    assert not any("tool_call_id" in item or "tool_name" in item for item in reloaded)
    _, history = await repository.append_message(**conversation, role="user", content="继续", load_context=True)
    assert history[1]["tool_call"] == calls
    assert history[2]["tool_name"] == "read_file"
    assert messages_to_input(history) == messages_to_input(result["messages"][1:]) + [{"role": "user", "content": "继续"}]


@pytest.mark.anyio
async def test_write_edit_read_round_trip(repository, conversation):
    text = "中文🙂\r\n第二行"
    edited = "中文🙂\r\n改好了"
    path = "generated/nested/text.txt"
    response = tool_response((path, path, path))
    write_call = response["response"]["output"][1]
    write_call["name"] = "write_file"
    write_call["arguments"] = json.dumps({"path": "generated/nested/text.txt", "content": text})
    edit_call = response["response"]["output"][2]
    edit_call["name"] = "edit_file"
    edit_call["arguments"] = json.dumps({
        "path": path, "edits": [{"old_text": " 第二行  ", "new_text": "改好了"}],
    })
    read_call = response["response"]["output"][3]
    read_call["arguments"] = json.dumps({
        "path": "generated/nested/text.txt", "start_line_no": 1, "start_byte_no": 1, "total_lines": 10,
    })
    events, captured = MemoryEvents(), []
    streams = [ModelStream([response]), ModelStream([completed("写入并读取成功")])]
    async with client_for(streams, captured) as client:
        result = await run_agent(build_graph(), state(conversation), AgentContext(client, events, repository))
    workspace = get_project_workdir(conversation["user_id"], conversation["project_id"])
    assert (workspace / path).read_bytes() == edited.encode()
    assert {tool["name"] for tool in captured[0]["tools"]} == {"read_file", "write_file", "edit_file"}
    _, tables = await message_snapshot(repository, conversation["session_id"])
    assert tables[0] == tables[1]
    writes, edits, reads = tables[0][2:5]
    assert (writes.tool_name, edits.tool_name, reads.tool_name) == ("write_file", "edit_file", "read_file")
    assert json.loads(writes.content) == {
        "error_code": None, "result": {"path": "generated/nested/text.txt", "bytes_written": len(text.encode())},
    }
    assert json.loads(edits.content) == {
        "error_code": None, "result": {"path": path, "edits_applied": 1, "bytes_written": len(edited.encode())},
    }
    assert json.loads(reads.content)["result"]["text"] == edited
    assert [item["output"] for item in captured[1]["input"] if item.get("type") == "function_call_output"] == [
        writes.content, edits.content, reads.content,
    ]
    tool_events = [item for item in events.items if item["role"] == "tool"]
    assert [(item["type"], item["content"]["tool_name"]) for item in tool_events] == [
        ("tool_start", "write_file"), ("tool_end", "write_file"),
        ("tool_start", "edit_file"), ("tool_end", "edit_file"),
        ("tool_start", "read_file"), ("tool_end", "read_file"),
    ]
    assert all(item["error"] is None for item in tool_events)
    assert result["pending_tool_calls"] == []
    assert events.items[-1]["content"] == {"status": "completed"}


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["database", "event"])
@pytest.mark.parametrize("tool", ["write_file", "edit_file"])
async def test_written_file_survives_result_persistence_or_event_failure(repository, conversation, failure, tool):
    workspace = get_project_workdir(conversation["user_id"], conversation["project_id"])
    if tool == "edit_file":
        (workspace / "written.txt").write_bytes(b"old")
    response = tool_response(("written.txt", "never.txt"))
    for call in response["response"]["output"][1:]:
        path = json.loads(call["arguments"])["path"]
        call["name"] = tool
        args = {"content": "written"} if tool == "write_file" else {
            "edits": [{"old_text": "old", "new_text": "written"}],
        }
        call["arguments"] = json.dumps({"path": path, **args})

    def fail(mapper, connection, message):
        if message.role == "tool":
            raise IntegrityError("forced failure", {}, RuntimeError("test"))

    events = MemoryEvents("tool_end" if failure == "event" else None)
    if failure == "database":
        event.listen(SessionMessage, "before_insert", fail)
    try:
        async with client_for(ModelStream([response]), []) as client:
            with pytest.raises(DatabaseError if failure == "database" else EventStoreError):
                await run_agent(build_graph(), state(conversation), AgentContext(client, events, repository))
    finally:
        if failure == "database":
            event.remove(SessionMessage, "before_insert", fail)
    workspace = get_project_workdir(conversation["user_id"], conversation["project_id"])
    assert (workspace / "written.txt").read_bytes() == b"written"
    assert not (workspace / "never.txt").exists()
    assert sum(item["type"] == "tool_start" for item in events.items) == 1
    assert events.items[-1]["content"] == {"status": "failed"}


@pytest.mark.anyio
async def test_tool_error_is_persisted_and_other_calls_continue(repository, conversation):
    streams = [ModelStream([tool_response(("missing.txt", "hello.txt"))]), ModelStream([completed("ok")])]
    events, captured = MemoryEvents(), []
    async with client_for(streams, captured) as client:
        result = await run_agent(build_graph(), state(conversation), AgentContext(client, events, repository))
    assert result["next_msg_seq"] == 6
    _, tables = await message_snapshot(repository, conversation["session_id"])
    assert tables[0] == tables[1]
    assert tables[0][2].content.startswith("文件不存在")
    assert "FILE_NOT_FOUND" in captured[1]["input"][-2]["output"]
    assert "你好" in captured[1]["input"][-1]["output"]
    assert [e["error"] for e in events.items if e["type"] == "tool_end"] == ["FILE_NOT_FOUND", None]


@pytest.mark.anyio
@pytest.mark.parametrize("fail_at,expected_count", [("tool_start", 2), ("tool_end", 3)])
async def test_tool_publish_failure_stops_without_second_call(repository, conversation, fail_at, expected_count):
    events, captured = MemoryEvents(fail_at), []
    async with client_for(ModelStream([tool_response()]), captured) as client:
        with pytest.raises(EventStoreError):
            await run_agent(build_graph(), state(conversation), AgentContext(client, events, repository))
    next_seq, tables = await message_snapshot(repository, conversation["session_id"])
    assert tables[0] == tables[1] and len(tables[0]) == expected_count
    assert next_seq == expected_count + 1 and len(captured) == 1
    assert events.items[-1]["content"] == {"status": "failed"}


@pytest.mark.anyio
async def test_tool_transaction_failure_rolls_back_and_never_emits_end(repository, conversation):
    def fail(mapper, connection, message):
        if message.role == "tool":
            raise IntegrityError("forced failure", {}, RuntimeError("test"))

    event.listen(SessionMessage, "before_insert", fail)
    events = MemoryEvents()
    try:
        async with client_for(ModelStream([tool_response()]), []) as client:
            with pytest.raises(DatabaseError):
                await run_agent(build_graph(), state(conversation), AgentContext(client, events, repository))
    finally:
        event.remove(SessionMessage, "before_insert", fail)
    next_seq, tables = await message_snapshot(repository, conversation["session_id"])
    assert next_seq == 3 and len(tables[0]) == 2 and tables[0] == tables[1]
    assert not any(e["type"] == "tool_end" for e in events.items)
    assert events.items[-1]["content"] == {"status": "failed"}


@pytest.mark.anyio
async def test_cancelling_tool_does_not_persist_partial_result(monkeypatch, repository, conversation):
    entered = asyncio.Event()

    async def wait(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("laxcode.agent.nodes.loop.execute_tool", wait)
    events = MemoryEvents()
    async with client_for(ModelStream([tool_response()]), []) as client:
        task = asyncio.create_task(run_agent(build_graph(), state(conversation), AgentContext(client, events, repository)))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    next_seq, tables = await message_snapshot(repository, conversation["session_id"])
    assert next_seq == 3 and len(tables[0]) == 2 and tables[0] == tables[1]
    assert not any(e["type"] == "tool_end" for e in events.items)
    assert events.items[-1]["content"] == {"status": "cancelled"}


@pytest.mark.anyio
async def test_repeated_tool_requests_stop_at_graph_limit(repository, conversation):
    streams = []
    for index in range(25):
        response = tool_response(("hello.txt",))
        response["response"]["output"][-1]["call_id"] = f"c{index}"
        streams.append(ModelStream([response]))
    events, captured = MemoryEvents(), []
    async with client_for(streams, captured) as client:
        with pytest.raises(ModelError, match="step limit"):
            await run_agent(build_graph(), state(conversation), AgentContext(client, events, repository))
    assert len(captured) <= 13
    assert events.items[-1]["content"] == {"status": "failed"}
