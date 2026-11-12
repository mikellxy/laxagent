from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

from laxcode.memory import MemoryCategory
from laxcode.run_errors import failure_description


class UserConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    key: str = Field(min_length=1)
    value: str

    @field_validator("key")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Configuration key must not be blank")
        return value


class UserConfigsResponse(BaseModel):
    configs: dict[str, str]


class MemoryResponse(BaseModel):
    id: int
    abstract: str
    detail: str


class MemoryCategoryResponse(BaseModel):
    category: MemoryCategory
    label: str
    items: list[MemoryResponse]


class MemoriesResponse(BaseModel):
    categories: list[MemoryCategoryResponse]


class AppendixReference(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: int = Field(gt=0)
    name: str


class AppendixDisplay(AppendixReference):
    kind: Literal["image", "file"]
    thumbnail_data_url: str | None = None


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    user_query: str
    model: str = Field(pattern=r"^[^:\s]+:\S+$")
    appendix: list[AppendixReference] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def content_required(self):
        if not self.user_query.strip() and not self.appendix:
            raise ValueError("消息文字或附件至少需要一个")
        if len({item.id for item in self.appendix}) != len(self.appendix):
            raise ValueError("附件不能重复")
        return self


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"]
    expires_in: int
    initial_session_id: int | None = None


class SubmittedRunResponse(BaseModel):
    run_id: int
    session_id: int
    status: Literal["waiting"]
    resume_time: int


class ResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    resume_time: int = Field(ge=0)


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
    error_code: str | None
    resume_time: int

    @computed_field
    @property
    def error_desc(self) -> str | None:
        return failure_description(self.error_code, self.error) if self.status == "failed" else None


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
    has_appendix: bool = False


class TextMessageResponse(BaseModel):
    seq: int
    role: Literal["user"]
    content: str
    appendix: list[AppendixDisplay] = Field(default_factory=list)


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
    supports_multimodal: bool = False


class ModelCatalogResponse(BaseModel):
    default_model: str
    models: list[ModelResponse]
