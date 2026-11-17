from starlette.concurrency import run_in_threadpool

from fastapi import APIRouter, Depends

from laxagent.api.auth import get_current_user
from laxagent.api.dependencies import get_repository, get_profile
from laxagent.api.schemas import ProjectRequest, ProjectResponse

router = APIRouter()


@router.get("/projects", response_model=list[ProjectResponse])
async def list_projects(user_id=Depends(get_current_user), repository=Depends(get_repository)):
    return await repository.list_projects(user_id)


@router.post("/projects", status_code=201, response_model=ProjectResponse)
async def create_project(body: ProjectRequest, user_id=Depends(get_current_user), repository=Depends(get_repository), profile=Depends(get_profile)):
    binding = await run_in_threadpool(profile.prepare_project, body.container_id, body.container_workspace)
    return await repository.create_project(user_id, body.name, **binding)
