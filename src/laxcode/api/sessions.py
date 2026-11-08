from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query

from laxcode.api.auth import get_current_user
from laxcode.api.dependencies import get_repository
from laxcode.api.schemas import (
    CreateSessionRequest, MessagePageResponse, NameRequest, RunResponse, SessionDetailResponse, SessionResponse,
)

router = APIRouter()
ResourceId = Annotated[int, Path(gt=0)]


@router.get("/projects/{project_id}/sessions", response_model=list[SessionResponse])
async def list_sessions(project_id: ResourceId, user_id=Depends(get_current_user), repository=Depends(get_repository)):
    return await repository.list_sessions(user_id, project_id)


@router.post("/projects/{project_id}/sessions", status_code=201, response_model=SessionResponse)
async def create_session(project_id: ResourceId, body: CreateSessionRequest | None = None,
                         user_id=Depends(get_current_user), repository=Depends(get_repository)):
    return await repository.create_session(user_id, project_id)


@router.patch("/sessions/{session_id}", response_model=SessionResponse)
async def rename_session(session_id: ResourceId, body: NameRequest,
                         user_id=Depends(get_current_user), repository=Depends(get_repository)):
    return await repository.rename_session(user_id, session_id, body.name)


@router.get("/sessions/{session_id}", response_model=SessionDetailResponse)
async def session_detail(session_id: ResourceId, user_id=Depends(get_current_user), repository=Depends(get_repository)):
    session, run = await repository.session_detail(user_id, session_id)
    return {**SessionResponse.model_validate(session).model_dump(),
            "latest_run": RunResponse.model_validate(run) if run else None}


@router.get("/sessions/{session_id}/messages", response_model=MessagePageResponse)
async def list_messages(session_id: ResourceId,
                        before_seq: Annotated[int | None, Query(gt=0)] = None,
                        limit: Annotated[int, Query(ge=1, le=100)] = 50,
                        user_id=Depends(get_current_user), repository=Depends(get_repository)):
    return await repository.list_messages(user_id, session_id, before_seq=before_seq, limit=limit)
