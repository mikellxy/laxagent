from datetime import datetime, timedelta, timezone
from typing import Annotated
from uuid import UUID

import jwt
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from laxcode.api.dependencies import get_config, get_repository
from laxcode.api.schemas import LoginRequest, TokenResponse

router = APIRouter()
bearer = HTTPBearer(auto_error=False)
TOKEN_SECONDS = 24 * 60 * 60


def unauthorized():
    return HTTPException(401, "Invalid or missing credentials", headers={"WWW-Authenticate": "Bearer"})


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    config=Depends(get_config), repository=Depends(get_repository),
) -> str:
    if credentials is None:
        raise unauthorized()
    try:
        payload = jwt.decode(credentials.credentials, config.auth.jwt_secret.get_secret_value(),
                             algorithms=["HS256"], options={"require": ["sub", "iat", "exp"]})
        user_id = str(UUID(payload["sub"]))
    except (jwt.InvalidTokenError, ValueError, TypeError, AttributeError):
        raise unauthorized() from None
    if not await repository.user_exists(user_id):
        raise unauthorized()
    return user_id


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, config=Depends(get_config), repository=Depends(get_repository)):
    initial_session_id = await repository.ensure_user(body.user_id, guest=body.from_page == "guest_login")
    now = datetime.now(timezone.utc)
    token = jwt.encode({"sub": body.user_id, "iat": now, "exp": now + timedelta(seconds=TOKEN_SECONDS)},
                       config.auth.jwt_secret.get_secret_value(), algorithm="HS256")
    return {"access_token": token, "token_type": "bearer", "expires_in": TOKEN_SECONDS,
            "initial_session_id": initial_session_id}
