from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from laxcode.agent.context import AgentContext
from laxcode.agent.graph import build_graph
from laxcode.api.debug import router as debug_router
from laxcode.api.runs import router as runs_router
from laxcode.api.users import router as users_router
from laxcode.api.projects import router as projects_router
from laxcode.api.sessions import router as sessions_router
from laxcode.config import AppConfig
from laxcode.models.factory import create_client
from laxcode.runtime.events import EventStore
from laxcode.storage.database import Database, DatabaseError
from laxcode.storage.repositories import Repository


def create_app(config: AppConfig | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if config is None:
            yield
            return
        async with AsyncExitStack() as stack:
            db = await stack.enter_async_context(Database.open(config.database))
            events = await stack.enter_async_context(EventStore.connect(config.nats.url))
            client = await stack.enter_async_context(create_client(config.llm_router.port))
            app.state.agent_context = AgentContext(client, events, Repository(db))
            app.state.graph = build_graph()
            try:
                yield
            finally:
                app.state.agent_context = None

    app = FastAPI(title="LaxCode", lifespan=lifespan)
    app.include_router(runs_router, prefix="/api/v1")
    app.include_router(debug_router, prefix="/api/v1")
    for router in (users_router, projects_router, sessions_router):
        app.include_router(router, prefix="/api/v1")

    @app.exception_handler(DatabaseError)
    async def database_error(request, exc):
        return JSONResponse({"detail": {"error": str(exc)}}, status_code=exc.status)

    return app
