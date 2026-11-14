import asyncio
import io
import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID
from zipfile import ZipFile

import httpx2
import pytest
from sqlalchemy import select

from laxagent.api.app import create_app
from laxagent.config import AppConfig
from laxagent.eval_api.app import create_eval_app
from laxagent.eval_api.store import EvalStore
from laxagent.layout import get_project_workdir, get_user_data_dir
from laxagent.storage.models import Message, Project, Session, User
from test_chat import chat_api, payload, wait_run


@pytest.fixture
async def eval_api(repository, config_data):
    owner = create_app()
    owner.state.config = AppConfig.model_validate(config_data)
    owner.state.agent_context = SimpleNamespace(repository=repository)
    owner.state.task_manager = SimpleNamespace(submit=AsyncMock())
    app = create_eval_app(owner)
    async with app.router.lifespan_context(app):
        assert app.state.agent_context is owner.state.agent_context
        assert app.state.task_manager is owner.state.task_manager
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app), base_url="http://eval",
                                     trust_env=False) as client:
            yield SimpleNamespace(api=client, app=app, owner=owner,
                                  manager=owner.state.task_manager, repository=repository)
    assert app.state.task_manager is None
    assert owner.state.task_manager is not None


@pytest.mark.anyio
async def test_create_fresh_user_project_session_without_jwt(eval_api):
    h = eval_api
    results = [await h.api.post("/eval/create_session", json={"session_name": "  图片测试  "})
               for _ in range(2)]
    assert all(result.status_code == 201 for result in results)
    first, second = [result.json() for result in results]
    assert set(first) == {"user_id", "project_id", "session_id"}
    for key in first:
        assert first[key] != second[key]
    for data in (first, second):
        assert str(UUID(data["user_id"])) == data["user_id"]
        async with h.repository.db.transaction() as tx:
            assert await tx.scalar(select(User).where(User.user_id == data["user_id"])) is not None
            session = await tx.get(Session, data["session_id"])
            project = await tx.get(Project, data["project_id"])
            assert session.name == project.name == "图片测试"
            assert session.user_id == project.user_id == data["user_id"]
            assert session.project_id == project.id
        assert not get_user_data_dir(data["user_id"]).exists()


@pytest.mark.anyio
@pytest.mark.parametrize("body", [{}, {"session_name": ""}, {"session_name": "  "},
    {"session_name": 1}, {"session_name": "x", "user_id": "external"},
    {"session_name": "x", "project_name": "external"}])
async def test_create_rejects_bad_or_old_parameters(eval_api, body):
    assert (await eval_api.api.post("/eval/create_session", json=body)).status_code == 422
    async with eval_api.repository.db.transaction() as tx:
        assert await tx.scalar(select(User.id)) is None


@pytest.mark.anyio
async def test_eval_chat_is_removed(eval_api, conversation):
    response = await eval_api.api.post("/eval/chat", params={"session_id": conversation["session_id"]},
                                       json=payload())
    assert response.status_code == 404
    eval_api.manager.submit.assert_not_awaited()


async def finished_run(h, conversation):
    run = await h.repository.create_run(
        user_id=conversation["user_id"], session_id=conversation["session_id"],
        user_query="question", model="example:model-name", context_limit=128000, output_limit=16000,
    )
    await h.repository.finish_run(run.id)
    return run


@pytest.mark.anyio
async def test_collect_exports_full_messages_and_workspace(eval_api, conversation, tmp_path):
    h = eval_api
    run = await finished_run(h, conversation)
    async with h.repository.db.transaction() as tx:
        for seq, role in ((3, "tool"), (1, "system"), (2, "assistant")):
            tx.add(Message(session_id=conversation["session_id"], seq=seq, role=role,
                           content=f"原始内容 {seq}", appendix=[], reasoning=[{"text": "公开 reasoning"}],
                           tool_call_id="call_1" if role == "tool" else None,
                           tool_name="read_file" if role == "tool" else None,
                           tool_call={"arguments": "{}"} if role == "tool" else None))
    workspace = get_project_workdir(conversation["user_id"], conversation["project_id"])
    (workspace / "空目录").mkdir(parents=True)
    (workspace / "内容.txt").write_text("图片说明", encoding="utf-8")
    (workspace / "run.sh").write_bytes(b"#!/bin/sh\n")
    (workspace / "run.sh").chmod(0o755)
    secret = tmp_path / "outside.txt"
    secret.write_text("never export the target")
    (workspace / "link").symlink_to(secret)
    response = await h.api.post("/eval/collect", json={"session_id": conversation["session_id"], "run_id": run.id})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    with ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.testzip() is None
        rows = json.loads(archive.read("trajectory.json"))
        assert [row["seq"] for row in rows] == [1, 2, 3]
        assert rows[-1]["content"] == "原始内容 3"
        assert rows[-1]["tool_call"] == {"arguments": "{}"}
        assert rows[-1]["reasoning"] == [{"text": "公开 reasoning"}]
        assert "workspace/空目录/" in archive.namelist()
        assert archive.read("workspace/内容.txt").decode() == "图片说明"
        assert archive.getinfo("workspace/run.sh").external_attr >> 16 & 0o777 == 0o755
        assert stat.S_ISLNK(archive.getinfo("workspace/link").external_attr >> 16)
        assert archive.read("workspace/link") == os.fsencode(secret)
        assert all("never export the target" not in archive.read(name).decode(errors="replace")
                   for name in archive.namelist())


@pytest.mark.anyio
async def test_collect_missing_workspace_is_valid_empty_archive(eval_api, conversation):
    run = await finished_run(eval_api, conversation)
    response = await eval_api.api.post("/eval/collect", json={
        "session_id": conversation["session_id"], "run_id": run.id,
    })
    assert response.status_code == 200, response.text
    with ZipFile(io.BytesIO(response.content)) as archive:
        assert set(archive.namelist()) == {"workspace/", "trajectory.json"}
        assert json.loads(archive.read("trajectory.json")) == []


@pytest.mark.anyio
async def test_snapshot_serializes_next_turn_submission(eval_api, conversation):
    h = eval_api
    run = await finished_run(h, conversation)
    async with EvalStore(h.repository.db).snapshot(conversation["session_id"], run.id):
        submitted = asyncio.create_task(h.repository.create_run(
            user_id=conversation["user_id"], session_id=conversation["session_id"],
            user_query="next", model="example:model-name", context_limit=128000, output_limit=16000,
        ))
        done, _ = await asyncio.wait({submitted}, timeout=0.1)
        assert not done
    next_run = await asyncio.wait_for(submitted, 2)
    assert next_run.id != run.id


@pytest.mark.anyio
async def test_collect_closes_temporary_file_on_archive_failure(eval_api, conversation, monkeypatch):
    import laxagent.eval_api.app as module
    run = await finished_run(eval_api, conversation)
    file = io.BytesIO()
    monkeypatch.setattr(module, "TemporaryFile", lambda **kwargs: file)

    def fail(*args):
        raise OSError("private filesystem details")

    monkeypatch.setattr(module, "write_archive", fail)
    response = await eval_api.api.post("/eval/collect", json={
        "session_id": conversation["session_id"], "run_id": run.id,
    })
    assert response.status_code == 503
    assert "private filesystem" not in response.text
    assert file.closed


@pytest.mark.anyio
async def test_collect_requires_latest_finished_run_and_live_session(eval_api, conversation):
    h = eval_api
    first = await finished_run(h, conversation)
    run = await h.repository.create_run(
        user_id=conversation["user_id"], session_id=conversation["session_id"],
        user_query="next", model="example:model-name", context_limit=128000, output_limit=16000,
    )
    body = {"session_id": conversation["session_id"], "run_id": run.id}
    for status in ("waiting", "running", "paused"):
        async with h.repository.db.transaction() as tx:
            from laxagent.storage.models import Run
            (await tx.get(Run, run.id)).status = status
        assert (await h.api.post("/eval/collect", json=body)).status_code == 409
    assert (await h.api.post("/eval/collect", json={**body, "run_id": first.id})).status_code == 409
    other = await EvalStore(h.repository.db).create_session("other")
    assert (await h.api.post("/eval/collect", json={**body, "session_id": other.id})).status_code == 404
    assert (await h.api.post("/eval/collect", json={**body, "run_id": 999999999})).status_code == 404
    await h.repository.finish_run(run.id, cancelled=True)
    assert (await h.api.post("/eval/collect", json=body)).status_code == 200
    await h.repository.delete_session(conversation["user_id"], conversation["session_id"])
    assert (await h.api.post("/eval/collect", json=body)).status_code == 404
    assert (await h.api.post("/eval/chat", params={"session_id": conversation["session_id"]},
                            json=payload())).status_code == 404


@pytest.mark.anyio
async def test_eval_routes_are_not_on_public_application(eval_api):
    public = eval_api.owner.openapi()["paths"]
    private = eval_api.app.openapi()["paths"]
    assert not any(path.startswith("/eval") for path in public)
    assert set(private) == {"/eval/create_session", "/eval/collect"}
    assert "application/zip" in private["/eval/collect"]["post"]["responses"]["200"]["content"]


@pytest.mark.anyio
async def test_eval_session_uses_authenticated_public_chat_and_collect(chat_api):
    h = chat_api
    owner = create_app()
    owner.state.config = h.config
    owner.state.agent_context = h.context
    owner.state.task_manager = h.manager
    app = create_eval_app(owner)
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app), base_url="http://eval",
                                     trust_env=False) as client:
            created = await client.post("/eval/create_session", json={"session_name": "public-chat"})
            assert created.status_code == 201
            ids = created.json()
            path = f"/api/v1/sessions/{ids['session_id']}/chat"
            denied = await h.api.post(path, json=payload(), headers={"Authorization": "Bearer invalid"})
            assert denied.status_code == 401
            logged_in = await h.api.post("/api/v1/login", json={"user_id": ids["user_id"]})
            assert logged_in.status_code == 200
            h.api.headers["Authorization"] = f"Bearer {logged_in.json()['access_token']}"
            response = await h.api.post(path, json=payload())
            assert response.status_code == 202, response.text
            run_id = response.json()["run_id"]
            assert (await wait_run(h, run_id))["status"] == "success"
            result = await client.post("/eval/collect", json={
                "session_id": ids["session_id"], "run_id": run_id,
            })
            assert result.status_code == 200, result.text
            with ZipFile(io.BytesIO(result.content)) as archive:
                messages = json.loads(archive.read("trajectory.json"))
                assert messages[-1]["content"] == "hello"
