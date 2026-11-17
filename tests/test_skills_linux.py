"""Real file-tool and skill-script workflows in bubblewrap."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import shlex
from uuid import uuid4

import pytest

from laxagent import layout
from laxagent.tools.executor import execute_tool
from tool_helpers import tool_context

pytestmark = [pytest.mark.sandbox_linux, pytest.mark.anyio]


@pytest.fixture
def context():
    user_id = str(uuid4())
    workspace = layout.get_project_workdir(user_id, 42)
    workspace.mkdir(parents=True)
    layout.get_skills_dir(user_id).mkdir()
    return tool_context(workspace, user_id=user_id)


async def call(context, tool, **args):
    return await execute_tool(tool, json.dumps(args), context=context)


async def write(context, path, content, operation="write"):
    return await call(context, "write_file", path=path, content=content, operation=operation)


async def setup_skill(context, script):
    for path, content in {
        "SKILL.md": "---\nname: test-skill\ndescription: Exercise the sandbox\n---\nRun scripts/main.py.",
        "scripts/main.py": script, "references/data.txt": "reference",
    }.items():
        result = await write(context, "/user/skills/test-skill/" + path, content)
        assert result["error_code"] is None, result
    result = await call(context, "load_skill", name="test-skill")
    assert result["error_code"] is None, result
    assert result["result"]["files"] == ["/user/skills/test-skill/" + path for path in
                                           ["SKILL.md", "references/data.txt", "scripts/main.py"]]


async def run(context, args=(), timeout=None):
    return await call(context, "bash", command=shlex.join([
        "python3", "/user/skills/test-skill/scripts/main.py", *args]), timeout_seconds=timeout)


async def wait_file(path):
    async with asyncio.timeout(5):
        while not path.exists():
            await asyncio.sleep(.02)


async def test_skill_files_and_bash_share_paths_and_current_project(context):
    await setup_skill(context, '''import json, sys
from pathlib import Path
assert str(Path.cwd()) == '/user/project/42'
root = Path(__file__).parent.parent
assert (root / 'references/data.txt').read_text() == 'reference'
(root / 'references/data.txt').write_text('changed by bash')
Path('result.json').write_text(json.dumps(sys.argv[1:]))
print('BEFORE')
''')
    path = "/user/skills/test-skill/scripts/main.py"
    read = await call(context, "read_file", path=path, start_line_no=1, start_byte_no=1, total_lines=100)
    assert "BEFORE" in read["result"]["text"]
    edited = await call(context, "edit_file", path=path, edits=[{"old_text": "BEFORE", "new_text": "AFTER"}])
    assert edited["error_code"] is None
    args = ["$(touch hacked)", "; touch hacked", "a|b", "中文"]
    result = await run(context, args)
    assert result["error_code"] is None and result["result"]["exit_code"] == 0, result
    assert result["result"]["stdout"] == "AFTER\n"
    assert json.loads((layout.get_project_workdir(context.user_id, 42) / "result.json").read_text()) == args
    assert not (layout.get_project_workdir(context.user_id, 42) / "hacked").exists()
    assert (layout.get_skills_dir(context.user_id) / "test-skill/references/data.txt").read_text() == "changed by bash"
    assert context.written_workspace_paths == []
    # Entry edits are ordinary file operations; validation happens when loading.
    assert (await write(context, "/user/skills/test-skill/SKILL.md", "invalid"))["error_code"] is None
    assert (await call(context, "load_skill", name="test-skill"))["error_code"] == "INVALID_SKILL_METADATA"
    for _ in range(2):
        assert (await write(context, "/user/skills/test-skill/SKILL.md", None, "delete"))["error_code"] is None
    assert (await call(context, "load_skill", name="test-skill"))["error_code"] == "INVALID_SKILL_METADATA"


async def test_timeout_cancel_and_live_resources(context):
    await setup_skill(context, '''import time
from pathlib import Path
Path('started').write_text('started')
print('partial', flush=True)
while not Path('release').exists(): time.sleep(.02)
Path('result').write_text(Path('/user/skills/test-skill/references/data.txt').read_text())
''')
    workspace = layout.get_project_workdir(context.user_id, 42)
    result = await run(context, timeout=1)
    assert result["error_code"] == "BASH_TIMEOUT", result
    assert result["result"]["stdout"] == "partial\n"
    assert (workspace / "started").exists() and not (workspace / "result").exists()
    (workspace / "started").unlink()
    task = asyncio.create_task(run(context))
    try:
        await wait_file(workspace / "started")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert not (workspace / "result").exists()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    (workspace / "started").unlink()
    task = asyncio.create_task(run(context))
    try:
        await wait_file(workspace / "started")
        assert (await write(context, "/user/skills/test-skill/references/data.txt", "live"))["error_code"] is None
        (workspace / "release").touch()
        result = await asyncio.wait_for(task, 5)
        assert result["result"]["exit_code"] == 0, result
        assert (workspace / "result").read_text() == "live"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_isolation_and_file_tool_boundaries(context):
    data = layout.get_user_data_dir(context.user_id)
    for name in ("appendix", "session", "diagnostics"):
        (data / name).mkdir()
        (data / name / "private").write_text("secret")
    other = layout.get_project_workdir(str(uuid4()), 42)
    other.mkdir(parents=True)
    (other / "secret").write_text("secret")
    second = layout.get_project_workdir(context.user_id, 43)
    second.mkdir()
    result = await write(context, "/user/project/43/shared.txt", "shared")
    assert result["error_code"] is None and (second / "shared.txt").read_text() == "shared"
    assert context.written_workspace_paths == []
    result = await call(context, "bash", command="test ! -e /user/appendix && test ! -e /user/session && test ! -e /user/diagnostics && test ! -e " + shlex.quote(str(other)), timeout_seconds=None)
    assert result["result"]["exit_code"] == 0
    (layout.get_project_workdir(context.user_id, 42) / "link").symlink_to(other / "secret")
    for path in ("relative.txt", "/user/project/42/../43/shared.txt", "/user/project/42/link"):
        assert (await write(context, path, "leak"))["error_code"] is not None
    assert (other / "secret").read_text() == "secret"
    # JSON transport preserves a page larger than bash's 32 KiB output buffer.
    path = "/user/project/42/large.txt"
    text = "中文" * 8000
    assert (await write(context, path, text))["error_code"] is None
    result = await call(context, "read_file", path=path, start_line_no=1, start_byte_no=1, total_lines=1)
    assert result["result"]["text"] == text and result["result"]["eof"]
