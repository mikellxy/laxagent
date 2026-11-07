from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ChatRequest(BaseModel):
    query: str = Field(min_length=1)


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


class DebugInvokeRequest(CreateUserRequest):
    user_query: str = Field(min_length=1, pattern=r"\S")
    model: str = Field(pattern=r"^[^:\s]+:\S+$")
    run_id: Annotated[int, Field(gt=0)]


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    user_id: str


class ProjectResponse(UserResponse):
    name: str


class SessionResponse(ProjectResponse):
    project_id: int
    next_msg_seq: int
