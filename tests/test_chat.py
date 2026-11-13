import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import httpx2
import jwt
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import delete, select

from laxagent.agent.context import AgentContext
from laxagent.agent.graph import build_graph
from laxagent.api.app import create_app
from laxagent.config import AppConfig
from laxagent.runtime.events import EventStore, EventStoreError
from laxagent.runtime.runner import run_agent
from laxagent.runtime.task_manager import TaskManager
from laxagent.storage.database import DatabaseError
from laxagent.run_errors import RunError
from laxagent.storage.models import Project, Run, User
from model_helpers import ModelStream, client_for, completed
from storage_helpers import message_snapshot
from test_tool_loop import tool_response


@pytest.fixture
async def chat_api(repository, conversation, config_data, monkeypatch):
    config = AppConfig.model_validate(config_data)
    app = create_app()
    app.state.config = config
    captured, created = [], []
    async with EventStore.connect(config.nats.url) as events, client_for(ModelStream([completed()]), captured) as client:
        create = events.create_run

        async def tracked_create(run_id, **kwargs):
            await create(run_id, **kwargs)
            if run_id not in created:
                created.append(run_id)

        monkeypatch.setattr(events, "create_run", tracked_create)
        context = AgentContext(client, events, repository)
        graph = build_graph(InMemorySaver())
        manager = TaskManager(graph, context)
        await manager.start()
        app.state.agent_context = context
        app.state.graph = graph
        app.state.task_manager = manager
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app), base_url="http://test", trust_env=False) as api:
            token = (await api.post("/api/v1/login", json={"user_id": conversation["user_id"]})).json()["access_token"]
            api.headers["Authorization"] = f"Bearer {token}"
            try:
                yield SimpleNamespace(api=api, manager=manager, context=context, graph=graph,
                                      config=config, conversation=conversation, captured=captured, created=created)
            finally:
                await manager.close()
                for run_id in created:
                    await events.js.delete_stream(events.stream_name(run_id))


def chat_path(h):
    return f"/api/v1/sessions/{h.conversation['session_id']}/chat"


def payload(**changes):
    return {"user_query": "  你好\n保留原文  ", "model": "example:model-name", **changes}


async def wait_run(h, run_id):
    async with asyncio.timeout(5):
        while True:
            result = await h.api.get(f"/api/v1/runs/{run_id}")
            assert result.status_code == 200, result.text
            if result.json()["status"] in ("success", "failed"):
                # Terminal DB commit precedes terminal SSE publication.
                execution = h.manager.executions.get((run_id, result.json()["resume_time"]))
                if execution is not None:
                    await asyncio.gather(execution.task, return_exceptions=True)
                return result.json()
            await asyncio.sleep(0.01)


@pytest.mark.anyio
async def test_chat_snapshots_model_checkpoint_and_sse_replay(chat_api):
    h = chat_api
    result = await h.api.post(chat_path(h), json=payload())
    assert result.status_code == 202, result.text
    run_id = result.json()["run_id"]
    assert result.json() == {"run_id": run_id, "session_id": h.conversation["session_id"], "status": "waiting", "resume_time": 0}
    # The stream exists before the response is sent, even if the worker has not started.
    await h.context.events.js.stream_info(h.context.events.stream_name(run_id))
    summary = await wait_run(h, run_id)
    assert summary["status"] == "success" and summary["error"] is None
    assert summary["created_at"] and summary["started_at"] and summary["finished_at"]
    run = await h.context.repository.get_run(run_id, h.conversation["user_id"])
    assert (run.context_limit, run.output_limit, run.model) == (128000, 16000, "example:model-name")
    snapshot = await h.graph.aget_state({"configurable": {"thread_id": run.thread_id}})
    assert snapshot.values["run_id"] == run_id and not snapshot.next
    assert snapshot.values["messages"][-1]["content"] == "hello"
    seq, rows = await message_snapshot(h.context.repository, run.session_id)
    assert seq == 3 and rows[0] == rows[1]
    assert rows[0][0].content == payload()["user_query"]
    first = await h.api.get(f"/api/v1/runs/{run_id}/events")
    second = await h.api.get(f"/api/v1/runs/{run_id}/events")
    assert first.content == second.content
    assert '"status": "completed"' in first.text
    assert (await h.context.events.js.stream_info(h.context.events.stream_name(run_id))).state.consumer_count == 0
    assert "thread_id" not in summary and "jwt_secret" not in summary
    assert (await h.api.post("/api/v1/debug/agent/invoke", json={})).status_code == 404
    assert (await h.api.get(f"/api/v1/debug/runs/{run_id}/events")).status_code == 404
    # New runs rebuild history from PG, without inheriting prior run checkpoint state.
    next_result = await h.api.post(chat_path(h), json=payload(user_query="继续"))
    next_id = next_result.json()["run_id"]
    assert (await wait_run(h, next_id))["status"] == "success"
    next_run = await h.context.repository.get_run(next_id, run.user_id)
    assert next_run.thread_id != run.thread_id
    assert len(h.captured[1]["input"]) == 4


@pytest.mark.anyio
async def test_submission_is_async_and_same_session_is_exclusive(chat_api):
    h = chat_api
    entered = asyncio.Event()
    stream = ModelStream([], wait=entered)
    async with client_for(stream, []) as client:
        h.context.client = client
        responses = await asyncio.gather(*(h.api.post(chat_path(h), json=payload()) for _ in range(2)))
        assert sorted(response.status_code for response in responses) == [202, 409]
        run_id = next(r.json()["run_id"] for r in responses if r.status_code == 202)
        await asyncio.wait_for(entered.wait(), 2)
        assert (await h.api.get(f"/api/v1/runs/{run_id}")).json()["status"] == "running"
        # Duplicate dispatch must not call the graph or finish the already running task.
        assert await run_agent(h.graph, run_id, h.context) is None
        assert (await h.api.get(f"/api/v1/runs/{run_id}")).json()["status"] == "running"
        await h.manager.close()
        assert stream.closed
        assert (await wait_run(h, run_id))["status"] == "failed"


@pytest.mark.anyio
@pytest.mark.parametrize("body", [{}, {"user_query": "x"}, payload(user_query="  "),
    payload(user_query=42), payload(model="missing:model"), payload(model="bad"),
    payload(user_id=str(uuid4())), payload(session_id=1), {"query": "old"}])
async def test_invalid_chat_never_creates_run(chat_api, body):
    h = chat_api
    assert (await h.api.post(chat_path(h), json=body)).status_code == 422
    async with h.context.repository.db.transaction() as tx:
        assert await tx.scalar(select(Run.id)) is None


@pytest.mark.anyio
async def test_login_and_jwt_validation(chat_api):
    h = chat_api
    api = h.api
    assert (await api.post("/api/v1/login", json={"user_id": "invalid"})).status_code == 422
    response = await api.post("/api/v1/login", json={"user_id": h.conversation["user_id"].upper()})
    assert response.status_code == 200 and response.json()["expires_in"] == 86400
    valid = response.json()["access_token"]
    now = datetime.now(timezone.utc)
    secret = h.config.auth.jwt_secret.get_secret_value()
    claims = {"sub": h.conversation["user_id"], "iat": now, "exp": now + timedelta(hours=1)}
    invalid = ["invalid", jwt.encode(claims, "wrong-secret-for-signing-at-least-32-bytes", algorithm="HS256"),
               jwt.encode({**claims, "exp": now - timedelta(seconds=1)}, secret, algorithm="HS256"),
               jwt.encode({"sub": claims["sub"]}, secret, algorithm="HS256"),
               jwt.encode({**claims, "sub": "not-uuid"}, secret, algorithm="HS256"),
               jwt.encode(claims, secret, algorithm="HS384")]
    for token in invalid:
        result = await api.post(chat_path(h), json=payload(), headers={"Authorization": f"Bearer {token}"})
        assert result.status_code == 401 and result.headers["www-authenticate"] == "Bearer"
    for authorization in ("", "Basic abc"):
        assert (await api.post(chat_path(h), json=payload(), headers={"Authorization": authorization})).status_code == 401
    async with h.context.repository.db.transaction() as tx:
        await tx.execute(delete(User).where(User.user_id == claims["sub"]))
    assert (await api.post(chat_path(h), json=payload(), headers={"Authorization": f"Bearer {valid}"})).status_code == 401


@pytest.mark.anyio
async def test_ownership_checks_on_chat_query_events_and_worker(chat_api):
    h = chat_api
    other = str(uuid4())
    await h.context.repository.create_user(other)
    token = (await h.api.post("/api/v1/login", json={"user_id": other})).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert (await h.api.post(chat_path(h), json=payload(), headers=headers)).status_code == 404
    result = await h.api.post(chat_path(h), json=payload())
    run_id = result.json()["run_id"]
    await wait_run(h, run_id)
    for suffix in ("", "/events"):
        assert (await h.api.get(f"/api/v1/runs/{run_id}{suffix}", headers=headers)).status_code == 404
        assert (await h.api.get(f"/api/v1/runs/{run_id + 100}{suffix}")).status_code == 404
    run = await h.context.repository.create_run(user_id=h.conversation["user_id"],
        session_id=h.conversation["session_id"], user_query="hello", model="example:model-name",
        context_limit=128000, output_limit=16000)
    await h.context.events.create_run(run.id)
    async with h.context.repository.db.transaction() as tx:
        project = await tx.get(Project, h.conversation["project_id"])
        project.user_id = other
    assert (await h.api.post(chat_path(h), json=payload())).status_code == 404
    with pytest.raises(RunError, match="Run data operation failed"):
        await run_agent(h.graph, run.id, h.context)
    async with h.context.repository.db.transaction() as tx:
        assert (await tx.get(Run, run.id)).status == "failed"
    assert len(h.captured) == 1


@pytest.mark.anyio
async def test_submission_nats_failure_is_persisted(chat_api, monkeypatch):
    h = chat_api
    async def fail(run_id):
        raise EventStoreError()
    monkeypatch.setattr(h.context.events, "create_run", fail)
    response = await h.api.post(chat_path(h), json=payload())
    assert response.status_code == 503
    async with h.context.repository.db.transaction() as tx:
        run = await tx.scalar(select(Run))
        assert run.status == "failed" and run.finished_at and run.error == "Run submission failed"
    assert not h.captured


@pytest.mark.anyio
async def test_tool_loop_through_chat(chat_api):
    h = chat_api
    async with client_for([ModelStream([tool_response()]), ModelStream([completed("done")])], []) as client:
        h.context.client = client
        response = await h.api.post(chat_path(h), json=payload())
        run_id = response.json()["run_id"]
        assert (await wait_run(h, run_id))["status"] == "success"
        seq, rows = await message_snapshot(h.context.repository, h.conversation["session_id"])
        assert seq == 6 and rows[0] == rows[1]
        assert [r.role for r in rows[0]] == ["user", "assistant", "tool", "tool", "assistant"]


@pytest.mark.anyio
async def test_completion_publish_failure_does_not_revert_success(chat_api, monkeypatch):
    h = chat_api
    publish = h.context.events.publish
    async def fail_end(event):
        if event["type"] == "sse_end":
            run = await h.context.repository.get_run(event["run_id"], h.conversation["user_id"])
            assert run.status == "success"
            raise EventStoreError()
        await publish(event)
    monkeypatch.setattr(h.context.events, "publish", fail_end)
    response = await h.api.post(chat_path(h), json=payload())
    assert (await wait_run(h, response.json()["run_id"]))["status"] == "success"


@pytest.mark.anyio
async def test_cancelled_http_submission_is_still_owned(chat_api, monkeypatch):
    h = chat_api
    entered, release = asyncio.Event(), asyncio.Event()
    create = h.context.events.create_run
    async def blocked_create(run_id):
        entered.set()
        await release.wait()
        await create(run_id)
    monkeypatch.setattr(h.context.events, "create_run", blocked_create)
    request = asyncio.create_task(h.api.post(chat_path(h), json=payload()))
    await asyncio.wait_for(entered.wait(), 2)
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert len(h.manager.controls) == 1
    release.set()
    await asyncio.gather(*h.manager.controls)
    async with h.context.repository.db.transaction() as tx:
        run_id = await tx.scalar(select(Run.id))
    assert (await wait_run(h, run_id))["status"] == "success"


@pytest.mark.anyio
async def test_api_startup_keeps_other_instances_active_runs(repository, conversation, config_data):
    values = dict(user_id=conversation["user_id"], session_id=conversation["session_id"], user_query="x",
                  model="example:model-name", context_limit=100, output_limit=10)
    run = await repository.create_run(**values)
    await repository.claim_run(run.id)
    app = create_app(AppConfig.model_validate(config_data))
    async with app.router.lifespan_context(app):
        assert (await repository.get_run(run.id, run.user_id)).status == "running"
    assert (await repository.get_run(run.id, run.user_id)).status == "running"


@pytest.mark.anyio
async def test_checkpoint_write_failure_is_sanitized_and_persisted(chat_api):
    class BrokenSaver(InMemorySaver):
        async def aput(self, *args, **kwargs):
            raise OSError("checkpoint-secret-key")

    h = chat_api
    h.manager.graph = build_graph(BrokenSaver())
    response = await h.api.post(chat_path(h), json=payload())
    run_id = response.json()["run_id"]
    summary = await wait_run(h, run_id)
    assert summary["status"] == "failed" and summary["error"] == "Agent execution failed"
    replay = await h.api.get(f"/api/v1/runs/{run_id}/events")
    assert '"status": "failed"' in replay.text
    assert "checkpoint-secret-key" not in replay.text


@pytest.mark.anyio
async def test_shutdown_finishes_run_cancelled_before_worker_starts(chat_api):
    h = chat_api
    # Direct submission returns immediately after create_task, before that task's first step.
    run = await h.manager._submit(dict(user_id=h.conversation["user_id"],
        session_id=h.conversation["session_id"], user_query="hello", model="example:model-name",
        context_limit=128000, output_limit=16000))
    await h.manager.close()
    summary = await wait_run(h, run.id)
    assert summary["status"] == "failed" and summary["error"] == "Execution cancelled"
    assert not h.captured
    assert (await h.api.post(chat_path(h), json=payload())).status_code == 503


@pytest.mark.anyio
async def test_failed_terminal_commit_does_not_emit_success(chat_api, monkeypatch, caplog):
    h = chat_api
    async def fail_finish(*args, **kwargs):
        raise DatabaseError()
    monkeypatch.setattr(h.context.repository, "finish_run", fail_finish)
    response = await h.api.post(chat_path(h), json=payload())
    run_id = response.json()["run_id"]
    async with asyncio.timeout(5):
        while h.manager.executions:
            await asyncio.sleep(0.01)
    run = await h.context.repository.get_run(run_id, h.conversation["user_id"])
    assert run.status == "running" and run.finished_at is None
    async with h.context.events.subscribe(run_id) as sub:
        messages = await sub.fetch(batch=1, timeout=1)
        assert json.loads(messages[0].data)["type"] == "message_end"
    assert "Could not persist failed run" in caplog.text


@pytest.mark.anyio
@pytest.mark.parametrize('request_cancelled', [False, True])
async def test_shutdown_during_submission_marks_it_failed(chat_api, monkeypatch, request_cancelled):
    h = chat_api
    entered, release = asyncio.Event(), asyncio.Event()
    create = h.context.events.create_run
    async def blocked_create(run_id):
        entered.set()
        await release.wait()
        await create(run_id)
    monkeypatch.setattr(h.context.events, "create_run", blocked_create)
    request = asyncio.create_task(h.api.post(chat_path(h), json=payload()))
    await asyncio.wait_for(entered.wait(), 2)
    if request_cancelled:
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
    closing = asyncio.create_task(h.manager.close())
    await asyncio.sleep(0)
    assert not closing.done() and len(h.manager.controls) == 1
    release.set()
    if not request_cancelled:
        assert (await request).status_code == 503
    await closing
    async with h.context.repository.db.transaction() as tx:
        run = await tx.scalar(select(Run))
        assert run.status == "failed"
    assert not h.captured and not h.manager.executions and not h.manager.controls
