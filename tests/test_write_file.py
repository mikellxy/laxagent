import asyncio
from concurrent.futures import CancelledError
import importlib
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from laxagent.sandbox import backend as dispatch
from laxagent import layout
from laxagent.sandbox import bubblewrap
from laxagent.sandbox.bubblewrap import SandboxError
from laxagent.tools.executor import execute_tool
from laxagent.tools.registry import tool_definitions
from tool_helpers import tool_context

backend = importlib.import_module("laxagent.sandbox.file_tools")
tool = importlib.import_module("laxagent.tools.builtin.write_file")
REQUEST = {"operation": "write", "path": "/user/project/1/file.txt", "content": "中文🙂\r\n"}


@pytest.fixture
def context():
    return tool_context(layout.get_project_workdir("test-user", 1))


@pytest.mark.anyio
async def test_dispatch_and_current_project_candidates(context, monkeypatch):
    calls = []

    def run(tool_name, mounts, request, *, stop_event):
        calls.append((tool_name, mounts, request, threading.get_ident()))
        assert not stop_event.is_set()
        return {"error_code": None, "result": {"path": request["path"], "bytes_written": len(request["content"].encode())}}

    monkeypatch.setattr(dispatch, "run_file_tool", run)
    for content in [REQUEST["content"], ""]:
        args = {**REQUEST, "content": content}
        result = await execute_tool("write_file", json.dumps(args), context=context)
        assert result == {"error_code": None, "result": {"path": args["path"], "bytes_written": len(content.encode())}}
        assert calls[-1][:3] == ("write_file", layout.get_user_sandbox_mounts(context.user_id), args)
        assert calls[-1][3] != threading.get_ident()
    assert context.written_workspace_paths == ["file.txt"]
    for path in ["/user/project/2/other.txt", "/user/project/10/other.txt", "/user/skills/example/SKILL.md"]:
        assert (await execute_tool("write_file", json.dumps({**REQUEST, "path": path}), context=context))["error_code"] is None
    assert context.written_workspace_paths == ["file.txt"]


@pytest.mark.anyio
async def test_schema_and_validation(context, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Invalid input must not reach the sandbox")

    monkeypatch.setattr(dispatch, "run_file_tool", unexpected)
    for invalid in ["{", "[]", "null", {"path": "/file"}, {"content": "hi"},
                    {**REQUEST, "path": ""}, {**REQUEST, "content": True},
                    {**REQUEST, "path": 1}, {**REQUEST, "content": None},
                    {**REQUEST, "skill_name": None}, {**REQUEST, "operation": "unknown"},
                    {**REQUEST, "operation": "delete"}]:
        payload = invalid if isinstance(invalid, str) else json.dumps(invalid)
        assert (await execute_tool("write_file", payload, context=context))["error_code"] == "INVALID_ARGUMENTS"
    definition = next(item for item in tool_definitions() if item["name"] == "write_file")
    assert definition["parameters"]["required"] == ["operation", "path", "content"]
    assert definition["parameters"]["additionalProperties"] is False


@pytest.mark.anyio
async def test_delete_has_no_workspace_dependency_or_candidate(context, monkeypatch):
    def run(tool_name, mounts, request, *, stop_event):
        assert tool_name == "write_file"
        return {"error_code": None, "result": {"path": request["path"], "deleted": True}}

    monkeypatch.setattr(dispatch, "run_file_tool", run)
    for path in ["/user/project/1/file.txt", "/user/skills/example/SKILL.md"]:
        result = await execute_tool("write_file", json.dumps({"operation": "delete", "path": path, "content": None}), context=context)
        assert result == {"error_code": None, "result": {"path": path, "deleted": True}}
    assert context.written_workspace_paths == []


@pytest.mark.anyio
async def test_errors_do_not_record_candidates_or_fall_back(context, monkeypatch):
    def fail(*args, **kwargs):
        return {"error_code": "WRITE_FAILED", "result": {}}

    monkeypatch.setattr(dispatch, "run_file_tool", fail)
    assert (await execute_tool("write_file", json.dumps(REQUEST), context=context))["error_code"] == "WRITE_FAILED"
    monkeypatch.setattr(dispatch, "run_file_tool", backend.run_file_tool)
    monkeypatch.setattr(bubblewrap, "sys", SimpleNamespace(platform="darwin"))
    assert (await execute_tool("write_file", json.dumps(REQUEST), context=context))["error_code"] == "SANDBOX_UNAVAILABLE"
    assert context.written_workspace_paths == []
    assert not Path(context.workspace).exists()


def test_transport_preserves_large_literal_content(monkeypatch):
    # Exercise stdin serialization without spawning a process or mounting bwrap.
    literal = "中文🙂\n\"'`$(touch must-not-run)`\\\r\n" * 10000

    def inspect_request(*args, **kwargs):
        assert json.load(kwargs["stdin"]) == {**REQUEST, "content": literal}
        raise OSError("do not spawn")

    monkeypatch.setattr(backend, "build_argv", lambda *args: ["/write_file"])
    monkeypatch.setattr(backend.subprocess, "Popen", inspect_request)
    with pytest.raises(SandboxError, match="SANDBOX_START_FAILED"):
        backend.run_file_tool("write_file", (), {**REQUEST, "content": literal})
    with pytest.raises(SandboxError, match="NOT_UTF8_TEXT"):
        backend.run_file_tool("write_file", (), {**REQUEST, "content": "\ud800"})


@pytest.mark.anyio
async def test_cancelled_write_has_no_candidate(context, monkeypatch):
    def cancel(*args, **kwargs):
        raise CancelledError

    monkeypatch.setattr(dispatch, "run_file_tool", cancel)
    with pytest.raises(asyncio.CancelledError):
        await execute_tool("write_file", json.dumps(REQUEST), context=context)
    assert context.written_workspace_paths == []
