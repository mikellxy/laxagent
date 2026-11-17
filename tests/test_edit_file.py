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
tool = importlib.import_module("laxagent.tools.builtin.edit_file")
REQUEST = {"path": "/user/project/1/file.txt", "edits": [{"old_text": "old", "new_text": "new"}]}


@pytest.fixture
def context():
    return tool_context(layout.get_project_workdir("test-user", 1))


@pytest.mark.anyio
async def test_dispatch_and_noop_candidates(context, monkeypatch):
    calls = []

    def run(tool_name, mounts, request, *, stop_event):
        calls.append((tool_name, mounts, request, threading.get_ident()))
        assert not stop_event.is_set()
        changed = request["edits"][0]["old_text"] != request["edits"][0]["new_text"]
        return {"error_code": None, "result": {"path": request["path"], "edits_applied": 1,
                                                "bytes_written": 3 if changed else 0}}

    monkeypatch.setattr(dispatch, "run_file_tool", run)
    for new_text in ["new", "old"]:
        args = {**REQUEST, "edits": [{"old_text": "old", "new_text": new_text}]}
        result = await execute_tool("edit_file", json.dumps(args), context=context)
        assert result == {"error_code": None, "result": {"path": args["path"], "edits_applied": 1,
                                                         "bytes_written": 0 if new_text == "old" else 3}}
        assert calls[-1][:3] == ("edit_file", layout.get_user_sandbox_mounts(context.user_id), args)
        assert calls[-1][3] != threading.get_ident()
    assert context.written_workspace_paths == ["file.txt"]
    for path in ["/user/project/2/other.txt", "/user/project/10/other.txt", "/user/skills/example/SKILL.md"]:
        assert (await execute_tool("edit_file", json.dumps({**REQUEST, "path": path}), context=context))["error_code"] is None
    assert context.written_workspace_paths == ["file.txt"]


@pytest.mark.anyio
async def test_schema_and_validation(context, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Invalid input must not reach the sandbox")

    monkeypatch.setattr(dispatch, "run_file_tool", unexpected)
    invalid = ["{", "[]", "null", {"path": "/file"}, {"edits": []},
               {**REQUEST, "path": ""}, {**REQUEST, "path": 1}, {**REQUEST, "edits": []},
               {**REQUEST, "skill_name": None}, {**REQUEST, "extra": 1}]
    for edit in [{"old_text": "", "new_text": "new"}, {"old_text": "old"},
                 {"old_text": "old", "new_text": True}, {"old_text": 1, "new_text": "new"},
                 {"old_text": "old", "new_text": "new", "extra": 1}]:
        invalid.append({**REQUEST, "edits": [edit]})
    for args in invalid:
        payload = args if isinstance(args, str) else json.dumps(args)
        assert (await execute_tool("edit_file", payload, context=context))["error_code"] == "INVALID_ARGUMENTS"
    definition = next(item for item in tool_definitions() if item["name"] == "edit_file")
    schema = definition["parameters"]
    assert schema["required"] == ["path", "edits"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["edits"]["minItems"] == 1
    assert schema["$defs"]["TextEdit"]["required"] == ["old_text", "new_text"]
    assert schema["$defs"]["TextEdit"]["additionalProperties"] is False


@pytest.mark.anyio
async def test_errors_do_not_record_candidates_or_fall_back(context, monkeypatch):
    def fail(*args, **kwargs):
        return {"error_code": "EDIT_NOT_FOUND", "result": {}}

    monkeypatch.setattr(dispatch, "run_file_tool", fail)
    assert await execute_tool("edit_file", json.dumps(REQUEST), context=context) == {
        "error_code": "EDIT_NOT_FOUND", "result": {},
    }
    monkeypatch.setattr(dispatch, "run_file_tool", backend.run_file_tool)
    monkeypatch.setattr(bubblewrap, "sys", SimpleNamespace(platform="darwin"))
    assert (await execute_tool("edit_file", json.dumps(REQUEST), context=context))["error_code"] == "SANDBOX_UNAVAILABLE"
    assert context.written_workspace_paths == []
    assert not Path(context.workspace).exists()


def test_transport_preserves_literal_edits(monkeypatch):
    edits = [{"old_text": "中文\r\n\"'`$(touch must-not-run)`\\" * 10000, "new_text": "🙂\n"}]

    def inspect_request(*args, **kwargs):
        assert json.load(kwargs["stdin"]) == {**REQUEST, "edits": edits}
        raise OSError("do not spawn")

    monkeypatch.setattr(backend, "build_argv", lambda *args: ["/edit_file"])
    monkeypatch.setattr(backend.subprocess, "Popen", inspect_request)
    with pytest.raises(SandboxError, match="SANDBOX_START_FAILED"):
        backend.run_file_tool("edit_file", (), {**REQUEST, "edits": edits})
    with pytest.raises(SandboxError, match="NOT_UTF8_TEXT"):
        backend.run_file_tool("edit_file", (), {**REQUEST, "edits": [{"old_text": "old", "new_text": "\ud800"}]})


@pytest.mark.anyio
async def test_cancelled_edit_has_no_candidate(context, monkeypatch):
    def cancel(*args, **kwargs):
        raise CancelledError

    monkeypatch.setattr(dispatch, "run_file_tool", cancel)
    with pytest.raises(asyncio.CancelledError):
        await execute_tool("edit_file", json.dumps(REQUEST), context=context)
    assert context.written_workspace_paths == []
