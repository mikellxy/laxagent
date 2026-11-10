import asyncio
from uuid import uuid4

import httpx2
import pytest
from pydantic import SecretStr
from langgraph.runtime import Runtime
from sqlalchemy import event, inspect, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError

from laxcode.agent.context import AgentContext
from laxcode.agent.nodes.preparation import prepare
from laxcode.context.prompts import system_prompt
from laxcode.layout import get_project_workdir
from laxcode.api.app import create_app
from laxcode.storage.database import Database, DatabaseError
from laxcode.storage.models import Message, Session, SessionMessage, User
from storage_helpers import message_snapshot


@pytest.mark.anyio
async def test_migration_has_no_foreign_keys_and_nullable_tool_columns(repository):
    def check(connection):
        schema = inspect(connection)
        assert set(schema.get_table_names()) == {
            "users", "projects", "sessions", "messages", "session_messages", "runs", "memories", "alembic_version",
        }
        for table in schema.get_table_names():
            assert schema.get_foreign_keys(table) == []
        for table in ("messages", "session_messages"):
            columns = {c["name"]: c for c in schema.get_columns(table)}
            assert columns["tool_call_id"]["nullable"]
            assert str(columns["tool_call_id"]["type"]) == "TEXT"
            assert columns["tool_name"]["nullable"]
            assert str(columns["tool_name"]["type"]) == "TEXT"
            assert columns["tool_call"]["nullable"]
            assert isinstance(columns["tool_call"]["type"], JSONB)
            assert columns["id"]["identity"]["always"]
            assert any(c["column_names"] == ["session_id", "seq"] for c in schema.get_unique_constraints(table))

    async with repository.db.engine.connect() as connection:
        await connection.run_sync(check)


@pytest.mark.anyio
async def test_creation_apis_and_ownership(repository, config_data):
    from laxcode.config import AppConfig
    app = create_app(AppConfig.model_validate(config_data))
    app.state.agent_context = AgentContext(None, None, repository)
    user_id = str(uuid4())
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app), base_url="http://test", trust_env=False) as api:
        assert (await api.post("/api/v1/projects", json={"name": "test"})).status_code == 401
        token = (await api.post("/api/v1/login", json={"user_id": user_id})).json()["access_token"]
        api.headers["Authorization"] = f"Bearer {token}"
        assert (await api.post("/api/v1/projects", json={"user_id": user_id, "name": "test"})).status_code == 422
        assert (await api.post("/api/v1/projects", json={"name": "  "})).status_code == 422
        project = await api.post("/api/v1/projects", json={"name": " test "})
        assert project.status_code == 201 and project.json()["name"] == "test"
        path = f"/api/v1/projects/{project.json()['id']}/sessions"
        other = str(uuid4())
        await repository.create_user(other)
        other_project = await repository.create_project(other, "other")
        assert (await api.post(f"/api/v1/projects/{other_project.id}/sessions")).status_code == 404
        assert (await api.post(path, json={"user_id": user_id})).status_code == 422
        session = await api.post(path)
        assert session.status_code == 201
        assert session.json()["name"] == "会话 1"
        assert session.json()["next_msg_seq"] == 1
        assert session.json()["user_id"] == user_id
        assert session.json()["project_id"] == project.json()["id"]


@pytest.mark.anyio
@pytest.mark.parametrize("model,hook", [(Message, "before_insert"), (SessionMessage, "before_insert"), (Session, "before_update")])
async def test_message_transaction_rolls_back_both_tables_and_sequence(repository, conversation, model, hook):
    def fail(*args):
        raise IntegrityError("forced failure", {}, RuntimeError("test"))

    event.listen(model, hook, fail)
    try:
        with pytest.raises(DatabaseError):
            await repository.append_message(**conversation, role="user", content="rollback")
    finally:
        event.remove(model, hook, fail)
    assert await message_snapshot(repository, conversation["session_id"]) == (1, [[], []])
    next_seq, _ = await repository.append_message(**conversation, role="user", content="retry")
    assert next_seq == 2


@pytest.mark.anyio
async def test_row_lock_serializes_allocations_and_expected_sequence_conflicts(repository, conversation):
    results = await asyncio.gather(*[
        repository.append_message(**conversation, role="user", content=str(i)) for i in range(5)
    ])
    assert sorted(result[0] for result in results) == [2, 3, 4, 5, 6]
    next_seq, rows = await message_snapshot(repository, conversation["session_id"])
    assert next_seq == 6 and rows[0] == rows[1]
    assert [row.seq for row in rows[0]] == [1, 2, 3, 4, 5]
    results = await asyncio.gather(*[
        repository.append_message(**conversation, role="assistant", content=str(i), expected_seq=6)
        for i in range(2)
    ], return_exceptions=True)
    assert sum(isinstance(result, tuple) for result in results) == 1
    error = next(result for result in results if isinstance(result, DatabaseError))
    assert error.status == 409
    next_seq, rows = await message_snapshot(repository, conversation["session_id"])
    assert next_seq == 7 and len(rows[0]) == 6 and rows[0] == rows[1]
    assert all(row.tool_call is None and row.tool_call_id is None for row in rows[0])
    async with repository.db.transaction() as transaction:
        # JSON null and SQL NULL are different; these reserved fields must be SQL NULL.
        assert len((await transaction.scalars(select(Message).where(Message.tool_call.is_(None)))).all()) == 6


@pytest.mark.anyio
async def test_preparation_orders_context_and_hides_database_fields(repository, conversation):
    async with repository.db.transaction() as transaction:
        for seq, role, content in [(2, "assistant", "second"), (1, "user", "first")]:
            for model in (Message, SessionMessage):
                transaction.add(model(seq=seq, role=role, content=content, session_id=conversation["session_id"]))
        session = await transaction.get(Session, conversation["session_id"])
        session.next_msg_seq = 3
    result = await prepare(
        {**conversation, "user_query": "third", "next_msg_seq": 1, "messages": []},
        Runtime(context=AgentContext(None, None, repository)),
    )
    assert result["workspace"] == str(get_project_workdir(conversation["user_id"], conversation["project_id"]))
    assert result["model_input"] == result["messages"]
    assert result["pending_tool_calls"] == []
    assert result["next_msg_seq"] == 4
    assert result["messages"] == [
        {"role": "system", "content": system_prompt(result["workspace"])},
        {"role": "user", "content": "first"}, {"role": "assistant", "content": "second"},
        {"role": "user", "content": "third"},
    ]


@pytest.mark.anyio
async def test_message_write_rejects_wrong_owner_and_orphan_session(repository, conversation):
    with pytest.raises(DatabaseError) as exc:
        await repository.append_message(**{**conversation, "user_id": str(uuid4())}, role="user", content="no")
    assert exc.value.status == 404
    async with repository.db.transaction() as transaction:
        user = await transaction.scalar(select(User).where(User.user_id == conversation["user_id"]))
        await transaction.delete(user)
    with pytest.raises(DatabaseError) as exc:
        await repository.append_message(**conversation, role="user", content="orphan")
    assert exc.value.status == 404
    assert await message_snapshot(repository, conversation["session_id"]) == (1, [[], []])


@pytest.mark.anyio
async def test_database_connect_failure_is_sanitized(migrated_database):
    config = migrated_database.model_copy(update={"password": SecretStr("invalid-secret-password")})
    with pytest.raises(DatabaseError) as exc:
        async with Database.open(config):
            pass
    assert "invalid-secret-password" not in str(exc.value)


@pytest.mark.anyio
async def test_startup_rejects_wrong_migration_revision(monkeypatch, migrated_database):
    monkeypatch.setattr("laxcode.storage.database.SCHEMA_REVISION", "future-revision")
    with pytest.raises(DatabaseError, match="schema is not current"):
        async with Database.open(migrated_database):
            pass
