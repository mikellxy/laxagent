"""Server-owned Doubao Search client; credentials never enter graph state."""

import asyncio
import json
from contextlib import asynccontextmanager

import httpx2

from laxagent.config import WebSearchConfig
from laxagent.tools.errors import ToolError


SEARCH_URL = "https://open.feedcoopapi.com/search_api/web_search"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_RESULT_BYTES = 64 * 1024


class WebSearchService:
    def __init__(self, client: httpx2.AsyncClient, timeout_seconds: float):
        self._client = client
        self._timeout_seconds = timeout_seconds
        self._slots = asyncio.Semaphore(5)

    @classmethod
    @asynccontextmanager
    async def open(cls, config: WebSearchConfig):
        async with httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {config.api_key.get_secret_value()}"},
            timeout=config.timeout_seconds, trust_env=False, follow_redirects=False,
        ) as client:
            yield cls(client, config.timeout_seconds)

    async def search(self, body: dict) -> dict:
        try:
            # Includes waiting for a shared slot and the entire response body.
            async with asyncio.timeout(self._timeout_seconds), self._slots:
                async with self._client.stream("POST", SEARCH_URL, json=body) as response:
                    if response.status_code in (401, 403):
                        raise ToolError("SEARCH_AUTH_FAILED")
                    if response.status_code == 429:
                        raise ToolError("SEARCH_RATE_LIMITED")
                    if response.status_code != 200:
                        raise ToolError("SEARCH_UPSTREAM_FAILED")
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(data) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise ToolError("SEARCH_RESPONSE_TOO_LARGE")
                        data.extend(chunk)
            return _normalize(json.loads(data), body["SearchType"], body["Count"])
        except (TimeoutError, httpx2.TimeoutException):
            raise ToolError("SEARCH_TIMEOUT") from None
        except httpx2.HTTPError:
            raise ToolError("SEARCH_UNAVAILABLE") from None
        except (ValueError, TypeError, KeyError):
            raise ToolError("SEARCH_INVALID_RESPONSE") from None


def _text(data: dict, key: str, limit: int) -> tuple[str | None, bool]:
    value = data.get(key)
    if value is None:
        return None, False
    if not isinstance(value, str):
        raise ValueError("Invalid text field")
    return value[:limit], len(value) > limit


def _normalize(data: dict, search_type: str, count: int) -> dict:
    if not isinstance(data, dict):
        raise ValueError("Invalid response")
    metadata = data.get("ResponseMetadata", {})
    if not isinstance(metadata, dict):
        raise ValueError("Invalid metadata")
    error = metadata.get("Error")
    if error:
        if not isinstance(error, dict):
            raise ValueError("Invalid error")
        code = str(error.get("Code") or error.get("CodeN", ""))
        mapped = {
            "invalid_api_key": "SEARCH_AUTH_FAILED", "10403": "SEARCH_AUTH_FAILED",
            "100013": "SEARCH_AUTH_FAILED", "700429": "SEARCH_RATE_LIMITED",
            "10406": "SEARCH_QUOTA_EXCEEDED", "10407": "SEARCH_QUOTA_EXCEEDED",
            "10408": "SEARCH_QUOTA_EXCEEDED", "10409": "SEARCH_PLAN_UNAVAILABLE",
            "10410": "SEARCH_PLAN_UNAVAILABLE", "10412": "SEARCH_QUOTA_EXCEEDED",
            "10400": "SEARCH_INVALID_ARGUMENTS", "10402": "SEARCH_INVALID_ARGUMENTS",
        }
        raise ToolError(mapped.get(code, "SEARCH_UPSTREAM_FAILED"))
    result = data.get("Result")
    if not isinstance(result, dict):
        raise ValueError("Missing result")
    items = result.get("WebResults" if search_type == "web" else "ImageResults")
    if items is None and result.get("ResultCount") == 0:
        items = []
    if not isinstance(items, list):
        raise ValueError("Missing results list")
    request_id, _ = _text(metadata, "RequestId", 128)
    output = {"search_type": search_type, "request_id": request_id, "results": [], "truncated": len(items) > count}
    for item in items[:count]:
        if not isinstance(item, dict):
            raise ValueError("Invalid search item")
        entry = {}
        for source, target, limit in (
            ("Title", "title", 512), ("Url", "url", 2048), ("SiteName", "site_name", 256),
            ("PublishTime", "publish_time", 64), ("AuthInfoDes", "authority", 128),
        ):
            entry[target], truncated = _text(item, source, limit)
            output["truncated"] |= truncated
        if search_type == "web":
            summary_key = "Summary" if item.get("Summary") else "Snippet"
            entry["summary"], entry["summary_truncated"] = _text(item, summary_key, 2000)
            output["truncated"] |= entry["summary_truncated"]
        else:
            image = item.get("Image")
            if not isinstance(image, dict):
                raise ValueError("Missing image")
            entry["image_url"], truncated = _text(image, "Url", 2048)
            if not entry["image_url"]:
                raise ValueError("Missing image URL")
            output["truncated"] |= truncated
            for source in ("Width", "Height"):
                value = image.get(source)
                if value is not None and (type(value) is not int or value < 0):
                    raise ValueError("Invalid image size")
                entry[source.lower()] = value
        output["results"].append(entry)
        if len(json.dumps(output, ensure_ascii=False).encode("utf-8")) > MAX_RESULT_BYTES:
            output["results"].pop()
            output["truncated"] = True
            break
    return output
