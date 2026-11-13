from fastapi import APIRouter, Depends

from laxagent.api.auth import get_current_user
from laxagent.api.dependencies import get_repository
from laxagent.api.schemas import NameRequest, ProjectResponse

router = APIRouter()


@router.get("/projects", response_model=list[ProjectResponse])
async def list_projects(user_id=Depends(get_current_user), repository=Depends(get_repository)):
    return await repository.list_projects(user_id)


@router.post("/projects", status_code=201, response_model=ProjectResponse)
async def create_project(body: NameRequest, user_id=Depends(get_current_user), repository=Depends(get_repository)):
    return await repository.create_project(user_id, body.name)
