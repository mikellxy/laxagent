from fastapi import APIRouter, Depends

from laxcode.api.auth import get_current_user
from laxcode.api.dependencies import get_repository
from laxcode.api.schemas import UserConfigRequest, UserConfigsResponse

router = APIRouter()


@router.get("/user-configs", response_model=UserConfigsResponse)
async def get_user_configs(user_id=Depends(get_current_user), repository=Depends(get_repository)):
    return {"configs": await repository.get_user_configs(user_id)}


@router.patch("/user-configs", response_model=UserConfigsResponse)
async def update_user_config(body: UserConfigRequest, user_id=Depends(get_current_user),
                             repository=Depends(get_repository)):
    return {"configs": await repository.update_user_config(user_id, body.key, body.value)}
