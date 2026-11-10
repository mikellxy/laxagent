"""Real Linux/bwrap skill execution; missing sandbox dependencies must fail."""

import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import pwd
import sys
import tempfile
from uuid import uuid4

import pytest

from laxcode import layout
from laxcode.sandbox.bubblewrap import BWRAP
from laxcode.tools.executor import execute_tool
from tool_helpers import tool_context

pytestmark = [pytest.mark.sandbox_linux, pytest.mark.anyio]


@pytest.fixture
def context(monkeypatch):
    assert sys.platform == "linux" and os.access(BWRAP, os.X_OK)
    with tempfile.TemporaryDirectory(prefix=".laxcode-skills-test-", dir=pwd.getpwuid(os.getuid()).pw_dir) as temporary:
        root = Path(temporary)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: root))
        workspace = root / "workspace"
        workspace.mkdir()
        (root / "secret").write_text("private")
        yield replace(tool_context(workspace), user_id=str(uuid4()))


async def call(context, tool, **args):
    return await execute_tool(tool, json.dumps(args), context=context)


async def setup_skill(context, script):
    for path, content in {
        "SKILL.md": "---\nname: test-skill\ndescription: Exercise the sandbox\n---\nRun scripts/main.py.",
        "scripts/main.py": script,
        "references/data.txt": "reference",
    }.items():
        result = await call(context, "write_file", skill_name="test-skill", path=path, content=content)
        assert result["error_code"] is None, result
    assert (await call(context, "load_skill", name="test-skill"))["error_code"] is None


async def run(context, args, timeout=None):
    return await call(context, "run_skill_scripts", name="test-skill", runner="python3",
                      script="scripts/main.py", args=args, timeout_seconds=timeout)


async def wait_file(path):
    async with asyncio.timeout(5):
        while not path.exists():
            await asyncio.sleep(.02)


async def test_skill_readonly_mount_resources_workspace_and_literal_arguments(context):
    await setup_skill(context, '''import json, os, sys
from pathlib import Path
assert str(Path.cwd()) == '/skill'
assert Path('references/data.txt').read_text() == 'reference'
for target in ['SKILL.md', 'scripts/main.py', 'new.txt']:
    try:
        Path(target).write_text('must fail')
    except OSError:
        pass
    else:
        raise AssertionError('writable skill')
assert not Path(sys.argv[2]).exists()
assert not Path(sys.argv[3]).exists()
Path(sys.argv[1]).write_text(json.dumps(sys.argv[4:]))
print('verified')
''')
    original = layout.get_skills_dir(context.user_id) / "test-skill"
    output = Path(context.workspace) / "out file.json"
    result = await run(context, ["{workspace}/out file.json", str(original), str(Path.home() / "secret"),
                                 "$(touch hacked)", "; touch hacked", "a|b", "中文"])
    assert result["error_code"] is None and result["result"]["exit_code"] == 0, result
    assert json.loads(output.read_text()) == ["$(touch hacked)", "; touch hacked", "a|b", "中文"]
    assert not (Path(context.workspace) / "hacked").exists()
    assert (original / "SKILL.md").read_text().startswith("---")


async def test_timeout_and_cancel_cleanup_leave_completed_workspace_writes(context):
    await setup_skill(context, '''import sys, time
from pathlib import Path
Path(sys.argv[1]).write_text('started')
print('partial', flush=True)
time.sleep(30)
Path(sys.argv[2]).write_text('should not finish')
''')
    workspace = Path(context.workspace)
    result = await run(context, ["{workspace}/started", "{workspace}/finished"], timeout=1)
    assert result["error_code"] == "BASH_TIMEOUT", result
    assert result["result"]["stdout"] == "partial\n"
    assert (workspace / "started").read_text() == "started" and not (workspace / "finished").exists()
    (workspace / "started").unlink()
    task = asyncio.create_task(run(context, ["{workspace}/started", "{workspace}/finished"]))
    await wait_file(workspace / "started")
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert not (workspace / "finished").exists()


async def test_running_script_reads_same_snapshot_after_user_skill_update(context):
    await setup_skill(context, '''import sys, time
from pathlib import Path
Path(sys.argv[1]).write_text('started')
while not Path(sys.argv[2]).exists(): time.sleep(.02)
Path(sys.argv[3]).write_text(Path('references/data.txt').read_text())
''')
    workspace = Path(context.workspace)
    task = asyncio.create_task(run(context, ["{workspace}/started", "{workspace}/release", "{workspace}/result"]))
    try:
        await wait_file(workspace / "started")
        changed = await call(context, "write_file", skill_name="test-skill", path="references/data.txt", content="new")
        assert changed["error_code"] is None
        (workspace / "release").touch()
        result = await asyncio.wait_for(task, 5)
        assert result["error_code"] is None and result["result"]["exit_code"] == 0, result
        assert (workspace / "result").read_text() == "reference"
        assert (await run(context, []))["error_code"] == "SKILL_CHANGED"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
