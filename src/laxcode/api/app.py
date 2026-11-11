from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from psycopg import Error as PsycopgError

from laxcode.agent.context import AgentContext
from laxcode.agent.graph import build_graph
from laxcode.api.auth import router as auth_router
from laxcode.api.runs import router as runs_router
from laxcode.api.projects import router as projects_router
from laxcode.api.sessions import router as sessions_router
from laxcode.api.models import router as models_router
from laxcode.api.memories import router as memories_router
from laxcode.api.user_configs import router as user_configs_router
from laxcode.api.frontend import frontend_files
from laxcode.config import AppConfig
from laxcode.models.factory import create_client
from laxcode.runtime.events import EventStore, EventStoreError
from laxcode.runtime.task_manager import TaskManager
from laxcode.storage.database import Database, DatabaseError
from laxcode.storage.repositories import Repository
from laxcode.storage.checkpoints import open_checkpointer
from laxcode.skills import builtin_snapshots


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
            app.state.agent_context = AgentContext(
                client, events, Repository(db), recursion_limit=config.recursion_limit,
                builtin_skills_dir=config.builtin_skills_dir,
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
    app.include_router(auth_router, prefix="/api/v1")
    for router in (projects_router, sessions_router, models_router, memories_router, user_configs_router):
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
