import json
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from openai import AsyncAPIResponse, AsyncOpenAI, DefaultAsyncHttpx2Client

from laxcode.llm_router.config import LLMConfig
from laxcode.llm_router.proxy import LLMProxy, ProxyError, stream_bytes


def create_router_app(config: LLMConfig) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with AsyncExitStack() as stack:
            clients = {}
            for provider in config.provider_list:
                clients[provider.name] = await stack.enter_async_context(
                    AsyncOpenAI(
                        base_url=str(provider.base_url),
                        api_key=provider.api_key.get_secret_value(),
                        max_retries=0,
                        timeout=300.0,
                        http_client=DefaultAsyncHttpx2Client(follow_redirects=False),
                    )
                )
            app.state.proxy = LLMProxy(config, clients)
            yield

    app = FastAPI(title="LaxCode LLM Router", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(ProxyError)
    async def proxy_error(request: Request, exc: ProxyError):
        return JSONResponse(exc.body(), status_code=exc.status)

    async def upstream(request: Request) -> AsyncIterator[AsyncAPIResponse]:
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeError):
            raise ProxyError(400, "invalid_json", "A JSON object is required") from None
        if not isinstance(body, dict):
            raise ProxyError(400, "invalid_json", "A JSON object is required")
        async with request.app.state.proxy.open_stream(body) as response:
            yield response

    @app.post("/v1/responses")
    async def responses(response: Annotated[AsyncAPIResponse, Depends(upstream, scope="request")]):
        return StreamingResponse(
            stream_bytes(response),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app
