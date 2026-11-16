import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx2
import pytest
from langgraph.types import RetryPolicy
from openai import AsyncOpenAI

from laxagent.agent import graph as graph_module
from laxagent.agent.context import AgentContext
from laxagent.models.errors import ModelError
from laxagent.runtime.events import EventStoreError
from model_helpers import ModelStream, client_for, completed


@pytest.fixture
def model_graph(monkeypatch):
    async def prepared(state):
        return {}

    # Exercise the production graph/retry predicate without waiting for backoff.
    monkeypatch.setattr(graph_module, "prepare", prepared)
    monkeypatch.setattr(graph_module, "RetryPolicy", lambda **kwargs: RetryPolicy(
        **{**kwargs, "initial_interval": 0, "jitter": False},
    ))
    return graph_module.build_graph()


async def invoke(graph, client, *, events=None):
    repository = SimpleNamespace(append_message=AsyncMock(return_value=(3, [])))
    context = AgentContext(client, events or SimpleNamespace(publish=AsyncMock()), repository, resume_time=2)
    state = {
        "user_id": "test-user", "session_id": 1, "project_id": 1, "run_id": 1,
        "model": "chosen:model", "messages": [], "model_input": [], "next_msg_seq": 2,
    }
    result = await graph.ainvoke(state, context=context)
    return result, context


@pytest.mark.anyio
@pytest.mark.parametrize("status,code,attempts", [
    (408, None, 3), (429, "rate_limit_exceeded", 3),
    (500, None, 3), (502, None, 3), (503, None, 3), (504, None, 3),
    (400, None, 1), (401, None, 1), (403, None, 1), (404, None, 1), (422, None, 1),
    (429, "insufficient_quota", 1), (429, "billing_hard_limit_reached", 1),
])
async def test_graph_limits_http_failure_attempts(model_graph, status, code, attempts):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx2.Response(status, json={"error": {"code": code, "message": "secret-key"}})

    async with AsyncOpenAI(api_key="placeholder", max_retries=0,
                           http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handle))) as client:
        with pytest.raises(ModelError, match="Model request failed"):
            await invoke(model_graph, client)
    assert len(requests) == attempts


@pytest.mark.anyio
@pytest.mark.parametrize("failure", [httpx2.ConnectError, httpx2.ConnectTimeout])
async def test_connection_failure_recovers_on_second_attempt(model_graph, failure):
    requests = []

    def handle(request):
        requests.append(request)
        if len(requests) == 1:
            raise failure("secret-key", request=request)
        return httpx2.Response(200, headers={"content-type": "text/event-stream"},
                               stream=ModelStream([completed("recovered")]))

    async with AsyncOpenAI(api_key="placeholder", max_retries=0,
                           http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handle))) as client:
        result, _ = await invoke(model_graph, client)
    assert len(requests) == 2 and result["messages"][-1]["content"] == "recovered"


@pytest.mark.anyio
@pytest.mark.parametrize("status,retryable,attempts", [(429, False, 1), (502, False, 1), (503, True, 3)])
async def test_router_retry_classification_survives_sdk_and_graph(model_graph, status, retryable, attempts):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx2.Response(status, json={"error": {
            "type": "router_error", "code": "upstream_error", "message": "Upstream rejected the request",
            "retryable": retryable,
        }})

    async with AsyncOpenAI(api_key="placeholder", max_retries=0,
                           http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handle))) as client:
        with pytest.raises(ModelError):
            await invoke(model_graph, client)
    assert len(requests) == attempts


@pytest.mark.anyio
@pytest.mark.parametrize("tail,attempts", [
    ([], 3),
    ([httpx2.ReadError("secret-key")], 3),
    ([httpx2.ReadTimeout("secret-key")], 3),
    ([httpx2.RemoteProtocolError("secret-key")], 3),
    ([{"type": "error", "code": "upstream_stream_error", "message": "secret-key"}], 3),
    ([{"type": "error", "error": {"code": "server_error", "message": "secret-key"}}], 3),
    ([{"type": "response.failed", "response": {"error": {"code": "server_error"}}}], 3),
    ([{"type": "response.failed", "response": {"error": {"code": "rate_limit_exceeded"}}}], 3),
    ([{"type": "response.failed", "response": {"error": {"code": "insufficient_quota"}}}], 1),
    ([{"type": "response.failed", "response": {"error": {"code": "unknown_error"}}}], 1),
    ([{"type": "response.incomplete", "response": {"incomplete_details": {"reason": "max_output_tokens"}}}], 1),
    ([{"type": "response.incomplete", "response": {"incomplete_details": {"reason": "content_filter"}}}], 1),
    ([{"type": "response.output_text.delta", "delta": None}], 1),
])
async def test_graph_only_retries_transient_stream_failures(model_graph, tail, attempts):
    requests = []
    stream = ModelStream(tail)
    async with client_for(stream, requests) as client:
        with pytest.raises(ModelError) as error:
            await invoke(model_graph, client)
    assert "secret-key" not in str(error.value)
    assert len(requests) == attempts
    assert stream.closed


@pytest.mark.anyio
async def test_third_attempt_saves_only_successful_response_and_reasoning(model_graph):
    broken = lambda: ModelStream([
        {"type": "response.reasoning_text.delta", "delta": "failed thought"},
        {"type": "response.output_item.added", "item": {
            "type": "function_call", "id": "partial", "call_id": "partial", "name": "write_file",
        }},
        {"type": "response.output_text.delta", "delta": "failed text"},
        httpx2.ReadError("secret-key"),
    ])
    streams = [broken(), broken(), ModelStream([
        {"type": "response.reasoning_text.delta", "delta": "successful thought"}, completed("final"),
    ])]
    originals, requests = list(streams), []
    async with client_for(streams, requests) as client:
        result, context = await invoke(model_graph, client)
    assert len(requests) == 3 and requests[0] == requests[1] == requests[2]
    assert all(stream.closed for stream in originals)
    assert result["messages"] == [{"role": "assistant", "content": "final"}]
    assert result["pending_tool_calls"] == []
    context.repository.append_message.assert_awaited_once()
    saved = context.repository.append_message.await_args.kwargs
    assert saved["reasoning"] == [{"channel": "text", "text": "successful thought"}]
    assert saved["content"] == "final"
    assert all(call.args[0]["resume_time"] == 2 for call in context.events.publish.await_args_list)


@pytest.mark.anyio
async def test_event_failure_does_not_retry_model(model_graph):
    requests = []
    async with client_for(ModelStream([
        {"type": "response.output_text.delta", "delta": "hello"}, completed(),
    ]), requests) as client:
        with pytest.raises(EventStoreError):
            await invoke(model_graph, client, events=SimpleNamespace(publish=AsyncMock(side_effect=EventStoreError())))
    assert len(requests) == 1


@pytest.mark.anyio
async def test_cancellation_does_not_retry_model(model_graph):
    ready, requests = asyncio.Event(), []
    source = ModelStream([], wait=ready)
    async with client_for(source, requests) as client:
        task = asyncio.create_task(invoke(model_graph, client))
        await asyncio.wait_for(ready.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(requests) == 1 and source.closed
