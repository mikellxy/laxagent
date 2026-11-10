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
    initial_session_id: int | None = None


class SubmittedRunResponse(BaseModel):
    run_id: int
    session_id: int
    status: Literal["waiting"]


class RunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    run_id: int = Field(validation_alias="id")
    session_id: int
    status: Literal["waiting", "running", "success", "failed", "cancelled"]
    model: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error: str | None


class UserIdRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    user_id: str

    @field_validator("user_id")
    @classmethod
    def normalize_uuid(cls, value):
        return str(UUID(value))


class LoginRequest(UserIdRequest):
    from_page: Literal["guest_login"] | None = None


class NameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def trim_name(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Name must not be blank")
        return value


class CreateSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ProjectResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    user_id: str
    name: str


class SessionResponse(ProjectResponse):
    project_id: int
    next_msg_seq: int


class SessionDetailResponse(SessionResponse):
    latest_run: RunResponse | None


class TextMessageResponse(BaseModel):
    seq: int
    role: Literal["user"]
    content: str


class ReasoningPart(BaseModel):
    channel: Literal["text", "summary"]
    text: str


class AssistantMessageResponse(BaseModel):
    seq: int
    role: Literal["assistant"]
    content: str
    reasoning: list[ReasoningPart] = Field(default_factory=list)


class ToolMessageResponse(BaseModel):
    seq: int
    role: Literal["tool"]
    tool_name: str
    arguments: str


class MessagePageResponse(BaseModel):
    items: list[TextMessageResponse | AssistantMessageResponse | ToolMessageResponse]
    has_more: bool
    next_before_seq: int | None


class ModelResponse(BaseModel):
    id: str
    provider: str
    name: str


class ModelCatalogResponse(BaseModel):
    default_model: str
    models: list[ModelResponse]
