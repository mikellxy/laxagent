import httpx2
import pytest

from laxcode.agent.context import AgentContext
from laxcode.agent.graph import build_graph
from laxcode.api.app import create_app
from laxcode.runtime.events import EventStoreError
from test_agent import ModelStream, client_for, completed


def body(run_id):
    return {"user_query": "hello", "model": "chosen:model", "run_id": run_id,
            "user_id": "debug", "project_id": 1, "session_id": 1}


@pytest.mark.anyio
async def test_debug_invoke_replay_and_duplicate(run_store):
    store, run_id = run_store
    captured = []
    stream = ModelStream([
        {"type": "response.created", "response": {"id": "resp_test"}},
        {"type": "response.output_text.delta", "delta": "hello"}, completed(),
    ])
    app = create_app()
    async with client_for(stream, captured) as model_client:
        app.state.agent_context = AgentContext(model_client, store)
        app.state.graph = build_graph()
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app), base_url="http://test", trust_env=False) as api:
            response = await api.post("/api/v1/debug/agent/invoke", json=body(run_id))
            assert response.status_code == 200, response.text
            result = response.json()
            assert result["model"] == "chosen:model"
            assert result["next_msg_id"] == 0
            assert [m["role"] for m in result["messages"]] == ["system", "user", "assistant"]
            assert result["messages"][-1]["content"] == "hello"
            assert captured[0]["model"] == "chosen:model"
            first = await api.get(f"/api/v1/debug/runs/{run_id}/events")
            second = await api.get(f"/api/v1/debug/runs/{run_id}/events")
            assert first.content == second.content
            assert first.headers["content-type"].startswith("text/event-stream")
            assert first.text.count("event:") == 4
            duplicate = await api.post("/api/v1/debug/agent/invoke", json=body(run_id))
            assert duplicate.status_code == 409
            assert len(captured) == 1
            missing = await api.get(f"/api/v1/debug/runs/{run_id + 1}/events")
            assert missing.status_code == 404
    assert (await store.js.stream_info(store.stream_name(run_id))).state.consumer_count == 0


@pytest.mark.anyio
@pytest.mark.parametrize("change", [
    lambda b: b.pop("model"),
    lambda b: b.update(model="no-provider"),
    lambda b: b.update(user_query="   "),
    lambda b: b.update(user_id=""),
    lambda b: b.update(run_id=True),
    lambda b: b.update(project_id=0),
    lambda b: b.update(session_id="1"),
])
async def test_debug_input_validation(change):
    payload = body(1)
    change(payload)
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(create_app()), base_url="http://test", trust_env=False) as api:
        result = await api.post("/api/v1/debug/agent/invoke", json=payload)
        assert result.status_code == 422


@pytest.mark.anyio
async def test_uninitialized_runtime_returns_run_id():
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(create_app()), base_url="http://test", trust_env=False) as api:
        result = await api.post("/api/v1/debug/agent/invoke", json=body(1))
        assert result.status_code == 503
        assert result.json()["detail"]["run_id"] == 1


@pytest.mark.anyio
async def test_debug_model_error_returns_502_and_error_stream(run_store):
    store, run_id = run_store
    app = create_app()
    async with client_for(None, [], status=400) as model_client:
        app.state.agent_context = AgentContext(model_client, store)
        app.state.graph = build_graph()
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app), base_url="http://test", trust_env=False) as api:
            result = await api.post("/api/v1/debug/agent/invoke", json=body(run_id))
            assert result.status_code == 502
            assert result.json()["detail"]["run_id"] == run_id
            stream = await api.get(f"/api/v1/debug/runs/{run_id}/events")
            assert '"status": "failed"' in stream.text
            assert "secret-key" not in result.text + stream.text


@pytest.mark.anyio
async def test_debug_storage_error_returns_503(monkeypatch, run_store):
    store, run_id = run_store

    async def fail(run_id):
        raise EventStoreError()

    monkeypatch.setattr(store, "create_run", fail)
    app = create_app()
    app.state.agent_context = AgentContext(None, store)
    app.state.graph = build_graph()
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app), base_url="http://test", trust_env=False) as api:
        result = await api.post("/api/v1/debug/agent/invoke", json=body(run_id))
        assert result.status_code == 503
        assert result.json()["detail"]["run_id"] == run_id
