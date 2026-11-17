from dataclasses import replace
import json
import os
from uuid import uuid4

import pytest

from laxagent import layout, skills
from laxagent.tools.executor import execute_tool
from laxagent.tools.registry import tool_definitions
from tool_helpers import tool_context


def entry(name="example", body="Use this skill.", extra=""):
    return f"---\nname: {name}\ndescription: Test purpose\n{extra}---\n{body}\n"


@pytest.fixture
def context(tmp_path):
    return tool_context(tmp_path / "project" / "42", user_id=str(uuid4()))


def make_skill(context, name="example", **kwargs):
    root = layout.get_skills_dir(context.user_id) / name
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(entry(name, **kwargs))
    return root


async def load(context, name="example"):
    return await execute_tool("load_skill", json.dumps({"name": name}), context=context)


@pytest.mark.anyio
async def test_load_returns_content_and_sorted_absolute_paths_without_reading_resources(context, monkeypatch):
    root = make_skill(context)
    (root / "scripts").mkdir()
    (root / "scripts" / "run.py").write_text("print('hello')")
    # Listing resources does not impose the former execution snapshot's byte limit.
    with (root / "data.bin").open("wb") as file:
        file.truncate(skills.MAX_BYTES + 1)
    original_open = os.open
    opened = []

    def track_open(path, *args, **kwargs):
        opened.append(str(path))
        assert str(path) not in ("data.bin", "run.py")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(skills.os, "open", track_open)
    assert await load(context) == {"error_code": None, "result": {
        "name": "example", "content": entry(), "files": [
            "/user/skills/example/SKILL.md", "/user/skills/example/data.bin",
            "/user/skills/example/scripts/run.py",
        ],
    }}
    assert "SKILL.md" in opened
    assert not hasattr(context, "loaded_skills")
    definition = next(tool for tool in tool_definitions() if tool["name"] == "load_skill")
    assert set(definition["parameters"]["properties"]) == {"name"}


@pytest.mark.anyio
async def test_reload_reads_current_content_and_file_list(context):
    root = make_skill(context)
    (root / "old.txt").write_text("old")
    assert (await load(context))["result"]["content"] == entry()
    (root / "SKILL.md").write_text(entry(body="Updated instructions."))
    (root / "old.txt").unlink()
    (root / "new.txt").write_text("new")
    # A fresh context needs no saved load state.
    result = await load(replace(context))
    assert result == {"error_code": None, "result": {
        "name": "example", "content": entry(body="Updated instructions."),
        "files": ["/user/skills/example/SKILL.md", "/user/skills/example/new.txt"],
    }}


@pytest.mark.anyio
async def test_manual_load_and_user_isolation(context):
    make_skill(context, extra="allow_auto_use: false\n")
    assert skills.skill_catalog(context.user_id) == []
    assert (await load(context))["error_code"] is None
    other = tool_context(context.workspace, user_id=str(uuid4()))
    assert (await load(other))["error_code"] == "SKILL_NOT_FOUND"
    assert not layout.get_skills_dir(other.user_id).exists()


@pytest.mark.anyio
@pytest.mark.parametrize("name", ["../example", "/example", "x/y", "x--y"])
async def test_invalid_names(context, name):
    assert (await load(context, name))["error_code"] == "INVALID_SKILL_NAME"


@pytest.mark.anyio
@pytest.mark.parametrize("content,code", [
    ("no metadata", "INVALID_SKILL_METADATA"),
    (entry("different"), "INVALID_SKILL_METADATA"),
    (b"\xff", "INVALID_SKILL_METADATA"),
    (b"x" * (skills.MAX_SKILL_BYTES + 1), "SKILL_TOO_LARGE"),
])
async def test_invalid_entry(context, content, code):
    root = make_skill(context)
    (root / "SKILL.md").write_bytes(content.encode() if isinstance(content, str) else content)
    assert (await load(context))["error_code"] == code


@pytest.mark.anyio
async def test_missing_skill_and_entry(context):
    assert (await load(context))["error_code"] == "SKILL_NOT_FOUND"
    root = make_skill(context)
    (root / "SKILL.md").unlink()
    assert (await load(context))["error_code"] == "INVALID_SKILL_METADATA"


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
async def test_invalid_resources(context, tmp_path, kind):
    root = make_skill(context)
    outside = tmp_path / "private"
    outside.write_text("private")
    path = root / "resource"
    if kind == "symlink":
        path.symlink_to(outside)
    elif kind == "hardlink":
        os.link(outside, path)
    else:
        os.mkfifo(path)
    assert (await load(context))["error_code"] == "INVALID_SKILL_FILE"
    assert skills.skill_catalog(context.user_id) == []


@pytest.mark.anyio
async def test_file_count_limit(context, monkeypatch):
    root = make_skill(context)
    (root / "resource").touch()
    monkeypatch.setattr(skills, "MAX_FILES", 1)
    assert (await load(context))["error_code"] == "SKILL_TOO_LARGE"


@pytest.mark.anyio
async def test_tool_node_saves_result_without_loaded_skill_state(context):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from laxagent.agent.context import AgentContext
    from laxagent.agent.nodes.loop import tool_node

    make_skill(context)
    repository = SimpleNamespace(append_message=AsyncMock(return_value=(2, [])))
    events = SimpleNamespace(publish=AsyncMock())
    runtime = SimpleNamespace(context=AgentContext(client=None, events=events, repository=repository, workspace=context.workspace,
                                                         skill_dir=context.skill_dir, sandbox_skill_dir=context.sandbox_skill_dir))
    state = {
        "user_id": context.user_id, "project_id": 42, "session_id": 1, "run_id": 1,
        "workspace": context.workspace, "model": "test:model", "next_msg_seq": 1,
        "messages": [], "model_input": [], "pending_tool_calls": [{
            "name": "load_skill", "arguments": '{"name":"example"}', "call_id": "load-1",
        }],
    }
    update = await tool_node(state, runtime)
    assert "loaded_skills" not in update
    assert update["pending_tool_calls"] == []
    assert update["tool_call_history"] == ["load_skill"]
    saved = repository.append_message.call_args.kwargs["content"]
    assert json.loads(saved) == {"error_code": None, "result": {
        "name": "example", "content": entry(), "files": ["/user/skills/example/SKILL.md"],
    }}
    assert update["messages"][0]["content"] == saved
    assert update["model_input"][0]["output"] == saved
