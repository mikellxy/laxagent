import asyncio
from concurrent.futures import CancelledError
import importlib
import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from laxagent import layout
from laxagent.sandbox import bubblewrap
from laxagent.sandbox.bubblewrap import SandboxError
from laxagent.tools.executor import execute_tool
from laxagent.tools.registry import tool_definitions
from tool_helpers import tool_context

backend = importlib.import_module("laxagent.sandbox.file_tools")
tool = importlib.import_module("laxagent.tools.builtin.read_file")
REQUEST = {"path": "/user/project/1/text.txt", "start_line_no": 1, "start_byte_no": 1, "total_lines": 1}
PAGE = {"text": "中文\n", "eof": True, "last_line_no": 1, "last_line_complete": True, "last_byte_no": 7}


@pytest.fixture
def context():
    return tool_context("/unused-by-read-file")


@pytest.mark.anyio
async def test_dispatch_uses_absolute_paths_and_worker(context, monkeypatch):
    calls = []

    def run(tool_name, mounts, request, *, stop_event):
        assert tool_name == "read_file"
        calls.append((mounts, request, threading.get_ident()))
        assert not stop_event.is_set()
        return {"error_code": None, "result": PAGE}

    monkeypatch.setattr(tool, "run_file_tool", run)
    result = await execute_tool("read_file", json.dumps(REQUEST), context=context)
    assert result == {"error_code": None, "result": PAGE}
    assert calls[0][:2] == (layout.get_user_sandbox_mounts(context.user_id), REQUEST)
    assert calls[0][2] != threading.get_ident()
    definition = next(item for item in tool_definitions() if item["name"] == "read_file")
    assert "skill_name" not in definition["parameters"]["properties"]
    for invalid in ["{", "[]", {**REQUEST, "start_byte_no": True}, {**REQUEST, "skill_name": None}]:
        payload = invalid if isinstance(invalid, str) else json.dumps(invalid)
        assert (await execute_tool("read_file", payload, context=context))["error_code"] == "INVALID_ARGUMENTS"
    assert len(calls) == 1


@pytest.mark.anyio
async def test_file_errors_and_no_host_fallback(context, monkeypatch):
    def missing(*args, **kwargs):
        return {"error_code": "FILE_NOT_FOUND", "result": {}}

    monkeypatch.setattr(tool, "run_file_tool", missing)
    assert await execute_tool("read_file", json.dumps(REQUEST), context=context) == {
        "error_code": "FILE_NOT_FOUND", "result": {},
    }
    monkeypatch.setattr(tool, "run_file_tool", backend.run_file_tool)
    monkeypatch.setattr(bubblewrap, "sys", SimpleNamespace(platform="darwin"))
    assert (await execute_tool("read_file", json.dumps(REQUEST), context=context))["error_code"] == "SANDBOX_UNAVAILABLE"


@pytest.mark.parametrize("tool_name", ["read_file", "write_file", "edit_file", "bash"])
def test_profiles_share_mounts_and_reject_invalid_sources(tmp_path, monkeypatch, tool_name):
    root = tmp_path.resolve()
    projects, skills = root / "project", root / "skills"
    projects.mkdir()
    skills.mkdir()
    binary = root / tool_name
    binary.write_bytes(b"test binary")
    binary.chmod(0o755)
    original_resolve, original_access = Path.resolve, os.access

    def resolve(path, *args, **kwargs):
        # Only the fixed Linux system files are simulated on non-Linux hosts.
        return path if str(path).startswith("/etc/") else original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(bubblewrap, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(backend, "BIN_DIR", root)
    monkeypatch.setattr(Path, "resolve", resolve)
    monkeypatch.setattr(backend.os, "access", lambda path, mode: True if path == bubblewrap.BWRAP else original_access(path, mode))
    mounts = ((projects, "/user/project"), (skills, "/user/skills"))
    def build(mounts):
        if tool_name == "bash":
            return bubblewrap.build_argv(mounts, "/user/project/42", "python3 /user/skills/demo/scripts/run.py", 17)
        return backend.build_argv(tool_name, mounts, 17)

    argv = build(mounts)
    binds = [argv[i + 1:i + 3] for i, value in enumerate(argv) if value == "--bind"]
    assert binds == [[str(projects), "/user/project"], [str(skills), "/user/skills"]]
    if tool_name == "bash":
        assert argv[argv.index("--chdir") + 1] == "/user/project/42"
        assert argv[-6:] == ["--", "/bin/bash", "--noprofile", "--norc", "-c", "python3 /user/skills/demo/scripts/run.py"]
        assert "/skill" not in argv
        assert bubblewrap.SANDBOX_PATH in argv
    else:
        assert argv[-2:] == ["--", f"/{tool_name}"]
        assert argv[argv.index("--chdir") + 1] == "/"
        assert ["--ro-bind", str(binary), f"/{tool_name}"] == argv[argv.index(str(binary)) - 1:argv.index(str(binary)) + 2]
    assert not any("appendix" in value or "diagnostics" in value for value in argv)
    linked_skills = root / "linked-skills"
    linked_skills.symlink_to(skills, target_is_directory=True)
    for invalid_source in (linked_skills, root / "missing", binary):
        code = "SANDBOX_INVALID_WORKSPACE" if tool_name == "bash" else f"{tool_name.upper()}_INVALID_WORKSPACE"
        with pytest.raises(SandboxError, match=code):
            build(((projects, "/user/project"), (invalid_source, "/user/skills")))
    if tool_name != "bash":
        monkeypatch.setattr(backend, "BIN_DIR", root / "missing")
        with pytest.raises(SandboxError, match=f"{tool_name.upper()}_UNAVAILABLE"):
            build(mounts)


def test_complete_json_larger_than_bash_output_limit():
    response = {"error_code": None, "result": {**PAGE, "text": "\x01" * (50 * 1024)}}
    raw = json.dumps(response).encode()
    assert len(raw) > 32 * 1024
    output = backend.JSONOutput()
    for start in range(0, len(raw), 777):
        output.feed(raw[start:start + 777])
    assert backend.decode_response("read_file", output, 0) == response
    output.feed(b"x" * backend.MAX_RESPONSE_BYTES)
    assert len(output.data) == backend.MAX_RESPONSE_BYTES
    with pytest.raises(SandboxError, match="READ_FILE_INVALID_RESPONSE"):
        backend.decode_response("read_file", output, 0)


@pytest.mark.parametrize("raw,code", [(b"{", 0), (b"\xff", 0),
                                      (b'{"error_code":"FILE_NOT_FOUND","result":{}}', 1)])
def test_invalid_response(raw, code):
    output = backend.JSONOutput()
    output.feed(raw)
    with pytest.raises(SandboxError, match="READ_FILE_INVALID_RESPONSE"):
        backend.decode_response("read_file", output, code)


def test_spawn_failure_and_precancel_cleanup(monkeypatch):
    pipes = []
    original_pipe = backend._StatusPipe

    def status_pipe():
        status = original_pipe()
        pipes.append(status)
        return status

    def fail_start(*args, **kwargs):
        assert json.load(kwargs["stdin"]) == REQUEST
        raise OSError("host path must not leak")

    monkeypatch.setattr(backend, "_StatusPipe", status_pipe)
    monkeypatch.setattr(backend, "build_argv", lambda *args: ["/unused"])
    monkeypatch.setattr(backend.subprocess, "Popen", fail_start)
    with pytest.raises(SandboxError, match="^SANDBOX_START_FAILED$"):
        backend.run_file_tool("read_file", (), REQUEST)
    assert pipes[0].read_fd is None and pipes[0].write_fd is None
    stop = threading.Event()
    stop.set()
    with pytest.raises(CancelledError):
        backend.run_file_tool("read_file", (), REQUEST, stop_event=stop)
    assert len(pipes) == 1


@pytest.mark.anyio
async def test_cancellation_waits_for_cleanup(context, monkeypatch):
    loop = asyncio.get_running_loop()
    started, stopping = asyncio.Event(), asyncio.Event()
    release, finished = threading.Event(), threading.Event()

    def run(*args, stop_event):
        loop.call_soon_threadsafe(started.set)
        try:
            assert stop_event.wait(5)
            loop.call_soon_threadsafe(stopping.set)
            assert release.wait(5)
            raise CancelledError
        finally:
            finished.set()

    monkeypatch.setattr(tool, "run_file_tool", run)
    task = asyncio.create_task(execute_tool("read_file", json.dumps(REQUEST), context=context))
    try:
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        await asyncio.wait_for(stopping.wait(), 2)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert finished.is_set()
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
