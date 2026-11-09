import asyncio
import importlib
import json
import os
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


@pytest.mark.anyio
async def test_status_requires_real_exit_record():
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


@pytest.mark.anyio
async def test_status_cleanup_waits_for_buffered_data_and_real_eof():
    status = _StatusPipe()
    # Leave the exit record in the OS pipe until after the body has finished.
    status.loop.remove_reader(status.read_fd)
    spawn = asyncio.create_task(asyncio.sleep(0))
    body = asyncio.create_task(asyncio.sleep(0))
    closing = None
    try:
        os.write(status.write_fd, b'{"exit-code":7}\n')
        await asyncio.gather(spawn, body)
        closing = asyncio.create_task(backend._close_status(spawn, body, status))
        await asyncio.sleep(0)
        assert status.write_fd is None
        assert not closing.done()
        assert not status.eof.done()

        status.loop.add_reader(status.read_fd, status._read)
        await asyncio.wait_for(closing, 1)
        assert status.exit_code() == 7
        assert status.read_fd is None
    finally:
        status.close_writer()
        status.close_reader()
        await asyncio.gather(spawn, body, *([closing] if closing else []), return_exceptions=True)


@pytest.mark.anyio
async def test_bash_schema_and_async_dispatch(tmp_path, monkeypatch):
    called = []
    expected = {"exit_code": 7, "stdout": "hi", "stderr": "", "timed_out": False,
                "stdout_truncated": False, "stderr_truncated": False}

    async def run(workspace, command, timeout):
        called.append((workspace, command, timeout, asyncio.get_running_loop()))
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
    assert called == [(str(tmp_path), "pwd", 120, asyncio.get_running_loop())]
    definition = next(item for item in tool_definitions() if item["name"] == "bash")
    assert definition["strict"]
    assert definition["parameters"]["required"] == ["command", "timeout_seconds"]
    assert definition["parameters"]["additionalProperties"] is False


@pytest.mark.anyio
async def test_timeout_keeps_partial_output_and_cancellation_propagates(tmp_path, monkeypatch):
    async def timeout(*args):
        raise SandboxError("BASH_TIMEOUT", {"stdout": "partial", "timed_out": True})

    monkeypatch.setattr(tool, "run_bash", timeout)
    args = '{"command":"sleep 5","timeout_seconds":1}'
    result = await execute_tool("bash", args, workspace=str(tmp_path))
    assert result == {"error_code": "BASH_TIMEOUT", "result": {"stdout": "partial", "timed_out": True}}

    async def cancel(*args):
        raise asyncio.CancelledError

    monkeypatch.setattr(tool, "run_bash", cancel)
    with pytest.raises(asyncio.CancelledError):
        await execute_tool("bash", args, workspace=str(tmp_path))


@pytest.mark.anyio
async def test_no_host_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "sys", SimpleNamespace(platform="darwin"))
    result = await execute_tool("bash", '{"command":"touch leaked","timeout_seconds":null}', workspace=str(tmp_path))
    assert result == {"error_code": "SANDBOX_UNAVAILABLE", "result": {}}
    assert not (tmp_path / "leaked").exists()
