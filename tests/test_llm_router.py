import asyncio
import copy
import json
from types import SimpleNamespace

import httpx2
import pytest
from fastapi.testclient import TestClient

from laxcode.llm_router import app as router_module
from laxcode.llm_router.config import LLMConfig


SSE = (
    b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"hello"}\n\n'
    b'event: response.function_call_arguments.delta\ndata: {"type":"response.function_call_arguments.delta","delta":"{}"}\n\n'
    b'event: response.completed\ndata: {"type":"response.completed","response":{"model":"model-name","usage":{"output_tokens":2}}}\n\n'
)


class TrackedStream(httpx2.AsyncByteStream):
    def __init__(self, mode="normal"):
        self.mode = mode
        self.closed = False

    async def __aiter__(self):
        yield SSE[:20]
        if self.mode == "error":
            raise httpx2.ReadError("sk-private-test should never escape")
        if self.mode == "wait":
            await asyncio.Event().wait()
        yield SSE[20:]

    async def aclose(self):
        self.closed = True


@pytest.fixture
def upstream(monkeypatch):
    state = SimpleNamespace(requests=[], streams=[], clients=[], mode="normal", status=200, content_type="text/event-stream")

    async def handle(request):
        state.requests.append(request)
        if state.mode == "timeout":
            raise httpx2.ConnectTimeout("sk-private-test", request=request)
        if state.mode == "connect_error":
            raise httpx2.ConnectError("sk-private-test", request=request)
        if state.status != 200:
            return httpx2.Response(state.status, json={"error": {"message": "sk-private-test"}})
        stream = TrackedStream(state.mode)
        state.streams.append(stream)
        return httpx2.Response(200, headers={"content-type": state.content_type}, stream=stream)

    def client_factory(**kwargs):
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(handle), trust_env=False, **kwargs)
        state.clients.append(client)
        return client

    monkeypatch.setattr(router_module, "DefaultAsyncHttpx2Client", client_factory)
    yield state
    assert all(client.is_closed for client in state.clients)
    assert all(stream.closed for stream in state.streams)


def test_routes_provider_credentials_and_preserves_json_and_sse(config_data, upstream):
    second = copy.deepcopy(config_data["llm"]["provider_list"][0])
    second.update(name="second", base_url="https://second.example/v1", api_key="second-key")
    config_data["llm"]["provider_list"].append(second)
    app = router_module.create_router_app(LLMConfig.model_validate(config_data["llm"]))
    with TestClient(app) as client:
        for provider, key, url in [
            ("example", "sk-private-test", "https://provider.example.com/v1/responses"),
            ("second", "second-key", "https://second.example/v1/responses"),
        ]:
            body = {
                "model": f"{provider}:model-name", "stream": True, "input": "hi",
                "tools": [{"type": "function", "name": "test", "parameters": {}}],
                "temperature": None, "provider_extension": {"enabled": True},
                "extra_headers": {"Authorization": "must-stay-in-json"},
            }
            response = client.post("/v1/responses", json=body, headers={"Authorization": "Bearer placeholder"})
            assert response.status_code == 200
            assert response.content == SSE
            request = upstream.requests[-1]
            assert str(request.url) == url
            assert request.headers["authorization"] == f"Bearer {key}"
            assert json.loads(request.content) == {**body, "model": "model-name", "max_output_tokens": 16000}


@pytest.mark.parametrize("body", [
    [], {}, {"model": "example:model-name", "stream": False},
    {"model": "example:model-name", "stream": 1},
    {"model": "missing:model-name", "stream": True},
])
def test_rejects_invalid_requests_before_upstream(config_data, upstream, body):
    with TestClient(router_module.create_router_app(LLMConfig.model_validate(config_data["llm"]))) as client:
        response = client.post("/v1/responses", json=body)
    assert response.status_code == 400
    assert "error" in response.json()
    assert not upstream.requests


@pytest.mark.parametrize("requested_output", [16, 64, "ignored"])
def test_output_limit_always_comes_from_config(config_data, upstream, requested_output):
    config_data["llm"]["provider_list"][0]["model_list"][0]["limit"]["output"] = 32
    with TestClient(router_module.create_router_app(LLMConfig.model_validate(config_data["llm"]))) as client:
        response = client.post("/v1/responses", json={
            "model": "example:model-name", "stream": True,
            "max_output_tokens": requested_output,
        })
    assert response.status_code == 200
    assert json.loads(upstream.requests[0].content)["max_output_tokens"] == 32


@pytest.mark.parametrize("status", [401, 429, 500])
def test_upstream_http_errors_are_sanitized_and_not_retried(config_data, upstream, status):
    upstream.status = status
    with TestClient(router_module.create_router_app(LLMConfig.model_validate(config_data["llm"]))) as client:
        response = client.post("/v1/responses", json={"model": "example:model-name", "stream": True})
    assert response.status_code == status
    assert "sk-private-test" not in response.text
    assert response.json()["error"]["code"] == "upstream_error"
    assert len(upstream.requests) == 1


def test_transport_and_midstream_errors(config_data, upstream):
    with TestClient(router_module.create_router_app(LLMConfig.model_validate(config_data["llm"]))) as client:
        for mode, status in [("timeout", 504), ("connect_error", 502)]:
            upstream.mode = mode
            response = client.post("/v1/responses", json={"model": "example:model-name", "stream": True})
            assert response.status_code == status
            assert "sk-private-test" not in response.text
        upstream.mode = "error"
        response = client.post("/v1/responses", json={"model": "example:model-name", "stream": True})
        assert b"upstream_stream_error" in response.content
        assert b"response.completed" not in response.content
        assert "sk-private-test" not in response.text


def test_malformed_json_and_non_sse_upstream(config_data, upstream):
    with TestClient(router_module.create_router_app(LLMConfig.model_validate(config_data["llm"]))) as client:
        response = client.post("/v1/responses", content=b'{"bad":', headers={"content-type": "application/json"})
        assert response.status_code == 400
        assert not upstream.requests
        upstream.content_type = "application/json"
        response = client.post("/v1/responses", json={"model": "example:model-name", "stream": True})
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "invalid_upstream_response"


def test_downstream_disconnect_closes_upstream(config_data, upstream):
    upstream.mode = "wait"
    app = router_module.create_router_app(LLMConfig.model_validate(config_data["llm"]))

    async def exercise():
        sent_body = False
        disconnect = asyncio.Event()

        async def receive():
            nonlocal sent_body
            if not sent_body:
                sent_body = True
                return {"type": "http.request", "body": b'{"model":"example:model-name","stream":true}'}
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body":
                disconnect.set()

        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1", "method": "POST", "scheme": "http",
            "path": "/v1/responses", "raw_path": b"/v1/responses", "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
            "client": ("127.0.0.1", 1234), "server": ("127.0.0.1", 8001),
        }
        async with app.router.lifespan_context(app):
            await asyncio.wait_for(app(scope, receive, send), timeout=2)
            assert upstream.streams[0].closed

    asyncio.run(exercise())
