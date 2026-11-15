"""Unauthenticated evaluation endpoints served only by the loopback eval listener."""

from contextlib import asynccontextmanager
from tempfile import TemporaryFile
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Form, UploadFile
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.concurrency import run_in_threadpool

from laxagent.api.dependencies import get_repository
from laxagent.api.workspace_files import FileDownload
from laxagent.eval_api.archive import write_archive
from laxagent.eval_api.store import EvalStore
from laxagent.eval_api.initialize import initialize_workspace, PackageError
from laxagent.layout import get_project_workdir


class CreateSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    session_name: str = Field(min_length=1)

    @field_validator("session_name")
    @classmethod
    def trim_name(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Session name must not be blank")
        return value


class CreateSessionResponse(BaseModel):
    user_id: str
    project_id: int
    session_id: int


class CollectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    session_id: int = Field(gt=0)
    run_id: int = Field(gt=0)


class InitSessionResponse(BaseModel):
    session_id: int
    files: list[str]


def get_eval_store(repository=Depends(get_repository)):
    return EvalStore(repository.db)


def create_eval_app(api_app: FastAPI) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # The supervisor starts API first and stops eval first. Resources are
        # owned and closed by API, never duplicated by this third listener.
        if (getattr(api_app.state, "agent_context", None) is None
                or getattr(api_app.state, "task_manager", None) is None):
            raise RuntimeError("API runtime must start before the eval listener")
        app.state.config = api_app.state.config
        app.state.agent_context = api_app.state.agent_context
        app.state.task_manager = api_app.state.task_manager
        try:
            yield
        finally:
            app.state.agent_context = None
            app.state.task_manager = None

    app = FastAPI(title="LaxAgent Eval", lifespan=lifespan)
    app.exception_handlers.update(api_app.exception_handlers)

    @app.post("/eval/create_session", status_code=201, response_model=CreateSessionResponse)
    async def create_session(body: CreateSessionRequest, store=Depends(get_eval_store)):
        session = await store.create_session(body.session_name)
        return {"user_id": session.user_id, "project_id": session.project_id, "session_id": session.id}

    @app.post("/eval/init_session", response_model=InitSessionResponse)
    async def init_session(session_id: Annotated[int, Form(gt=0)], package: UploadFile,
                           store=Depends(get_eval_store)):
        try:
            async with store.initialization(session_id) as session:
                files = await run_in_threadpool(
                    initialize_workspace, package.file,
                    get_project_workdir(session.user_id, session.project_id),
                )
            return {"session_id": session.id, "files": files}
        except PackageError as exc:
            raise HTTPException(exc.status_code, str(exc)) from None
        except OSError:
            raise HTTPException(503, "Workspace could not be initialized") from None
        finally:
            await package.close()

    @app.post("/eval/collect", status_code=200, response_class=FileDownload, responses={
        200: {"content": {"application/zip": {"schema": {"type": "string", "format": "binary"}}}},
    })
    async def collect(body: CollectRequest, store=Depends(get_eval_store)):
        file = TemporaryFile(mode="w+b")
        try:
            async with store.snapshot(body.session_id, body.run_id) as (session, messages):
                await run_in_threadpool(write_archive, file, session.user_id, session.project_id, messages)
            response = FileDownload(file, f"eval-{body.session_id}-{body.run_id}.zip")
            response.headers["content-type"] = "application/zip"
            return response
        except OSError:
            file.close()
            raise HTTPException(503, "Evidence archive could not be created") from None
        except BaseException:
            file.close()
            raise

    return app
