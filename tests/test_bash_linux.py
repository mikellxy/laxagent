"""Real acceptance: pytest -m sandbox_linux tests/test_bash_linux.py. Never skips missing dependencies."""

import asyncio
import json
import os
from pathlib import Path
import pwd
import shlex
import signal
import sys
import tempfile
import threading
from uuid import uuid4

import pytest

from laxcode.sandbox import bubblewrap as backend
from laxcode.tools.executor import execute_tool

pytestmark = [pytest.mark.sandbox_linux, pytest.mark.anyio]


@pytest.fixture
def area():
    assert sys.platform == "linux", "Run real sandbox acceptance in Lima/Linux"
    assert os.access(backend.BWRAP, os.X_OK), "Install bubblewrap and configure AppArmor first"
    # A home-based root proves isolation is not merely a side effect of replacing /tmp.
    with tempfile.TemporaryDirectory(prefix=".laxcode-test-", dir=pwd.getpwuid(os.getuid()).pw_dir) as directory:
        root = Path(directory)
        workspace = root / "workspace"
        workspace.mkdir()
        yield root, workspace


async def run(workspace, command, timeout=None):
    return await execute_tool("bash", json.dumps({"command": command, "timeout_seconds": timeout}),
                              workspace=str(workspace))


async def wait_file(path):
    async with asyncio.timeout(5):
        while not path.exists():
            await asyncio.sleep(0.02)


def live_processes(token):
    found = []
    for path in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            if token.encode() in path.read_bytes() and path.parent.name != str(os.getpid()):
                found.append(int(path.parent.name))
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            pass
    return found


async def assert_gone(token):
    async with asyncio.timeout(5):
        while live_processes(token):
            await asyncio.sleep(0.02)


async def test_mounts_environment_and_persistent_deliverables(area, monkeypatch):
    root, workspace = area
    outside = root / "private-config"
    outside.write_text("not mounted")
    (workspace / "link").symlink_to(outside)
    monkeypatch.setenv("LAXCODE_TEST_API_KEY", "test-only-secret")
    monkeypatch.setenv("BASH_ENV", str(outside))
    monkeypatch.setenv("PYTHONPATH", str(root))
    command = f"""
set -eu
test "$PWD" = {shlex.quote(str(workspace))}
test ! -e {shlex.quote(str(outside))}
test ! -e link
test ! -e /proc/1/root{shlex.quote(str(outside))}
test ! -e /proc/{os.getpid()}/cmdline
test ! -e /run
test -z "${{LAXCODE_TEST_API_KEY+x}}${{BASH_ENV+x}}${{PYTHONPATH+x}}"
test -c /dev/null
test -c /dev/urandom
test ! -e /dev/sda
if touch /usr/.laxcode-write-probe 2>/dev/null; then exit 91; fi
if mkdir /unapproved 2>/dev/null; then exit 92; fi
if unshare -Ur true 2>/dev/null; then exit 93; fi
printf intermediate > /tmp/laxcode-ephemeral
printf intermediate > /dev/shm/laxcode-ephemeral
python3 -c 'from pathlib import Path; Path("result.txt").write_text("中文🙂")'
python3 --version
go version
"""
    result = await run(workspace, command)
    assert result["error_code"] is None, result
    assert result["result"]["exit_code"] == 0, result
    assert (workspace / "result.txt").read_text() == "中文🙂"
    assert outside.read_text() == "not mounted"
    again = await run(workspace, "test ! -e /tmp/laxcode-ephemeral && test ! -e /dev/shm/laxcode-ephemeral")
    assert again["result"]["exit_code"] == 0
    assert not Path("/tmp/laxcode-ephemeral").exists()
    assert not Path("/dev/shm/laxcode-ephemeral").exists()


@pytest.mark.parametrize("command,code", [("exit 7", 7), ("if", 2), ("true", 0), ("printf 中文🙂", 0)])
async def test_command_exit_is_not_sandbox_failure(area, command, code):
    result = await run(area[1], command)
    assert result["error_code"] is None
    assert result["result"]["exit_code"] == code
    if "printf" in command:
        assert result["result"]["stdout"] == "中文🙂"


async def test_bounded_output_drains_both_pipes(area):
    command = "python3 - <<'PY'\nimport os\nfor _ in range(200):\n os.write(1,b'x'*8192)\n os.write(2,b'y'*8192)\nprint('END')\nPY"
    result = await run(area[1], command)
    assert result["error_code"] is None
    result = result["result"]
    assert result["stdout_truncated"] and result["stderr_truncated"]
    assert result["stdout"].endswith("END\n")
    assert len(result["stdout"]) < 33000 and len(result["stderr"]) < 33000


async def test_shared_network_dns_and_https(area):
    async def serve(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async with await asyncio.start_server(serve, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        result = await run(area[1], f"curl -fsS --max-time 10 http://127.0.0.1:{port}")
        assert result["result"]["stdout"] == "ok", result
    result = await run(area[1], "getent hosts example.com && curl -fsS --max-time 20 https://example.com > /tmp/https && test -s /tmp/https", 30)
    assert result["error_code"] is None and result["result"]["exit_code"] == 0, result


@pytest.mark.parametrize("launcher", ["", "nohup", "setsid"])
async def test_normal_exit_cleans_background_processes(area, launcher):
    token = "laxcode-" + uuid4().hex
    command = f"{launcher} bash -c 'trap \"\" TERM; touch ready; while :; do sleep 1; done' {token} >/dev/null 2>&1 &\nwhile [ ! -e ready ]; do sleep .01; done"
    result = await run(area[1], command, 5)
    assert result["error_code"] is None and result["result"]["exit_code"] == 0, result
    await assert_gone(token)


async def test_timeout_and_repeated_cancellation_clean_descendants(area):
    workspace = area[1]
    token = "laxcode-" + uuid4().hex
    command = f"printf partial; touch ready; setsid bash -c 'trap \"\" TERM; while :; do sleep 1; done' {token} & wait"
    result = await run(workspace, command, 1)
    assert result["error_code"] == "BASH_TIMEOUT", result
    assert result["result"]["timed_out"] and result["result"]["stdout"] == "partial"
    assert (workspace / "ready").exists()
    await assert_gone(token)
    (workspace / "ready").unlink()
    task = asyncio.create_task(run(workspace, command))
    await wait_file(workspace / "ready")
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    await assert_gone(token)


async def test_cancellation_during_spawn_does_not_lose_process(area, monkeypatch):
    real_start = backend._start
    loop = asyncio.get_running_loop()
    spawned, release = asyncio.Event(), threading.Event()
    token = "laxcode-" + uuid4().hex

    def delayed(*args):
        process = real_start(*args)
        loop.call_soon_threadsafe(spawned.set)
        release.wait(5)
        return process

    monkeypatch.setattr(backend, "_start", delayed)
    task = asyncio.create_task(run(area[1], f"bash -c 'sleep 60' {token}"))
    try:
        await asyncio.wait_for(spawned.wait(), 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
    await assert_gone(token)


@pytest.mark.parametrize("ignore_term", [False, True])
async def test_output_failure_waits_for_process_and_readers(area, monkeypatch, ignore_term):
    real_start, real_read = backend._start, backend._read_ready
    real_signal = backend._signal_group
    processes, selectors_used = [], []
    token = "laxcode-" + uuid4().hex

    def start(*args):
        process = real_start(*args)
        processes.append(process)
        return process

    def read_ready(selector, timeout):
        selectors_used.append(selector)
        real_read(selector, timeout)
        raise OSError("injected output read failure")

    def signal_group(process, sig):
        # Simulate ineffective SIGTERM while still delivering the real SIGKILL.
        if not ignore_term or sig != signal.SIGTERM:
            real_signal(process, sig)

    monkeypatch.setattr(backend, "_start", start)
    monkeypatch.setattr(backend, "_read_ready", read_ready)
    monkeypatch.setattr(backend, "_signal_group", signal_group)
    try:
        result = await asyncio.wait_for(run(area[1], f"printf x; bash -c 'sleep 60' {token}"), 5)
        assert result["error_code"] == "SANDBOX_START_FAILED"
        assert processes[0].returncode is not None
        if ignore_term:
            assert processes[0].returncode == -signal.SIGKILL
        assert processes[0].stdout.closed and processes[0].stderr.closed
        assert selectors_used and all(not selector.get_map() for selector in selectors_used)
        await assert_gone(token)
    finally:
        for process in processes:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()


async def test_status_is_drained_even_after_process_exit(area, monkeypatch):
    real_start, real_read = backend._start, backend._read_ready
    processes = []

    def start(*args):
        process = real_start(*args)
        processes.append(process)
        return process

    def read_after_exit(selector, timeout):
        processes[0].wait(timeout=5)
        real_read(selector, timeout)

    monkeypatch.setattr(backend, "_start", start)
    monkeypatch.setattr(backend, "_read_ready", read_after_exit)
    result = await run(area[1], "exit 7")
    assert result["error_code"] is None and result["result"]["exit_code"] == 7


async def test_parent_crash_cleans_sandbox(area):
    token = "laxcode-" + uuid4().hex
    command = f"touch ready; setsid bash -c 'trap \"\" TERM; while :; do sleep 1; done' {token} & wait"
    code = ("import asyncio,sys; from laxcode.tools.builtin.bash import BashArgs,bash; "
            "asyncio.run(bash(sys.argv[1],BashArgs(command=sys.argv[2],timeout_seconds=None)))")
    worker = await asyncio.create_subprocess_exec(sys.executable, "-c", code, str(area[1]), command,
                                                   stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    try:
        await wait_file(area[1] / "ready")
        worker.send_signal(signal.SIGKILL)
        await worker.wait()
        await assert_gone(token)
    finally:
        if worker.returncode is None:
            worker.kill()
            await worker.wait()


async def test_missing_bwrap_and_setup_failure_never_execute_command(area, monkeypatch):
    monkeypatch.setattr(backend, "BWRAP", "/missing/bwrap")
    assert (await run(area[1], "touch leaked"))["error_code"] == "SANDBOX_UNAVAILABLE"
    monkeypatch.setattr(backend, "BWRAP", "/usr/bin/bwrap")
    real_build = backend.build_argv

    def bad_mount(*args):
        argv = real_build(*args)
        argv[1:1] = ["--ro-bind", "/missing-laxcode-source", "/missing"]
        return argv

    monkeypatch.setattr(backend, "build_argv", bad_mount)
    assert (await run(area[1], "touch leaked; echo '{\"exit-code\":0}' >&2"))["error_code"] == "SANDBOX_START_FAILED"
    assert not (area[1] / "leaked").exists()
