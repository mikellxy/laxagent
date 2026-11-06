from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class ChatRequest(BaseModel):
    query: str = Field(min_length=1)


class DebugInvokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    user_query: str = Field(min_length=1, pattern=r"\S")
    model: str = Field(pattern=r"^[^:\s]+:\S+$")
    run_id: Annotated[int, Field(gt=0)]
    user_id: str = Field(min_length=1, pattern=r"\S")
    project_id: Annotated[int, Field(gt=0)]
    session_id: Annotated[int, Field(gt=0)]
