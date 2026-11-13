from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from laxagent.memory import MemoryCategory, memory_category_description
from laxagent.tools.context import ToolContext


MemoryId = Annotated[int, Field(gt=0, le=2**31 - 1)]


class MemoryArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class WriteMemoryArgs(MemoryArgs):
    abstract: str = Field(min_length=1, description="Self-contained summary including topic, scope and key conditions.")
    detail: str = Field(min_length=1, description="Complete memory, preserving applicable conditions and exceptions.")
    category: MemoryCategory = Field(description=memory_category_description())
    merge_ids: list[MemoryId] = Field(
        description="Old memory IDs to atomically replace; [] creates a new memory. Read old details first.",
    )

    @field_validator("abstract", "detail")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Memory text must not be blank")
        return value


class ListMemoryAbstractsArgs(MemoryArgs):
    categories: list[MemoryCategory] = Field(min_length=1, description=memory_category_description())


class GetMemoryDetailsArgs(MemoryArgs):
    ids: list[MemoryId] = Field(min_length=1, description="Memory IDs selected from relevant abstracts.")


async def write_memory(context: ToolContext, args: WriteMemoryArgs) -> dict:
    return await context.repository.write_memory(user_id=context.user_id, **args.model_dump())


async def list_memory_abstracts(context: ToolContext, args: ListMemoryAbstractsArgs) -> dict:
    return {"memory_abstracts": await context.repository.list_memory_abstracts(
        user_id=context.user_id, categories=args.categories,
    )}


async def get_memory_details(context: ToolContext, args: GetMemoryDetailsArgs) -> dict:
    return {"memory_list": await context.repository.get_memory_details(user_id=context.user_id, ids=args.ids)}
