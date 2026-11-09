import asyncio
from concurrent.futures import CancelledError
import importlib
import json
import os
import selectors
import threading
from types import SimpleNamespace

import pytest

from laxcode.sandbox.bubblewrap import OUTPUT_HALF, OutputBuffer, SandboxError, _StatusPipe
from laxcode.tools.executor import execute_tool
from laxcode.tools.registry import tool_definitions

tool = importlib.import_module("laxcode.tools.builtin.bash")
backend = importlib.import_module("laxcode.sandbox.bubblewrap")


@pytest.mark.parametrize("size", [0, 10, OUTPUT_HALF, OUTPUT_HALF * 2, OUTPUT_HALF * 2 + 1, 1_000_000])
def test_output_is_bounded_and_preserves_head_tail(size):
    raw = bytes(index % 128 for index in range(size))
    output = OutputBuffer()
    for start in range(0, size, 777):
        output.feed(raw[start:start + 777])
    assert len(output.head) + len(output.tail) <= 2 * OUTPUT_HALF
    assert output.truncated == (size > 2 * OUTPUT_HALF)
    if not output.truncated:
        assert output.text() == raw.decode()
    else:
        assert output.text().startswith(raw[:OUTPUT_HALF].decode())
        assert output.text().endswith(raw[-OUTPUT_HALF:].decode())


def test_unicode_across_chunks_and_invalid_output():
    output = OutputBuffer()
    for byte in "中文🙂".encode():
        output.feed(bytes([byte]))
    assert output.text() == "中文🙂"
    output.feed(b"\xff")
    assert output.text().endswith("�")


def test_status_requires_real_exit_record():
    status = _StatusPipe()
    try:
        status.data.extend(b'{\n "child-pid": 123\n}\n{ "exit-code": 7 }\n')
        assert status.exit_code() == 7
        status.data[:] = b'{"child-pid": 123}'
        assert status.exit_code() is None
        status.data[:] = b'{"exit-code":true}'
        assert status.exit_code() is None
    finally:
        status.close_writer()
        status.close_reader()


def test_status_cleanup_waits_for_buffered_data_and_real_eof():
    status = _StatusPipe()
    try:
        os.write(status.write_fd, b'{"exit-code":7}\n')
        status.close_writer()
        with selectors.DefaultSelector() as selector:
            selector.register(status.read_fd, selectors.EVENT_READ, status)
            backend._read_ready(selector, 1)
            assert status.exit_code() == 7
            assert selector.get_map()  # A data read must not be mistaken for EOF.
            backend._read_ready(selector, 1)
            assert not selector.get_map()
    finally:
        status.close_writer()
        status.close_reader()


def test_cancelled_before_start_never_spawns(monkeypatch):
    stop = threading.Event()
    stop.set()

    def unexpected_start(*args):
        pytest.fail("A cancelled call must not start bwrap")

    monkeypatch.setattr(backend, "_start", unexpected_start)
    with pytest.raises(CancelledError):
        backend.run_bash("/unused", "true", stop_event=stop)


@pytest.mark.parametrize("error", [FileNotFoundError, RuntimeError])
def test_spawn_failure_closes_status_pipe(monkeypatch, error):
    pipes = []

    def status_pipe():
        status = _StatusPipe()
        pipes.append(status)
        return status

    def fail_start(*args):
        raise error("injected spawn failure")

    monkeypatch.setattr(backend, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(backend, "BWRAP", "/bin/sh")
    monkeypatch.setattr(backend, "build_argv", lambda *args: [])
    monkeypatch.setattr(backend, "_StatusPipe", status_pipe)
    monkeypatch.setattr(backend, "_start", fail_start)
    with pytest.raises(SandboxError if error is FileNotFoundError else error):
        backend.run_bash("/unused", "true")
    assert len(pipes) == 1
    assert pipes[0].read_fd is None and pipes[0].write_fd is None


@pytest.mark.anyio
async def test_bash_schema_and_async_dispatch(tmp_path, monkeypatch):
    called = []
    expected = {"exit_code": 7, "stdout": "hi", "stderr": "", "timed_out": False,
                "stdout_truncated": False, "stderr_truncated": False}

    def run(workspace, command, timeout, *, stop_event):
        called.append((workspace, command, timeout, threading.get_ident()))
        assert not stop_event.is_set()
        return expected

    monkeypatch.setattr(tool, "run_bash", run)
    for invalid in ["{", "[]", json.dumps({"command": "pwd"}),
                    json.dumps({"command": "pwd", "timeout_seconds": True}),
                    json.dumps({"command": "pwd", "timeout_seconds": 301}),
                    json.dumps({"command": "pwd", "timeout_seconds": 0}),
                    json.dumps({"command": "pwd", "timeout_seconds": "1"}),
                    json.dumps({"command": "", "timeout_seconds": None}),
                    json.dumps({"command": "a\x00b", "timeout_seconds": None}),
                    json.dumps({"command": "pwd", "timeout_seconds": None, "cwd": "/"})]:
        assert (await execute_tool("bash", invalid, workspace=str(tmp_path)))["error_code"] == "INVALID_ARGUMENTS"
    assert not called
    result = await execute_tool("bash", '{"command":"pwd","timeout_seconds":null}', workspace=str(tmp_path))
    assert result == {"error_code": None, "result": expected}
    assert len(called) == 1
    assert called[0][:3] == (str(tmp_path), "pwd", 120)
    assert called[0][3] != threading.get_ident()
    definition = next(item for item in tool_definitions() if item["name"] == "bash")
    assert definition["strict"]
    assert definition["parameters"]["required"] == ["command", "timeout_seconds"]
    assert definition["parameters"]["additionalProperties"] is False


@pytest.mark.anyio
async def test_timeout_keeps_partial_output(tmp_path, monkeypatch):
    def timeout(*args, **kwargs):
        raise SandboxError("BASH_TIMEOUT", {"stdout": "partial", "timed_out": True})

    monkeypatch.setattr(tool, "run_bash", timeout)
    args = '{"command":"sleep 5","timeout_seconds":1}'
    result = await execute_tool("bash", args, workspace=str(tmp_path))
    assert result == {"error_code": "BASH_TIMEOUT", "result": {"stdout": "partial", "timed_out": True}}


@pytest.mark.anyio
@pytest.mark.parametrize("fail_on_exit", [False, True])
async def test_cancellation_waits_for_thread_cleanup(tmp_path, monkeypatch, fail_on_exit):
    loop = asyncio.get_running_loop()
    started, stopping = asyncio.Event(), asyncio.Event()
    release, finished = threading.Event(), threading.Event()

    def run(*args, stop_event):
        loop.call_soon_threadsafe(started.set)
        try:
            assert stop_event.wait(5)
            loop.call_soon_threadsafe(stopping.set)
            assert release.wait(5)
            if fail_on_exit:
                raise SandboxError("SANDBOX_START_FAILED")
            return {}
        finally:
            finished.set()

    monkeypatch.setattr(tool, "run_bash", run)
    task = asyncio.create_task(execute_tool("bash", '{"command":"true","timeout_seconds":null}',
                                            workspace=str(tmp_path)))
    try:
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        await asyncio.wait_for(stopping.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert finished.is_set()
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.anyio
async def test_no_host_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "sys", SimpleNamespace(platform="darwin"))
    result = await execute_tool("bash", '{"command":"touch leaked","timeout_seconds":null}', workspace=str(tmp_path))
    assert result == {"error_code": "SANDBOX_UNAVAILABLE", "result": {}}
    assert not (tmp_path / "leaked").exists()
