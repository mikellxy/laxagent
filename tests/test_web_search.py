import asyncio
import json
from dataclasses import replace

import httpx2
import pytest

from laxcode.config import load_config
from laxcode.tools.executor import execute_tool
from laxcode.tools.registry import tool_definitions
from laxcode.tools.results import format_result
from laxcode.web_search import MAX_RESPONSE_BYTES, MAX_RESULT_BYTES, SEARCH_URL, WebSearchService
from tool_helpers import tool_context


def response(items=None):
    return {"ResponseMetadata": {"RequestId": "search-request"}, "Result": {
        "WebResults": items if items is not None else [{
            "Title": "文档", "Url": "https://example.com/docs", "Summary": "相关摘要",
            "Snippet": "短片段", "PublishTime": "2026-11-13T00:00:00Z",
        }],
    }}


async def invoke(tmp_path, handler, args=None, timeout=1):
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
        context = replace(tool_context(tmp_path), web_search=WebSearchService(client, timeout))
        return await execute_tool("web_search", json.dumps(args or {"query": "测试"}), context=context)


def test_config_and_disabled_schema(config_data, tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config_data))
    assert load_config(path).web_search is None
    assert "web_search" not in {item["name"] for item in tool_definitions()}
    config_data["web_search"] = {"api_key": "search-secret"}
    path.write_text(json.dumps(config_data))
    config = load_config(path)
    assert config.web_search.timeout_seconds == 30
    assert "search-secret" not in repr(config)
    assert "search-secret" not in config.model_dump_json()
    definition = next(item for item in tool_definitions(web_search_enabled=True) if item["name"] == "web_search")
    schema = definition["parameters"]
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["additionalProperties"] is False
    assert "api_key" not in json.dumps(definition)


@pytest.mark.parametrize("settings", [
    {"api_key": ""}, {"api_key": " "}, {"api_key": "search-secret\n"}, {"api_key": "\x00"},
    {"api_key": "search-secret", "timeout_seconds": 0},
    {"api_key": "search-secret", "timeout_seconds": float("inf")},
    {"api_key": "search-secret", "timeout_seconds": True},
])
def test_invalid_config_is_safe(config_data, tmp_path, settings):
    config_data["web_search"] = settings
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config_data))
    with pytest.raises(ValueError) as exc:
        load_config(path)
    assert "search-secret" not in str(exc.value)


@pytest.mark.anyio
async def test_disabled_execution(tmp_path):
    result = await execute_tool("web_search", '{"query":"test"}', context=tool_context(tmp_path))
    assert result["error_code"] == "UNKNOWN_TOOL"


@pytest.mark.anyio
async def test_request_and_structured_results(tmp_path):
    def handler(request):
        assert str(request.url) == SEARCH_URL and request.method == "POST"
        assert json.loads(request.content) == {
            "Query": "文档", "SearchType": "web", "Count": 3,
            "TimeRange": "2026-01-01..2026-11-13", "Filter": {"NeedUrl": True, "AuthInfoLevel": 1},
            "QueryControl": {"QueryRewrite": True},
        }
        return httpx2.Response(200, json=response())
    result = await invoke(tmp_path, handler, {"query": " 文档 ", "count": 3,
        "time_range": "2026-01-01..2026-11-13", "authoritative_only": True, "query_rewrite": True})
    assert result["error_code"] is None
    item = result["result"]["results"][0]
    assert item["summary"] == "相关摘要" and item["url"] == "https://example.com/docs"
    assert item["publish_time"] == "2026-11-13T00:00:00Z"
    assert result["result"]["request_id"] == "search-request"


@pytest.mark.anyio
async def test_unrestricted_time_is_not_forwarded(tmp_path):
    def handler(request):
        body = json.loads(request.content)
        assert "TimeRange" not in body
        assert body["Filter"] == {"NeedUrl": True}
        return httpx2.Response(200, json=response())
    result = await invoke(tmp_path, handler, {"query": "文档", "time_range": "AnyTime"})
    assert result["error_code"] is None


@pytest.mark.anyio
async def test_client_credentials_and_lifecycle(config_data, tmp_path, monkeypatch):
    config_data["web_search"] = {"api_key": "search-secret", "timeout_seconds": 1}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config_data))
    original = httpx2.AsyncClient
    clients = []
    def handler(request):
        assert request.headers["Authorization"] == "Bearer search-secret"
        assert "search-secret" not in request.content.decode()
        return httpx2.Response(200, json=response())
    def factory(**kwargs):
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        client = original(**kwargs, transport=httpx2.MockTransport(handler))
        clients.append(client)
        return client
    monkeypatch.setattr(httpx2, "AsyncClient", factory)
    async with WebSearchService.open(load_config(path).web_search) as service:
        result = await service.search({"Query": "test", "SearchType": "web", "Count": 5})
        assert "search-secret" not in json.dumps(result)
        assert not clients[0].is_closed
    assert clients[0].is_closed


@pytest.mark.anyio
@pytest.mark.parametrize("args", [
    {"query": " "}, {"query": "x" * 101}, {"query": "x", "count": True},
    {"query": "x", "api_key": "secret"}, {"query": "x", "count": 0},
    {"query": "x", "count": 51}, {"query": "x", "time_range": "2026-02-30..2026-03-01"},
    {"query": "x", "time_range": "2026-11-13..2026-01-01"},
    {"query": "x", "time_range": "null"},
    {"query": "x", "time_range": None},
    {"query": "x", "time_range": "2026-W01-01..2026-W02-01"},
    {"query": "x", "search_type": "image", "count": 6},
    {"query": "x", "search_type": "image", "time_range": "OneDay"},
    {"query": "x", "search_type": "image", "authoritative_only": True},
])
async def test_invalid_arguments_never_send_request(tmp_path, args):
    def handler(request):
        pytest.fail("Invalid arguments reached the network")
    assert (await invoke(tmp_path, handler, args))["error_code"] == "INVALID_ARGUMENTS"


@pytest.mark.anyio
@pytest.mark.parametrize("status,payload,code", [
    (401, {}, "SEARCH_AUTH_FAILED"), (403, {}, "SEARCH_AUTH_FAILED"),
    (429, {}, "SEARCH_RATE_LIMITED"), (503, {}, "SEARCH_UPSTREAM_FAILED"),
    (302, {}, "SEARCH_UPSTREAM_FAILED"),
    (200, {"ResponseMetadata": {"Error": {"Code": "10403", "Message": "secret"}}}, "SEARCH_AUTH_FAILED"),
    (200, {"ResponseMetadata": {"Error": {"CodeN": 10412, "Message": "secret"}}}, "SEARCH_QUOTA_EXCEEDED"),
    (200, {"ResponseMetadata": {"Error": {"Code": "new-error", "Message": "secret"}}}, "SEARCH_UPSTREAM_FAILED"),
    (200, {}, "SEARCH_INVALID_RESPONSE"), (200, [], "SEARCH_INVALID_RESPONSE"),
    (200, {"Result": None}, "SEARCH_INVALID_RESPONSE"),
    (200, {"ResponseMetadata": [], "Result": {}}, "SEARCH_INVALID_RESPONSE"),
    (200, response(["invalid"]), "SEARCH_INVALID_RESPONSE"),
])
async def test_safe_errors(tmp_path, status, payload, code):
    result = await invoke(tmp_path, lambda _: httpx2.Response(status, json=payload))
    assert result == {"error_code": code, "result": {}}
    assert "secret" not in format_result(result)


@pytest.mark.anyio
async def test_empty_images_fallback_and_bounds(tmp_path):
    empty = await invoke(tmp_path, lambda _: httpx2.Response(200, json={"Result": {"ResultCount": 0}}))
    assert empty["error_code"] is None and empty["result"]["results"] == []
    image = await invoke(tmp_path, lambda _: httpx2.Response(200, json={"Result": {"ImageResults": [
        {"Title": "图片", "Image": {"Url": "https://example.com/image.png", "Width": 100, "Height": 200}},
    ]}}), {"query": "图片", "search_type": "image"})
    assert image["result"]["results"][0]["image_url"] == "https://example.com/image.png"
    snippet = await invoke(tmp_path, lambda _: httpx2.Response(200, json=response([{"Snippet": "片段"}])))
    assert snippet["result"]["results"][0]["summary"] == "片段"
    large = response([{"Title": "标题", "Summary": "中" * 3000}] * 50)
    bounded = await invoke(tmp_path, lambda _: httpx2.Response(200, json=large), {"query": "x", "count": 50})
    assert bounded["result"]["truncated"] is True
    assert bounded["result"]["results"][0]["summary_truncated"] is True
    assert len(json.dumps(bounded["result"], ensure_ascii=False).encode()) <= MAX_RESULT_BYTES
    huge = await invoke(tmp_path, lambda _: httpx2.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1)))
    assert huge["error_code"] == "SEARCH_RESPONSE_TOO_LARGE"
    invalid = await invoke(tmp_path, lambda _: httpx2.Response(200, content=b"not json secret"))
    assert invalid["error_code"] == "SEARCH_INVALID_RESPONSE"


@pytest.mark.anyio
async def test_timeout_and_cancellation_close_response(tmp_path):
    entered, closed = asyncio.Event(), asyncio.Event()
    class SlowBody(httpx2.AsyncByteStream):
        async def __aiter__(self):
            entered.set()
            await asyncio.Event().wait()
            yield b""

        async def aclose(self):
            closed.set()

    def handler(request):
        return httpx2.Response(200, stream=SlowBody())
    timed_out = await invoke(tmp_path, handler, timeout=0.02)
    assert timed_out["error_code"] == "SEARCH_TIMEOUT" and closed.is_set()
    entered.clear()
    closed.clear()
    task = asyncio.create_task(invoke(tmp_path, handler))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()


@pytest.mark.anyio
async def test_network_failure_does_not_leak_or_retry(tmp_path):
    calls = 0
    def handler(request):
        nonlocal calls
        calls += 1
        raise httpx2.ConnectError("secret", request=request)
    result = await invoke(tmp_path, handler)
    assert result == {"error_code": "SEARCH_UNAVAILABLE", "result": {}}
    assert calls == 1
