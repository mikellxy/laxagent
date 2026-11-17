"""Real Docker transport; use LAXAGENT_DOCKER_TEST_IMAGE for an available Linux image."""
import asyncio
from concurrent.futures import CancelledError
import json
import os
import subprocess
from threading import Event
from uuid import uuid4

import pytest

from laxagent.sandbox.bubblewrap import SandboxError
from laxagent.sandbox.docker import DockerBackend, prepare_container
from laxagent.tools.context import ToolContext
from laxagent.tools.executor import execute_tool
from laxagent.runtime.profile import DockerEvalProfile

pytestmark = [pytest.mark.sandbox_linux, pytest.mark.anyio]


@pytest.fixture
def container():
    image = os.environ.get("LAXAGENT_DOCKER_TEST_IMAGE", "ubuntu:24.04")
    name = "laxagent-docker-test-" + uuid4().hex
    subprocess.run(["docker","run","-d","--name",name,"--user","0","--entrypoint","/bin/bash",image,
                    "-c","mkdir -p /testbed; exec sleep infinity"], check=True, capture_output=True)
    try:
        yield prepare_container(name, "/testbed")
    finally:
        subprocess.run(["docker","rm","-f",name], check=True, capture_output=True)


async def test_crud_and_tool_path_passthrough(container):
    backend = DockerBackend(container,"/testbed")
    context = ToolContext("unused",None,"/testbed",backend=backend,tool_names=DockerEvalProfile.tool_names)
    async def call(name, **args):
        result = await execute_tool(name,json.dumps(args),context=context)
        assert result["error_code"] is None, result
        return result["result"]
    await call("write_file",path="/testbed/中文.txt",operation="write",content="before\n")
    await call("edit_file",path="/testbed/中文.txt",edits=[{"old_text":"before","new_text":"after"}])
    assert (await call("read_file",path="/testbed/中文.txt",start_line_no=1,start_byte_no=1,total_lines=10))["text"]=="after\n"
    assert (await call("bash",command="pwd; cat 中文.txt",timeout_seconds=5))["stdout"]=="/testbed\nafter\n"
    assert context.written_workspace_paths == ["中文.txt"]
    await call("write_file",path="/testbed/中文.txt",operation="delete",content=None)
    assert (await call("bash",command="test ! -e 中文.txt",timeout_seconds=5))["exit_code"]==0
    # File-tool JSON must remain complete above a pipe buffer and bash output limit.
    large = "中"*50000
    await call("write_file",path="/testbed/large.txt",operation="write",content=large)
    result = await call("read_file",path="/testbed/large.txt",start_line_no=1,start_byte_no=1,total_lines=1)
    assert result["text"] == "中"*(51200//3) and not result["eof"]


async def test_timeout_cancellation_and_detached_descendants(container):
    backend = DockerBackend(container,"/testbed")
    with pytest.raises(SandboxError, match="BASH_TIMEOUT") as error:
        await asyncio.to_thread(backend.run_bash,"echo partial; sleep 30",1)
    assert "partial" in error.value.result["stdout"] and error.value.result["timed_out"]
    # A detached child escapes a process group but must still be reaped by the supervisor.
    result = await asyncio.to_thread(backend.run_bash,"setsid sleep 30 >/dev/null 2>&1 & echo $! > child.pid",5)
    assert result["exit_code"]==0
    assert (await asyncio.to_thread(backend.run_bash,"! kill -0 $(cat child.pid) 2>/dev/null",5))["exit_code"]==0
    stop = Event()
    task = asyncio.create_task(asyncio.to_thread(backend.run_bash,"echo $$ > running.pid; sleep 30",60,stop_event=stop))
    try:
        async with asyncio.timeout(10):
            while (await asyncio.to_thread(backend.run_bash,"test -f running.pid",5))["exit_code"]:
                await asyncio.sleep(.05)
        stop.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task,10)
        assert (await asyncio.to_thread(backend.run_bash,"! kill -0 $(cat running.pid) 2>/dev/null",5))["exit_code"]==0
        assert (await asyncio.to_thread(backend.run_bash,"echo still-running",5))["stdout"]=="still-running\n"
    finally:
        stop.set()
        await asyncio.gather(task,return_exceptions=True)


async def test_output_and_pre_cancel(container):
    backend = DockerBackend(container,"/testbed")
    result = await asyncio.to_thread(backend.run_bash,"head -c 100000 /dev/zero | tr '\\0' x; echo tail; echo err >&2; exit 7",5)
    assert result["exit_code"]==7 and result["stderr"]=="err\n"
    assert result["stdout_truncated"] and result["stdout"].endswith("tail\n")
    stop=Event(); stop.set()
    with pytest.raises(CancelledError):
        backend.run_bash("touch should-not-exist",5,stop_event=stop)
    assert backend.run_bash("test ! -e should-not-exist",5)["exit_code"]==0
