from typing import Annotated

from fastapi import APIRouter, Depends, Path

from laxcode.api.dependencies import get_repository
from laxcode.api.schemas import CreateSessionRequest, SessionResponse

router = APIRouter()


@router.post("/projects/{project_id}/sessions", status_code=201, response_model=SessionResponse)
async def create_session(project_id: Annotated[int, Path(gt=0)], body: CreateSessionRequest,
                         repository=Depends(get_repository)):
    return await repository.create_session(body.user_id, project_id, body.name)
