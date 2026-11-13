import asyncio
from dataclasses import replace
import importlib
import json
import os
from pathlib import Path
import shlex
from uuid import uuid4

import pytest

from laxcode import layout
from laxcode.skills import builtin_snapshots, initialize_skills, parse_metadata, skill_catalog, SkillError
from laxcode.tools.builtin.skills import LoadSkillArgs, load_skill
from laxcode.tools.executor import execute_tool
from laxcode.tools.registry import tool_definitions
from laxcode.tools.results import format_result
from tool_helpers import tool_context
from test_chat import chat_api, chat_path, payload, wait_run
from model_helpers import ModelStream, client_for, completed

BUILTINS = Path(__file__).resolve().parents[1] / "config/builtin_skills"


def entry(name="example", extra="", body="Use this skill."):
    return f"---\nname: {name}\ndescription: Test purpose\n{extra}---\n{body}\n"


@pytest.fixture
def skill_context(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    context = replace(tool_context(workspace), user_id=str(uuid4()))
    initialize_skills(context.user_id, None)
    return context


async def call(context, tool, **args):
    return await execute_tool(tool, json.dumps(args), context=context)


async def write(context, path, content, name="example", **extra):
    return await call(context, "write_file", skill_name=name, path=path, content=content, **extra)


@pytest.mark.anyio
async def test_string_null_scope_rejects_without_side_effects_and_json_null_recovers(skill_context):
    context = skill_context
    root = layout.get_skills_dir(context.user_id) / "null"
    result = await call(context, "write_file", skill_name="null", path="page.html", content="new")
    assert result == {"error_code": "INVALID_FILE_SCOPE", "result": {}}
    assert '"skill_name": null' in format_result(result)
    assert not root.exists()
    assert not (Path(context.workspace) / "page.html").exists()
    assert context.written_workspace_paths == []
    # Even leftover files from earlier mistaken calls must not make this scope valid.
    root.mkdir()
    (root / "page.html").write_text("old")
    for tool, args in [
        ("read_file", {"start_line_no": 1, "start_byte_no": 1, "total_lines": 10}),
        ("write_file", {"content": "new"}),
        ("edit_file", {"edits": [{"old_text": "old", "new_text": "new"}]}),
        ("write_file", {"operation": "delete", "content": None}),
    ]:
        assert (await call(context, tool, skill_name="null", path="page.html", **args))["error_code"] == "INVALID_FILE_SCOPE"
        assert (root / "page.html").read_text() == "old"
    result = await call(context, "write_file", skill_name=None, path="page.html", content="workspace")
    assert result["error_code"] is None
    assert (Path(context.workspace) / "page.html").read_text() == "workspace"
    assert context.written_workspace_paths == ["page.html"]


@pytest.mark.parametrize("content", [
    "no frontmatter", "---\nname: example\n---", entry("different"),
    entry(extra="description: duplicate\n"), entry(extra="allow_auto_use: 'true'\n"),
    entry(extra="allow_auto_use: 1\n"), entry(extra="unknown: true\n"),
    entry().replace("Test purpose", "[]"), entry().replace("Test purpose", "null"),
    entry().replace("Test purpose", "!!python/object:bad {}"),
])
def test_invalid_metadata(content):
    with pytest.raises(SkillError, match="INVALID_SKILL_METADATA"):
        parse_metadata("example", content.encode())


def test_metadata_defaults_and_bundled_skills():
    assert parse_metadata("example", entry().encode())["allow_auto_use"] is True
    assert parse_metadata("example", entry(extra="allow_auto_use: false\n").encode())["allow_auto_use"] is False
    assert {s.metadata["name"] for s in builtin_snapshots(BUILTINS)} == {"laxcode", "skill-creator"}


@pytest.mark.anyio
async def test_seed_is_concurrent_idempotent_and_never_repairs_existing_skill(skill_context, tmp_path):
    context = skill_context
    source = tmp_path / "builtins"
    (source / "example/scripts").mkdir(parents=True)
    (source / "example/SKILL.md").write_text(entry())
    (source / "example/scripts/run.py").write_text("print('initial')")
    await asyncio.gather(*[asyncio.to_thread(initialize_skills, context.user_id, source) for _ in range(4)])
    root = layout.get_skills_dir(context.user_id)
    assert sorted(p.name for p in root.iterdir()) == ["example"]
    (root / "example/scripts/run.py").unlink()
    (root / "example/SKILL.md").write_text("user-invalid-but-preserved")
    initialize_skills(context.user_id, source)
    assert (root / "example/SKILL.md").read_text() == "user-invalid-but-preserved"
    assert not (root / "example/scripts/run.py").exists()
    assert skill_catalog(context.user_id) == []


@pytest.mark.anyio
async def test_create_read_edit_delete_and_metadata_atomicity(skill_context):
    context = skill_context
    result = await write(context, "scripts/run.py", "print('old')")
    assert result["result"]["files"] == ["scripts/run.py"]
    assert skill_catalog(context.user_id) == []
    assert (await call(context, "load_skill", name="example"))["error_code"] == "INVALID_SKILL_METADATA"
    assert (await write(context, "SKILL.md", entry()))["error_code"] is None
    loaded = await call(context, "load_skill", name="example")
    assert loaded["result"] == {"name": "example", "content": entry(), "files": ["SKILL.md", "scripts/run.py"]}
    assert json.loads(json.dumps(context.loaded_skills)) == context.loaded_skills
    read = await call(context, "read_file", skill_name="example", path="scripts/run.py",
                      start_line_no=1, start_byte_no=1, total_lines=10)
    assert read["result"]["text"] == "print('old')"
    result = await call(context, "edit_file", skill_name="example", path="scripts/run.py",
                        edits=[{"old_text": "old", "new_text": "new"}])
    assert result["error_code"] is None and result["result"]["files"] == loaded["result"]["files"]
    for tool, arguments in [
        ("write_file", {"content": entry("wrong")}),
        ("edit_file", {"edits": [{"old_text": "name: example", "new_text": "name: wrong"}]}),
    ]:
        result = await call(context, tool, skill_name="example", path="SKILL.md", **arguments)
        assert result["error_code"] == "INVALID_SKILL_METADATA"
        assert load_skill(context, LoadSkillArgs(name="example"))["content"] == entry()
    for _ in range(2):
        result = await write(context, "scripts/run.py", None, operation="delete")
        assert result["error_code"] is None and result["result"]["files"] == ["SKILL.md"]
    assert (await write(context, "SKILL.md", None, operation="delete"))["error_code"] == "SKILL_ENTRY_REQUIRED"
    assert (await write(context, "scripts", None, operation="delete"))["error_code"] == "NOT_A_FILE"
    assert (await call(context, "write_file", path="x", content=None, operation="delete"))["error_code"] == "INVALID_ARGUMENTS"
    assert (await write(context, "empty", ""))["result"]["bytes_written"] == 0
    assert (layout.get_skills_dir(context.user_id) / "example/empty").is_file()


@pytest.mark.anyio
async def test_invalid_creation_and_missing_delete_do_not_create_directories(skill_context):
    context = skill_context
    assert (await write(context, "SKILL.md", entry("wrong")))["error_code"] == "INVALID_SKILL_METADATA"
    result = await write(context, "missing/file", None, operation="delete")
    assert result == {"error_code": None, "result": {
        "path": "missing/file", "deleted": False, "skill_name": "example", "files": [],
    }}
    assert list(layout.get_skills_dir(context.user_id).iterdir()) == []


@pytest.mark.anyio
async def test_catalog_hides_manual_skill_but_allows_explicit_load(skill_context):
    context = skill_context
    await write(context, "SKILL.md", entry("auto"), name="auto")
    await write(context, "SKILL.md", entry("manual", "allow_auto_use: false\n"), name="manual")
    assert skill_catalog(context.user_id) == [{"name": "auto", "description": "Test purpose"}]
    assert (await call(context, "load_skill", name="manual"))["error_code"] is None
    other = replace(context, user_id=str(uuid4()), loaded_skills={})
    initialize_skills(other.user_id, None)
    assert (await call(other, "load_skill", name="auto"))["error_code"] == "SKILL_NOT_FOUND"


@pytest.mark.anyio
async def test_skill_paths_links_and_special_files_never_escape(skill_context, tmp_path):
    context = skill_context
    await write(context, "SKILL.md", entry())
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret"
    secret.write_text("private")
    root = layout.get_skills_dir(context.user_id) / "example"
    (root / "link").symlink_to(outside, target_is_directory=True)
    for tool, arguments in [
        ("write_file", {"content": "changed"}),
        ("read_file", {"start_line_no": 1, "start_byte_no": 1, "total_lines": 10}),
        ("edit_file", {"edits": [{"old_text": "private", "new_text": "changed"}]}),
    ]:
        for path in ["../outside/secret", "/secret", "link/secret"]:
            assert (await call(context, tool, skill_name="example", path=path, **arguments))["error_code"] == "INVALID_PATH"
    assert secret.read_text() == "private"
    assert (await call(context, "load_skill", name="example"))["error_code"] == "INVALID_SKILL_FILE"
    (root / "link").unlink()
    os.link(secret, root / "hardlink")
    assert (await call(context, "load_skill", name="example"))["error_code"] == "INVALID_SKILL_FILE"
    (root / "hardlink").unlink()
    os.mkfifo(root / "fifo")
    assert (await call(context, "load_skill", name="example"))["error_code"] == "INVALID_SKILL_FILE"
    for name in ["../example", "x/y", "/example", "x--y"]:
        assert (await call(context, "load_skill", name=name))["error_code"] == "INVALID_SKILL_NAME"


@pytest.mark.anyio
async def test_execution_requires_current_load_and_passes_literal_arguments(skill_context, monkeypatch):
    context = skill_context
    await write(context, "SKILL.md", entry())
    await write(context, "scripts/run.py", "print('old')")
    arguments = {"name": "example", "runner": "python3", "script": "scripts/run.py",
                 "args": ["{workspace}/out file", "; touch hacked", "$(secret)", "a|b"]}
    assert (await call(context, "run_skill_scripts", **arguments))["error_code"] == "SKILL_NOT_LOADED"
    await call(context, "load_skill", name="example")
    assert (await call(context, "run_skill_scripts", **{**arguments, "script": "missing.py"}))["error_code"] == "INVALID_SKILL_SCRIPT"
    await write(context, "scripts/run.py", "print('new')")
    assert (await call(context, "run_skill_scripts", **arguments))["error_code"] == "SKILL_CHANGED"
    await call(context, "load_skill", name="example")
    observed = []
    def run(workspace, command, timeout, *, stop_event, skill_dir):
        observed.append(Path(skill_dir))
        assert shlex.split(command) == ["python3", "./scripts/run.py", workspace + "/out file", "; touch hacked", "$(secret)", "a|b"]
        assert (Path(skill_dir) / "scripts/run.py").read_text() == "print('new')"
        assert not stop_event.is_set()
        return {"exit_code": 0}
    monkeypatch.setattr(importlib.import_module("laxcode.tools.builtin.skills"), "run_bash", run)
    # A new ToolContext models a graph checkpoint round-trip.
    context = replace(context, loaded_skills=json.loads(json.dumps(context.loaded_skills)))
    assert (await call(context, "run_skill_scripts", **arguments))["result"] == {"exit_code": 0}
    assert observed and not observed[0].exists()
    for runner in ["python3 -c", "../bash", "bash;id", "-python"]:
        assert (await call(context, "run_skill_scripts", **{**arguments, "runner": runner}))["error_code"] == "INVALID_ARGUMENTS"


def test_all_model_schemas_require_every_property():
    def check(value):
        if isinstance(value, dict):
            assert "default" not in value
            if value.get("type") == "object":
                assert value["additionalProperties"] is False
                assert set(value["required"]) == set(value["properties"])
            for child in value.values():
                check(child)
        elif isinstance(value, list):
            for child in value:
                check(child)
    for definition in tool_definitions({"enable_memory": "1"}):
        check(definition["parameters"])


@pytest.mark.anyio
async def test_graph_load_survives_failed_model_and_resume(chat_api, monkeypatch):
    h = chat_api
    h.context.builtin_skills_dir = BUILTINS
    context = replace(tool_context("unused"), user_id=h.conversation["user_id"])
    initialize_skills(context.user_id, None)
    await write(context, "SKILL.md", entry("manual", "allow_auto_use: false\n", "HIDDEN_BODY"), name="manual")
    await write(context, "scripts/run.py", "print('restored')", name="manual")
    def response(name, args, call_id):
        event = completed("")
        event["response"]["output"] = [{"type": "function_call", "name": name, "arguments": json.dumps(args),
                                       "call_id": call_id, "id": "fc_" + call_id}]
        return ModelStream([event])
    executed = []
    def run(*args, **kwargs):
        executed.append(args)
        return {"exit_code": 0, "stdout": "restored"}
    monkeypatch.setattr(importlib.import_module("laxcode.tools.builtin.skills"), "run_bash", run)
    streams = [response("load_skill", {"name": "manual"}, "load"), ModelStream([]),
               response("run_skill_scripts", {"name": "manual", "runner": "python3", "script": "scripts/run.py", "args": []}, "run"),
               ModelStream([completed("done")])]
    captured = []
    async with client_for(streams, captured) as client:
        h.context.client = client
        run_id = (await h.api.post(chat_path(h), json=payload(user_query="使用 manual 技能"))).json()["run_id"]
        assert (await wait_run(h, run_id))["status"] == "failed"
        run_record = await h.context.repository.get_run(run_id, context.user_id)
        config = {"configurable": {"thread_id": run_record.thread_id}}
        checkpoint = await h.graph.aget_state(config)
        assert checkpoint.values["loaded_skills"]["manual"]["files"] == ["SKILL.md", "scripts/run.py"]
        prompt = captured[0]["input"][0]["content"]
        assert '"name": "laxcode"' in prompt and '"name": "skill-creator"' in prompt
        assert "HIDDEN_BODY" not in prompt and '"name": "manual"' not in prompt
        assert (await h.api.post(f"/api/v1/runs/{run_id}/resume", json={"resume_time": 0})).status_code == 202
        assert (await wait_run(h, run_id))["status"] == "success"
        assert len(executed) == 1
