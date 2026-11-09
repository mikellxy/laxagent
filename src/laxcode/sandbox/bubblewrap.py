"""Fixed bwrap profile and bounded async execution. Also runnable with --probe.

Only the standard library is used, so deployment can probe this exact source
before copying the application or installing its Python dependencies.
"""

import argparse
import asyncio
import json
import os
from pathlib import Path
import signal
import sys
import tempfile

BWRAP = "/usr/bin/bwrap"
SANDBOX_PATH = "/usr/local/go/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
OUTPUT_HALF = 16 * 1024


class SandboxError(Exception):
    def __init__(self, code: str, result: dict | None = None):
        super().__init__(code)
        self.result = result if result is not None else {}


def build_argv(workspace: str, command: str, status_fd: int) -> list[str]:
    if sys.platform != "linux" or not os.access(BWRAP, os.X_OK):
        raise SandboxError("SANDBOX_UNAVAILABLE")
    root = Path(workspace)
    try:
        if not root.is_absolute() or root.resolve(strict=True) != root or not root.is_dir():
            raise SandboxError("SANDBOX_INVALID_WORKSPACE")
    except (OSError, RuntimeError, ValueError):
        raise SandboxError("SANDBOX_INVALID_WORKSPACE") from None
    # Never let a workspace bind replace a runtime/system mount or the sandbox root.
    reserved = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", "/proc", "/dev", "/tmp")
    if root == Path("/") or any(root == Path(p) or root in Path(p).parents for p in reserved):
        raise SandboxError("SANDBOX_INVALID_WORKSPACE")
    if any(Path(p) in root.parents for p in reserved if p != "/tmp"):
        raise SandboxError("SANDBOX_INVALID_WORKSPACE")
    args = [BWRAP, "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
            "--share-net", "--disable-userns", "--cap-drop", "ALL", "--die-with-parent", "--new-session"]
    for path in ("/usr", "/bin", "/sbin", "/lib"):
        args += ["--ro-bind", path, path]
    if Path("/lib64").exists():
        args += ["--ro-bind", "/lib64", "/lib64"]
    for path in ("/etc/ld.so.cache", "/etc/nsswitch.conf", "/etc/hosts", "/etc/resolv.conf", "/etc/ssl/certs"):
        # Follow trusted system configuration symlinks without mounting their parent (/run).
        try:
            source = str(Path(path).resolve(strict=True))
        except (OSError, RuntimeError):
            raise SandboxError("SANDBOX_UNAVAILABLE") from None
        args += ["--ro-bind", source, path]
    args += ["--proc", "/proc", "--dev", "/dev", "--perms", "1777", "--tmpfs", "/tmp",
             "--bind", workspace, workspace, "--chdir", workspace, "--clearenv",
             "--setenv", "PATH", SANDBOX_PATH, "--setenv", "HOME", "/tmp",
             "--setenv", "TMPDIR", "/tmp", "--setenv", "LANG", "C.UTF-8",
             "--remount-ro", "/", "--json-status-fd", str(status_fd),
             "--", "/bin/bash", "--noprofile", "--norc", "-c", command]
    return args


class OutputBuffer:
    """Retain at most the first/last 16 KiB while draining the entire stream."""

    def __init__(self):
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0

    def feed(self, data: bytes) -> None:
        self.total += len(data)
        count = min(len(data), OUTPUT_HALF - len(self.head))
        self.head.extend(data[:count])
        self.tail.extend(data[count:])
        if len(self.tail) > OUTPUT_HALF:
            del self.tail[:-OUTPUT_HALF]

    @property
    def truncated(self) -> bool:
        return self.total > OUTPUT_HALF * 2

    def text(self) -> str:
        if not self.truncated:
            return bytes(self.head + self.tail).decode("utf-8", errors="replace")
        # Do not join unrelated byte fragments across the discarded middle.
        return (self.head.decode("utf-8", errors="replace") + "\n[... output truncated ...]\n"
                + self.tail.decode("utf-8", errors="replace"))


class _StatusPipe:
    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.read_fd, self.write_fd = os.pipe()
        os.set_blocking(self.read_fd, False)
        self.eof = self.loop.create_future()
        self.data = bytearray()
        self.overflow = False
        self.loop.add_reader(self.read_fd, self._read)

    def _read(self):
        try:
            chunk = os.read(self.read_fd, 4096)
        except BlockingIOError:
            return
        if not chunk:
            self.close_reader()
        elif len(self.data) + len(chunk) <= 16384:
            self.data.extend(chunk)
        else:
            self.overflow = True

    def close_writer(self):
        if self.write_fd is not None:
            os.close(self.write_fd)
            self.write_fd = None

    def close_reader(self):
        if self.read_fd is not None:
            self.loop.remove_reader(self.read_fd)
            os.close(self.read_fd)
            self.read_fd = None
        if not self.eof.done():
            self.eof.set_result(None)

    def exit_code(self) -> int | None:
        if self.overflow:
            return None
        try:
            decoder = json.JSONDecoder()
            pending = self.data.decode("utf-8").strip()
            records = []
            while pending:
                record, end = decoder.raw_decode(pending)
                records.append(record)
                pending = pending[end:].lstrip()
        except (ValueError, UnicodeDecodeError):
            return None
        codes = [record.get("exit-code") for record in records if isinstance(record, dict) and "exit-code" in record]
        if len(codes) == 1 and type(codes[0]) is int and 0 <= codes[0] <= 255:
            return codes[0]
        return None


async def _drain(stream: asyncio.StreamReader, output: OutputBuffer):
    while chunk := await stream.read(8192):
        output.feed(chunk)


async def _finish_cleanup(task: asyncio.Task) -> bool:
    """Finish cleanup even if cancellation is requested repeatedly; report it afterwards."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    task.result()
    return cancelled


async def run_bash(workspace: str, command: str, timeout: float = 120) -> dict:
    if sys.platform != "linux" or not os.access(BWRAP, os.X_OK):
        raise SandboxError("SANDBOX_UNAVAILABLE")
    stdout, stderr = OutputBuffer(), OutputBuffer()
    status = _StatusPipe()
    try:
        argv = build_argv(workspace, command, status.write_fd)
    except BaseException:
        status.close_writer()
        status.close_reader()
        raise
    spawn = asyncio.create_task(asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        cwd="/", pass_fds=(status.write_fd,), start_new_session=True,
    ))
    readers = []

    async def supervise():
        try:
            process = await asyncio.shield(spawn)
        finally:
            status.close_writer()
        readers.extend([asyncio.create_task(_drain(process.stdout, stdout)),
                        asyncio.create_task(_drain(process.stderr, stderr))])
        await asyncio.gather(process.wait(), *readers, status.eof)

    running = asyncio.create_task(supervise())

    async def cleanup():
        try:
            # A cancelled create_subprocess_exec can otherwise lose a live child handle.
            try:
                process = await spawn
            except OSError:
                return
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            try:
                await asyncio.wait_for(asyncio.shield(running), 1)
            except asyncio.TimeoutError:
                if process.returncode is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                await running
        finally:
            status.close_writer()
            status.close_reader()
            await asyncio.gather(running, *readers, return_exceptions=True)

    timed_out = False
    interrupted = False
    failure = False
    try:
        async with asyncio.timeout(timeout):
            await asyncio.shield(running)
    except TimeoutError:
        timed_out = True
    except asyncio.CancelledError:
        interrupted = True
    except OSError:
        failure = True
    finally:
        interrupted = await _finish_cleanup(asyncio.create_task(cleanup())) or interrupted
    if interrupted:
        raise asyncio.CancelledError
    code = status.exit_code()
    result = {"exit_code": code, "stdout": stdout.text(), "stderr": stderr.text(), "timed_out": timed_out,
              "stdout_truncated": stdout.truncated, "stderr_truncated": stderr.truncated}
    if timed_out:
        raise SandboxError("BASH_TIMEOUT", result)
    if failure or code is None:
        # Don't expose host paths from bwrap setup diagnostics to the model.
        raise SandboxError("SANDBOX_START_FAILED")
    return result


async def probe() -> None:
    # A home-based directory proves that /tmp is not accidentally masking the workspace bind.
    with tempfile.TemporaryDirectory(prefix=".laxcode-sandbox-probe-", dir=Path.home()) as workspace:
        result = await run_bash(workspace, "pwd && python3 --version && go version", timeout=10)
        if result["exit_code"] != 0:
            raise SandboxError("SANDBOX_PROBE_FAILED", result)
        print(result["stdout"], end="")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Probe the production bubblewrap sandbox as the service account.")
    parser.add_argument("--probe", required=True, action="store_true")
    parser.parse_args()
    try:
        asyncio.run(probe())
    except SandboxError as exc:
        print(f"Sandbox probe failed: {exc}. See docs/bash-sandbox.md (bwrap, tools, user namespaces, AppArmor).",
              file=sys.stderr)
        sys.exit(1)
