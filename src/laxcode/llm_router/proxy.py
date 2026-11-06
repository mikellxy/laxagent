import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import anyio
import httpx2
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI
from openai import AsyncAPIResponse

from laxcode.llm_router.config import LLMConfig


class ProxyError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code

    def body(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": str(self), "type": "router_error"}}


class LLMProxy:
    def __init__(self, config: LLMConfig, clients: dict[str, AsyncOpenAI]):
        self.clients = clients
        self.models = {
            f"{provider.name}:{model.name}": (provider.name, model)
            for provider in config.provider_list
            for model in provider.model_list
        }

    @asynccontextmanager
    async def open_stream(self, body: dict[str, Any]) -> AsyncIterator[AsyncAPIResponse]:
        if body.get("stream") is not True:
            raise ProxyError(400, "stream_required", "Only stream=true is supported")
        model_id = body.get("model")
        if not isinstance(model_id, str) or model_id not in self.models:
            raise ProxyError(400, "unknown_model", "A configured provider:model is required")
        provider_name, model = self.models[model_id]
        payload = {**body, "model": model.name, "max_output_tokens": model.limit.output}
        # Forward JSON as JSON, never as SDK keyword options (headers, URL, etc.).
        manager = self.clients[provider_name].responses.with_streaming_response.create(
            stream=True, extra_body=payload
        )
        try:
            response = await manager.__aenter__()
        except APIStatusError as exc:
            raise ProxyError(exc.status_code, "upstream_error", "Upstream rejected the request") from None
        except APITimeoutError:
            raise ProxyError(504, "upstream_timeout", "Upstream request timed out") from None
        except APIConnectionError:
            raise ProxyError(502, "upstream_unavailable", "Cannot connect to upstream") from None

        try:
            content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type != "text/event-stream":
                raise ProxyError(502, "invalid_upstream_response", "Upstream did not return an SSE stream")
            yield response
        finally:
            # Disconnect cancellation must not interrupt upstream connection cleanup.
            with anyio.CancelScope(shield=True):
                await manager.__aexit__(None, None, None)


async def stream_bytes(response: AsyncAPIResponse) -> AsyncIterator[bytes]:
    try:
        async for chunk in response.iter_bytes():
            yield chunk
    except (httpx2.HTTPError, APIConnectionError):
        error = {"type": "error", "code": "upstream_stream_error", "message": "Upstream stream interrupted"}
        yield f"\n\nevent: error\ndata: {json.dumps(error)}\n\n".encode()
