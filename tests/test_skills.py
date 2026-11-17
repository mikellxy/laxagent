import asyncio
from dataclasses import replace
import json
from pathlib import Path
from uuid import uuid4

import pytest

from laxagent import layout
from laxagent.skills import builtin_snapshots, initialize_skills, parse_metadata, skill_catalog, SkillError
from laxagent.tools.executor import execute_tool
from laxagent.tools.registry import tool_definitions
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
    context = tool_context(workspace, user_id=str(uuid4()))
    initialize_skills(context.user_id, None)
    return context


async def call(context, tool, **args):
    return await execute_tool(tool, json.dumps(args), context=context)


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
    assert {s.metadata["name"] for s in builtin_snapshots(BUILTINS)} == {"laxagent", "skill-creator"}


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
async def test_run_skill_scripts_is_removed(tmp_path):
    from laxagent.tools.registry import TOOLS

    assert "run_skill_scripts" not in TOOLS
    assert "run_skill_scripts" not in {tool["name"] for tool in tool_definitions()}
    result = await call(tool_context(tmp_path), "run_skill_scripts", name="example")
    assert result["error_code"] == "UNKNOWN_TOOL"


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
async def test_graph_reload_after_resume_uses_current_files_without_saved_state(chat_api, monkeypatch):
    h = chat_api
    h.context.builtin_skills_dir = BUILTINS
    context = tool_context("unused", user_id=h.conversation["user_id"])
    initialize_skills(context.user_id, None)
    root = layout.get_skills_dir(context.user_id) / "manual"
    root.mkdir()
    (root / "SKILL.md").write_text(entry("manual", "allow_auto_use: false\n", "HIDDEN_BODY"))
    def response(name, args, call_id):
        event = completed("")
        event["response"]["output"] = [{"type": "function_call", "name": name, "arguments": json.dumps(args),
                                       "call_id": call_id, "id": "fc_" + call_id}]
        return ModelStream([event])
    streams = [response("load_skill", {"name": "manual"}, "load"), *[ModelStream([]) for _ in range(3)],
               response("load_skill", {"name": "manual"}, "reload"),
               ModelStream([completed("done")])]
    captured = []
    async with client_for(streams, captured) as client:
        h.context.client = client
        run_id = (await h.api.post(chat_path(h), json=payload(user_query="使用 manual 技能"))).json()["run_id"]
        assert (await wait_run(h, run_id))["status"] == "failed"
        run_record = await h.context.repository.get_run(run_id, context.user_id)
        config = {"configurable": {"thread_id": run_record.thread_id}}
        checkpoint = await h.graph.aget_state(config)
        assert "loaded_skills" not in checkpoint.values
        (root / "SKILL.md").write_text(entry("manual", "allow_auto_use: false\n", "UPDATED_BODY"))
        prompt = captured[0]["input"][0]["content"]
        assert '"name": "laxagent"' in prompt and '"name": "skill-creator"' in prompt
        assert "HIDDEN_BODY" not in prompt and '"name": "manual"' not in prompt
        assert (await h.api.post(f"/api/v1/runs/{run_id}/resume", json={"resume_time": 0})).status_code == 202
        assert (await wait_run(h, run_id))["status"] == "success"
        outputs = [item["output"] for item in captured[-1]["input"] if item.get("type") == "function_call_output"]
        assert "UPDATED_BODY" in outputs[-1]
        assert "/user/skills/manual/SKILL.md" in outputs[-1]
