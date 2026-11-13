from datetime import date
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from laxagent.tools.context import ToolContext
from laxagent.tools.errors import ToolError


class WebSearchArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    query: str = Field(min_length=1, max_length=100, description="Specific search query, at most 100 characters.")
    search_type: Literal["web", "image"] = "web"
    count: int = Field(default=5, ge=1, le=50, description="Maximum results; images allow at most 5.")
    time_range: str = Field(default="AnyTime", description="AnyTime for unrestricted time; web also supports OneDay/OneWeek/OneMonth/OneYear or YYYY-MM-DD..YYYY-MM-DD. Images require AnyTime.")
    authoritative_only: bool = Field(default=False, description="Web only: restrict to highly authoritative sources.")
    query_rewrite: bool = Field(default=False, description="Allow the search service to rewrite the query; may increase latency.")

    @field_validator("query")
    @classmethod
    def check_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query must not be blank")
        return value.strip()

    @field_validator("time_range")
    @classmethod
    def check_time_range(cls, value: str) -> str:
        if value in {"AnyTime", "OneDay", "OneWeek", "OneMonth", "OneYear"}:
            return value
        parts = value.split("..")
        if len(parts) != 2 or any(len(part) != 10 for part in parts):
            raise ValueError("Invalid date range")
        start, end = map(date.fromisoformat, parts)
        if start.isoformat() != parts[0] or end.isoformat() != parts[1] or start > end:
            raise ValueError("Invalid date range")
        return value

    @model_validator(mode="after")
    def check_image_options(self) -> Self:
        if self.search_type == "image" and (self.count > 5 or self.time_range != "AnyTime" or self.authoritative_only):
            raise ValueError("Images allow count <= 5, time_range=AnyTime and authoritative_only=false")
        return self


async def web_search(context: ToolContext, args: WebSearchArgs) -> dict:
    if context.web_search is None:
        raise ToolError("SEARCH_UNAVAILABLE")
    body = {"Query": args.query, "SearchType": args.search_type, "Count": args.count,
            "QueryControl": {"QueryRewrite": args.query_rewrite}}
    if args.search_type == "web":
        body["Filter"] = {"NeedUrl": True}
        if args.authoritative_only:
            body["Filter"]["AuthInfoLevel"] = 1
        if args.time_range != "AnyTime":
            body["TimeRange"] = args.time_range
    return await context.web_search.search(body)
