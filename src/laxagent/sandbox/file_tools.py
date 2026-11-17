"""Shared file-tool profile and structured stdin/stdout transport.

The legacy bash/skill profiles remain unchanged during the staged migration.
"""

from concurrent.futures import CancelledError
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile
from threading import Event
import time

from laxagent.sandbox.bubblewrap import (
    BWRAP, OutputBuffer, SandboxError, _StatusPipe, _read_ready, _stop_process,
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
    binary = BIN_DIR / tool_name
    if sys.platform != "linux" or not os.access(BWRAP, os.X_OK):
        raise SandboxError("SANDBOX_UNAVAILABLE")
    # layout supplies the mount table. Only filesystem state can
    # change between preparation and this call, so check directories, not format.
    try:
        for source, _ in mounts:
            if source.resolve(strict=True) != source or not source.is_dir():
                raise SandboxError(f"{tool_name.upper()}_INVALID_WORKSPACE")
    except (OSError, RuntimeError):
        raise SandboxError(f"{tool_name.upper()}_INVALID_WORKSPACE") from None
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise SandboxError(f"{tool_name.upper()}_UNAVAILABLE")
    args = [BWRAP, "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
            "--share-net", "--disable-userns", "--cap-drop", "ALL", "--die-with-parent", "--new-session"]
    for path in ("/usr", "/bin", "/sbin", "/lib", "/lib64"):
        if path != "/lib64" or Path(path).exists():
            args += ["--ro-bind", path, path]
    for path in ("/etc/ld.so.cache", "/etc/nsswitch.conf", "/etc/hosts", "/etc/resolv.conf", "/etc/ssl/certs"):
        try:
            source = str(Path(path).resolve(strict=True))
        except (OSError, RuntimeError):
            raise SandboxError("SANDBOX_UNAVAILABLE") from None
        args += ["--ro-bind", source, path]
    args += ["--proc", "/proc", "--dev", "/dev", "--perms", "1777", "--tmpfs", "/tmp"]
    for source, target in mounts:
        args += ["--bind", str(source), target]
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
