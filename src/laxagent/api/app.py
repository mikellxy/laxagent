from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from psycopg import Error as PsycopgError

from laxagent.agent.context import AgentContext
from laxagent.agent.graph import build_graph
from laxagent.api.auth import router as auth_router
from laxagent.api.appendices import router as appendices_router
from laxagent.api.workspace_files import router as workspace_files_router
from laxagent.api.runs import router as runs_router
from laxagent.api.projects import router as projects_router
from laxagent.api.sessions import router as sessions_router
from laxagent.api.models import router as models_router
from laxagent.api.memories import router as memories_router
from laxagent.api.user_configs import router as user_configs_router
from laxagent.api.frontend import frontend_files
from laxagent.config import AppConfig
from laxagent.models.factory import create_client
from laxagent.runtime.events import EventStore, EventStoreError
from laxagent.runtime.task_manager import TaskManager
from laxagent.storage.database import Database, DatabaseError
from laxagent.storage.repositories import Repository
from laxagent.storage.checkpoints import open_checkpointer
from laxagent.skills import builtin_snapshots
from laxagent.web_search import WebSearchService


def create_app(config: AppConfig | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if config is None:
            yield
            return
        builtin_snapshots(config.builtin_skills_dir)
        async with AsyncExitStack() as stack:
            db = await stack.enter_async_context(Database.open(config.database))
            checkpointer = await stack.enter_async_context(open_checkpointer(config.database))
            events = await stack.enter_async_context(EventStore.connect(config.nats.url))
            client = await stack.enter_async_context(create_client(config.llm_router.port))
            web_search = (await stack.enter_async_context(WebSearchService.open(config.web_search))
                          if config.web_search is not None else None)
            app.state.agent_context = AgentContext(
                client, events, Repository(db), recursion_limit=config.recursion_limit,
                builtin_skills_dir=config.builtin_skills_dir,
                web_search=web_search,
            )
            app.state.graph = build_graph(checkpointer=checkpointer)
            app.state.task_manager = TaskManager(app.state.graph, app.state.agent_context,
                                                 cancel_timeout=config.cancel_timeout_seconds)
            await app.state.task_manager.start()
            try:
                yield
            finally:
                await app.state.task_manager.close()
                app.state.task_manager = None
                app.state.agent_context = None

    app = FastAPI(title="LaxCode", lifespan=lifespan)
    app.state.config = config
    app.include_router(runs_router, prefix="/api/v1")
    app.include_router(workspace_files_router, prefix="/api/v1")
    app.include_router(auth_router, prefix="/api/v1")
    for router in (projects_router, sessions_router, models_router, memories_router, user_configs_router, appendices_router):
        app.include_router(router, prefix="/api/v1")

    @app.exception_handler(DatabaseError)
    @app.exception_handler(EventStoreError)
    async def database_error(request, exc):
        return JSONResponse({"detail": {"error": str(exc)}}, status_code=exc.status)

    @app.exception_handler(PsycopgError)
    async def checkpoint_error(request, exc):
        return JSONResponse({"detail": {"error": "Database unavailable"}}, status_code=503)

    if config is not None and config.frontend is not None:
        app.mount("/", frontend_files(Path(config.frontend.dist_dir)))

    return app
