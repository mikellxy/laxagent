import asyncio
from unittest.mock import Mock
from uuid import uuid4

import httpx2
import jwt
import pytest
from fastapi import Depends
from sqlalchemy import event, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session as SyncSession

from laxcode.agent.context import AgentContext
from laxcode.api.app import create_app
from laxcode.api.auth import get_current_user
from laxcode.config import AppConfig
from laxcode.layout import get_user_data_dir
from laxcode.storage.models import Project, Session, User


@pytest.fixture
async def login_api(repository, config_data):
    app = create_app(AppConfig.model_validate(config_data))
    app.state.agent_context = AgentContext(None, None, repository)

    @app.get("/test-identity")
    async def identity(user_id=Depends(get_current_user)):
        return {"user_id": user_id}

    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app), base_url="http://test", trust_env=False) as api:
        yield api


@pytest.mark.anyio
async def test_login_creates_user_once_and_token_authenticates(login_api, repository, config_data):
    user_id = str(uuid4())
    assert not await repository.user_exists(user_id)
    for value in (user_id.upper(), user_id):
        response = await login_api.post("/api/v1/login", json={"user_id": value})
        assert response.status_code == 200
        body = response.json()
        assert set(body) == {"access_token", "token_type", "expires_in", "initial_session_id"}
        assert body["token_type"] == "bearer" and body["expires_in"] == 86400
        claims = jwt.decode(body["access_token"], config_data["auth"]["jwt_secret"], algorithms=["HS256"])
        assert claims["sub"] == user_id and claims["exp"] - claims["iat"] == 86400
        identity = await login_api.get("/test-identity", headers={"Authorization": f"Bearer {body['access_token']}"})
        assert identity.status_code == 200 and identity.json() == {"user_id": user_id}
        async with repository.db.transaction() as transaction:
            assert await transaction.scalar(select(func.count()).select_from(User)) == 1
            assert await transaction.scalar(select(func.count()).select_from(Project)) == 0
            assert await transaction.scalar(select(func.count()).select_from(Session)) == 0
    assert not get_user_data_dir(user_id).exists()


@pytest.mark.anyio
async def test_concurrent_first_logins_share_one_user(login_api, repository):
    user_id = str(uuid4())
    responses = await asyncio.gather(*[
        login_api.post("/api/v1/login", json={"user_id": value})
        for value in (user_id, user_id.upper()) * 4
    ])
    assert all(response.status_code == 200 for response in responses)
    async with repository.db.transaction() as transaction:
        original_id = await transaction.scalar(select(User.id).where(User.user_id == user_id))
        assert await transaction.scalar(select(func.count()).select_from(User)) == 1
    assert (await login_api.post("/api/v1/login", json={"user_id": user_id})).status_code == 200
    async with repository.db.transaction() as transaction:
        assert await transaction.scalar(select(User.id).where(User.user_id == user_id)) == original_id


@pytest.mark.anyio
@pytest.mark.parametrize("body", [{}, {"user_id": "invalid"}, {"user_id": 42},
                                  {"user_id": str(uuid4()), "password": "unused"}])
async def test_invalid_login_does_not_create_user(login_api, repository, body):
    assert (await login_api.post("/api/v1/login", json=body)).status_code == 422
    async with repository.db.transaction() as transaction:
        assert await transaction.scalar(select(func.count()).select_from(User)) == 0


@pytest.mark.anyio
@pytest.mark.parametrize("stage", ["insert", "commit"])
async def test_database_failure_rolls_back_and_never_signs_token(login_api, repository, monkeypatch, stage):
    user_id = str(uuid4())
    encode = Mock(side_effect=AssertionError("Token must not be signed before commit"))
    monkeypatch.setattr("laxcode.api.auth.jwt.encode", encode)

    def fail(*args):
        raise OperationalError("forced failure", {}, RuntimeError("private database error"))

    target, hook = ((repository.db.engine.sync_engine, "before_cursor_execute") if stage == "insert"
                    else (SyncSession, "before_commit"))
    event.listen(target, hook, fail)
    try:
        response = await login_api.post("/api/v1/login", json={"user_id": user_id})
    finally:
        event.remove(target, hook, fail)
    assert response.status_code == 503
    assert response.json() == {"detail": {"error": "Database unavailable"}}
    encode.assert_not_called()
    assert not await repository.user_exists(user_id)


@pytest.mark.anyio
async def test_user_creation_route_removed(login_api):
    assert (await login_api.post("/api/v1/users", json={"user_id": str(uuid4())})).status_code == 404
    schema = (await login_api.get("/openapi.json")).json()
    assert "/api/v1/users" not in schema["paths"]
    assert "/api/v1/login" in schema["paths"]
    assert "CreateUserRequest" not in schema["components"]["schemas"]
