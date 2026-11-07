from fastapi import APIRouter, Depends

from laxcode.api.dependencies import get_repository
from laxcode.api.schemas import CreateProjectRequest, ProjectResponse

router = APIRouter()


@router.post("/projects", status_code=201, response_model=ProjectResponse)
async def create_project(body: CreateProjectRequest, repository=Depends(get_repository)):
    return await repository.create_project(body.user_id, body.name)
