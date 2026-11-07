from fastapi import APIRouter, Depends

from laxcode.api.dependencies import get_repository
from laxcode.api.schemas import CreateUserRequest, UserResponse

router = APIRouter()


@router.post("/users", status_code=201, response_model=UserResponse)
async def create_user(body: CreateUserRequest, repository=Depends(get_repository)):
    return await repository.create_user(body.user_id)
