import asyncio
import json
from types import SimpleNamespace

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import text

from laxagent.agent.context import AgentContext
from laxagent.agent.graph import build_graph
from laxagent.models.responses import append_reasoning, completed_reasoning
from model_helpers import ModelStream, client_for, completed
from storage_helpers import message_snapshot, run_agent
from test_agent import MemoryEvents, state
from test_web_api import web_api, login


def test_public_reasoning_extraction_and_channel_order():
    output = [{"type": "reasoning", "encrypted_content": "opaque", "content": [
        {"type": "reasoning_text", "text": "first"}, {"type": "reasoning_text", "text": " second"},
    ], "summary": [{"type": "summary_text", "text": "summary"}]}]
    parts = completed_reasoning(output)
    append_reasoning(parts, "text", "last")
    append_reasoning(parts, "text", "")
    assert parts == [{"channel": "text", "text": "first second"},
                     {"channel": "summary", "text": "summary"}, {"channel": "text", "text": "last"}]


@pytest.mark.anyio
@pytest.mark.parametrize("deltas", [False, True])
async def test_reasoning_history_fallback_and_context_separation(repository, conversation, web_api, deltas):
    final = completed("说明正文")
    final["response"]["output"].insert(0, {"type": "reasoning", "encrypted_content": "opaque", "summary": [
        {"type": "summary_text", "text": "completed summary"},
    ]})
    chunks = [{"type": "response.reasoning_text.delta", "delta": value} for value in ("first", " second")] if deltas else []
    expected = [{"channel": "text", "text": "first second"}] if deltas else [{"channel": "summary", "text": "completed summary"}]
    events = MemoryEvents()
    async with client_for(ModelStream([*chunks, final]), []) as client:
        await run_agent(build_graph(), state(conversation), AgentContext(client, events, repository))
    _, rows = await message_snapshot(repository, conversation["session_id"])
    assert rows[0] == rows[1] and rows[0][-1].reasoning == expected
    assert next(e for e in events.items if e["type"] == "message_end")["content"]["reasoning"] == expected
    await login(web_api, conversation["user_id"])
    url = f"/api/v1/sessions/{conversation['session_id']}/messages"
    history = (await web_api.get(url, params={"limit": 1})).json()
    assert history["items"] == [{"seq": 2, "role": "assistant", "content": "说明正文", "reasoning": expected}]
    assert history["next_before_seq"] == 2
    await repository.append_message(**conversation, role="assistant", content="legacy")
    history = (await web_api.get(url)).json()["items"]
    assert "reasoning" not in history[0]
    assert history[-1]["reasoning"] == []
    _, context = await repository.append_message(**conversation, role="user", content="next", load_context=True)
    assert all("reasoning" not in row for row in context)


@pytest.mark.anyio
async def test_reasoning_migration_roundtrip_preserves_messages(repository, conversation, config_data, tmp_path):
    await repository.append_message(**conversation, role="assistant", content="kept",
                                    reasoning=[{"channel": "text", "text": "thought"}])
    path = tmp_path / "migration.json"
    path.write_text(json.dumps(config_data))
    config = Config("alembic.ini")
    config.cmd_opts = SimpleNamespace(x=[f"config={path}"])
    try:
        await asyncio.to_thread(command.downgrade, config, "0004")
        async with repository.db.transaction() as tx:
            for table in ("messages", "session_messages"):
                assert await tx.scalar(text(f"SELECT content FROM {table}")) == "kept"
    finally:
        await asyncio.to_thread(command.upgrade, config, "head")
    _, rows = await message_snapshot(repository, conversation["session_id"])
    assert rows[0] == rows[1] and rows[0][0].reasoning is None
