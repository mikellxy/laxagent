"""Fixed bwrap profile and bounded synchronous execution. Also runnable with --probe.

Only the standard library is used, so deployment can probe this exact source
before copying the application or installing its Python dependencies.
"""

import argparse
from concurrent.futures import CancelledError
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import tempfile
from threading import Event
import time

BWRAP = "/usr/bin/bwrap"
SANDBOX_PATH = "/usr/local/go/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
OUTPUT_HALF = 16 * 1024


class SandboxError(Exception):
    def __init__(self, code: str, result: dict | None = None):
        super().__init__(code)
        self.result = result if result is not None else {}


def build_argv(workspace: str, command: str, status_fd: int, *, skill_dir: str | None = None) -> list[str]:
    if sys.platform != "linux" or not os.access(BWRAP, os.X_OK):
        raise SandboxError("SANDBOX_UNAVAILABLE")
    root = Path(workspace)
    try:
        if not root.is_absolute() or root.resolve(strict=True) != root or not root.is_dir():
            raise SandboxError("SANDBOX_INVALID_WORKSPACE")
    except (OSError, RuntimeError, ValueError):
        raise SandboxError("SANDBOX_INVALID_WORKSPACE") from None
    # Never let a workspace bind replace a runtime/system mount or the sandbox root.
    reserved = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", "/proc", "/dev", "/tmp", "/skill")
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
             "--bind", workspace, workspace]
    if skill_dir is not None:
        source = Path(skill_dir)
        try:
            if not source.is_absolute() or source.resolve(strict=True) != source or not source.is_dir():
                raise SandboxError("INVALID_SKILL_FILE")
        except (OSError, RuntimeError):
            raise SandboxError("INVALID_SKILL_FILE") from None
        args += ["--ro-bind", skill_dir, "/skill"]
    args += ["--chdir", "/skill" if skill_dir is not None else workspace, "--clearenv",
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
        self.read_fd, self.write_fd = os.pipe()
        os.set_blocking(self.read_fd, False)
        self.data = bytearray()
        self.overflow = False

    def feed(self, chunk: bytes) -> None:
        if len(self.data) + len(chunk) <= 16384:
            self.data.extend(chunk)
        else:
            self.overflow = True

    def close_writer(self):
        if self.write_fd is not None:
            os.close(self.write_fd)
            self.write_fd = None

    def close_reader(self):
        if self.read_fd is not None:
            os.close(self.read_fd)
            self.read_fd = None

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


def _start(argv: list[str], status_fd: int) -> subprocess.Popen:
    return subprocess.Popen(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        cwd="/", pass_fds=(status_fd,), start_new_session=True,
    )


def _signal_group(process: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        pass


def _stop_process(process: subprocess.Popen) -> None:
    try:
        if process.poll() is None:
            _signal_group(process, signal.SIGTERM)
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
    finally:
        # Also reap the process if signaling or the grace-period wait failed.
        if process.poll() is None:
            _signal_group(process, signal.SIGKILL)
        process.wait()


def _read_ready(selector: selectors.BaseSelector, timeout: float) -> None:
    for key, _ in selector.select(timeout):
        try:
            chunk = os.read(key.fd, 8192)
        except BlockingIOError:
            continue
        if chunk:
            key.data.feed(chunk)
        else:
            # Process exit alone is not EOF: consume buffered data before unregistering.
            selector.unregister(key.fd)


def run_bash(workspace: str, command: str, timeout: float = 120, *, stop_event: Event | None = None,
             skill_dir: str | None = None) -> dict:
    deadline = time.monotonic() + timeout
    if stop_event is not None and stop_event.is_set():
        raise CancelledError
    if sys.platform != "linux" or not os.access(BWRAP, os.X_OK):
        raise SandboxError("SANDBOX_UNAVAILABLE")
    stdout, stderr = OutputBuffer(), OutputBuffer()
    status = _StatusPipe()
    process = None
    timed_out = failure = stopping = False
    try:
        argv = build_argv(workspace, command, status.write_fd, **({"skill_dir": skill_dir} if skill_dir else {}))
        with selectors.DefaultSelector() as selector:
            process = _start(argv, status.write_fd)
            status.close_writer()
            for fd, output in ((process.stdout.fileno(), stdout), (process.stderr.fileno(), stderr),
                               (status.read_fd, status)):
                os.set_blocking(fd, False)
                selector.register(fd, selectors.EVENT_READ, output)
            while selector.get_map() or process.poll() is None:
                cancelled = stop_event is not None and stop_event.is_set()
                remaining = deadline - time.monotonic()
                if not stopping and (cancelled or remaining <= 0):
                    timed_out = not cancelled and remaining <= 0
                    stopping = True
                    _stop_process(process)
                # A finite wait lets quiet commands respond to cancellation as well.
                _read_ready(selector, 0.1 if stopping else max(0, min(0.1, remaining)))
    except OSError:
        failure = True
    finally:
        try:
            if process is not None:
                _stop_process(process)
        finally:
            status.close_writer()
            status.close_reader()
            if process is not None:
                process.stdout.close()
                process.stderr.close()
    if stop_event is not None and stop_event.is_set():
        raise CancelledError
    code = status.exit_code()
    result = {"exit_code": code, "stdout": stdout.text(), "stderr": stderr.text(), "timed_out": timed_out,
              "stdout_truncated": stdout.truncated, "stderr_truncated": stderr.truncated}
    if timed_out:
        raise SandboxError("BASH_TIMEOUT", result)
    if failure or code is None:
        # Don't expose host paths from bwrap setup diagnostics to the model.
        raise SandboxError("SANDBOX_START_FAILED")
    return result


def probe() -> None:
    # A home-based directory proves that /tmp is not accidentally masking the workspace bind.
    with tempfile.TemporaryDirectory(prefix=".laxcode-sandbox-probe-", dir=Path.home()) as workspace:
        result = run_bash(workspace, "pwd && python3 --version && go version", timeout=10)
        if result["exit_code"] != 0:
            raise SandboxError("SANDBOX_PROBE_FAILED", result)
        print(result["stdout"], end="")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Probe the production bubblewrap sandbox as the service account.")
    parser.add_argument("--probe", required=True, action="store_true")
    parser.parse_args()
    try:
        probe()
    except SandboxError as exc:
        print(f"Sandbox probe failed: {exc}. See docs/bash-sandbox.md (bwrap, tools, user namespaces, AppArmor).",
              file=sys.stderr)
        sys.exit(1)
