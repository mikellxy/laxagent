from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    user_query: str = Field(min_length=1, pattern=r"\S")
    model: str = Field(pattern=r"^[^:\s]+:\S+$")


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"]
    expires_in: int


class SubmittedRunResponse(BaseModel):
    run_id: int
    session_id: int
    status: Literal["waiting"]


class RunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    run_id: int = Field(validation_alias="id")
    session_id: int
    status: Literal["waiting", "running", "success", "failed"]
    model: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error: str | None


class CreateUserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    user_id: str

    @field_validator("user_id")
    @classmethod
    def normalize_uuid(cls, value):
        return str(UUID(value))


class CreateProjectRequest(CreateUserRequest):
    name: str = Field(min_length=1, pattern=r"\S")


class CreateSessionRequest(CreateProjectRequest):
    pass


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    user_id: str


class ProjectResponse(UserResponse):
    name: str


class SessionResponse(ProjectResponse):
    project_id: int
    next_msg_seq: int
