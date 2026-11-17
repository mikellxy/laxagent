"""File-tool profile and structured transport using the shared sandbox mounts."""

from concurrent.futures import CancelledError
import json
import os
from pathlib import Path
import selectors
import subprocess
import tempfile
from threading import Event
import time

from laxagent.sandbox.bubblewrap import (
    OutputBuffer, SandboxError, _StatusPipe, _read_ready, _stop_process, build_mount_argv,
)

MAX_RESPONSE_BYTES = 1024 * 1024
BIN_DIR = Path(__file__).parents[1] / "tools" / "bin"


class JSONOutput:
    """Keep a complete bounded response; never splice truncated JSON."""

    def __init__(self):
        self.data = bytearray()
        self.truncated = False

    def feed(self, chunk: bytes) -> None:
        remaining = MAX_RESPONSE_BYTES - len(self.data)
        self.data.extend(chunk[:remaining])
        self.truncated |= len(chunk) > remaining


def build_argv(tool_name: str, mounts: tuple[tuple[Path, str], ...], status_fd: int) -> list[str]:
    args = build_mount_argv(mounts, invalid_mount_code=f"{tool_name.upper()}_INVALID_WORKSPACE")
    binary = BIN_DIR / tool_name
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise SandboxError(f"{tool_name.upper()}_UNAVAILABLE")
    return args + [
        "--ro-bind", str(binary), f"/{tool_name}", "--chdir", "/",
        "--clearenv", "--setenv", "HOME", "/tmp", "--setenv", "TMPDIR", "/tmp",
        "--setenv", "LANG", "C.UTF-8", "--remount-ro", "/",
        "--json-status-fd", str(status_fd), "--", f"/{tool_name}",
    ]


def decode_response(tool_name: str, output: JSONOutput, exit_code: int) -> dict:
    if exit_code != 0 or output.truncated:
        raise SandboxError(f"{tool_name.upper()}_INVALID_RESPONSE")
    try:
        return json.loads(output.data.decode("utf-8"))
    except (ValueError, UnicodeError):
        raise SandboxError(f"{tool_name.upper()}_INVALID_RESPONSE") from None


def run_file_tool(tool_name: str, mounts: tuple[tuple[Path, str], ...], request: dict,
                  *, stop_event: Event | None = None) -> dict:
    if stop_event is not None and stop_event.is_set():
        raise CancelledError
    status = _StatusPipe()
    stdout, stderr = JSONOutput(), OutputBuffer()
    process = None
    deadline = time.monotonic() + 120
    timed_out = False
    try:
        argv = build_argv(tool_name, mounts, status.write_fd)
        # An unlinked request file provides stdin without pipe backpressure or
        # shell quoting. The sandbox receives no path to this host-side file.
        with tempfile.TemporaryFile() as stdin, selectors.DefaultSelector() as selector:
            stdin.write(json.dumps(request, ensure_ascii=False).encode("utf-8"))
            stdin.seek(0)
            process = subprocess.Popen(
                argv, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}, cwd="/",
                pass_fds=(status.write_fd,), start_new_session=True,
            )
            status.close_writer()
            for fd, output in ((process.stdout.fileno(), stdout), (process.stderr.fileno(), stderr),
                               (status.read_fd, status)):
                os.set_blocking(fd, False)
                selector.register(fd, selectors.EVENT_READ, output)
            while selector.get_map() or process.poll() is None:
                if stop_event is not None and stop_event.is_set():
                    raise CancelledError
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                if stdout.truncated:
                    raise SandboxError(f"{tool_name.upper()}_INVALID_RESPONSE")
                _read_ready(selector, 0.1)
    except UnicodeEncodeError:
        raise SandboxError("NOT_UTF8_TEXT") from None
    except OSError:
        raise SandboxError("SANDBOX_START_FAILED") from None
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
    if timed_out:
        raise SandboxError(f"{tool_name.upper()}_TIMEOUT")
    code = status.exit_code()
    if code is None:
        # bwrap startup diagnostics can contain host paths; do not expose them.
        raise SandboxError("SANDBOX_START_FAILED")
    return decode_response(tool_name, stdout, code)
