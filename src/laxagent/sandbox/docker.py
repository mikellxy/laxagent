"""Docker transport; paths and shell environment belong to the bound container."""
from concurrent.futures import CancelledError
from dataclasses import dataclass
import json
import subprocess
import tempfile
import time
from uuid import uuid4

from laxagent.sandbox.bubblewrap import SandboxError
from laxagent.sandbox.file_tools import BIN_DIR, JSONOutput, decode_response


def docker_command(*args, timeout=30):
    try:
        result = subprocess.run(["docker", *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        raise SandboxError("DOCKER_UNAVAILABLE") from None
    if result.returncode:
        raise SandboxError("DOCKER_COMMAND_FAILED")
    return result.stdout


def prepare_container(container_id: str, workspace: str) -> str:
    info = json.loads(docker_command("container", "inspect", container_id))[0]
    if not info["State"]["Running"] or info["State"]["Paused"]:
        raise SandboxError("DOCKER_CONTAINER_NOT_RUNNING")
    container_id = info["Id"]
    docker_command("exec", container_id, "test", "-d", workspace)
    arch = json.loads(docker_command("image", "inspect", info["Image"]))[0]["Architecture"]
    directory = BIN_DIR / arch if (BIN_DIR / arch).is_dir() else BIN_DIR
    machine = {"amd64": 62, "arm64": 183}.get(arch)
    for name in ("read_file", "write_file", "edit_file", "sandbox_exec"):
        binary = directory / name
        try:
            with binary.open("rb") as source:
                header = source.read(20)
        except OSError:
            raise SandboxError("DOCKER_TOOLS_UNAVAILABLE") from None
        if header[:4] != b"\x7fELF" or machine is None or int.from_bytes(header[18:20], "little") != machine:
            raise SandboxError("DOCKER_TOOLS_ARCH_MISMATCH")
        docker_command("cp", str(binary), f"{container_id}:/{name}")
        docker_command("exec", "-u", "0", container_id, "chmod", "755", f"/{name}")
    backend = DockerBackend(container_id, workspace)
    for name in ("read_file", "write_file", "edit_file"):
        backend.run_file_tool(name, {})  # Invalid arguments exercise the executable without changing files.
    result = backend.run_bash("true", 10)
    if result["exit_code"] != 0:
        raise SandboxError("DOCKER_COMMAND_FAILED")
    return container_id


@dataclass(frozen=True)
class DockerBackend:
    container_id: str
    workspace: str

    def _execute(self, argv, cwd, input_text, timeout, limit, stop_event):
        if stop_event is not None and stop_event.is_set():
            raise CancelledError
        token = uuid4().hex
        request = {"argv": argv, "cwd": cwd, "input": input_text, "timeout": timeout, "limit": limit}
        process = None
        cancelled = False
        try:
            with tempfile.TemporaryFile() as stdin:
                stdin.write(json.dumps(request, ensure_ascii=False).encode("utf-8"))
                stdin.seek(0)
                process = subprocess.Popen(
                    ["docker", "exec", "-i", self.container_id, "/sandbox_exec", "run", token],
                    stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                deadline = time.monotonic() + timeout + 15
                while True:
                    try:
                        stdout, _ = process.communicate(timeout=0.1)
                        break
                    except subprocess.TimeoutExpired:
                        if (stop_event is not None and stop_event.is_set()) or time.monotonic() >= deadline:
                            cancelled = stop_event is not None and stop_event.is_set()
                            # The container supervisor owns and reaps the command. Killing
                            # the local Docker client alone cannot stop container processes.
                            docker_command("exec", self.container_id, "/sandbox_exec", "cancel", token, timeout=10)
                            try:
                                stdout, _ = process.communicate(timeout=15)
                            except subprocess.TimeoutExpired:
                                raise SandboxError("DOCKER_CLEANUP_FAILED") from None
                            if not cancelled:
                                raise SandboxError("DOCKER_COMMAND_FAILED")
                            break
            if cancelled:
                raise CancelledError
            if process.returncode:
                raise SandboxError("DOCKER_COMMAND_FAILED")
            return json.loads(stdout)
        except UnicodeEncodeError:
            raise SandboxError("NOT_UTF8_TEXT") from None
        except (OSError, ValueError):
            raise SandboxError("DOCKER_COMMAND_FAILED") from None
        finally:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait()
                process.stdout.close()
                process.stderr.close()

    def run_file_tool(self, tool_name, request, *, stop_event=None):
        result = self._execute([f"/{tool_name}"], "/", json.dumps(request, ensure_ascii=False),
                               120, 1024 * 1024, stop_event)
        if result["timed_out"]:
            raise SandboxError(f"{tool_name.upper()}_TIMEOUT")
        output = JSONOutput()
        output.feed(result["stdout"].encode("utf-8"))
        output.truncated |= result["stdout_truncated"]
        return decode_response(tool_name, output, result["exit_code"])

    def run_bash(self, command, timeout, *, stop_event=None):
        result = self._execute(["/bin/bash", "-c", command], self.workspace, "", timeout, 32768, stop_event)
        result.pop("cancelled")
        if result["timed_out"]:
            raise SandboxError("BASH_TIMEOUT", result)
        return result
