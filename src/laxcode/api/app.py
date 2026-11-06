from contextlib import asynccontextmanager

from fastapi import FastAPI

from laxcode.agent.context import AgentContext
from laxcode.agent.graph import build_graph
from laxcode.api.debug import router as debug_router
from laxcode.api.runs import router as runs_router
from laxcode.config import AppConfig
from laxcode.models.factory import create_client
from laxcode.runtime.events import EventStore


def create_app(config: AppConfig | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if config is None:
            yield
            return
        async with EventStore.connect(config.nats.url) as events:
            async with create_client(config.llm_router.port) as client:
                app.state.agent_context = AgentContext(client, events)
                app.state.graph = build_graph()
                try:
                    yield
                finally:
                    app.state.agent_context = None

    app = FastAPI(title="LaxCode", lifespan=lifespan)
    app.include_router(runs_router, prefix="/api/v1")
    app.include_router(debug_router, prefix="/api/v1")
    return app
