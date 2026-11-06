from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    query: str = Field(min_length=1)
