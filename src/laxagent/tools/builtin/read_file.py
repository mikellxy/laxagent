from pydantic import BaseModel, ConfigDict, Field

from laxagent import layout
from laxagent.sandbox.file_tools import run_file_tool
from laxagent.tools.builtin.bash import run_cancellable_sandbox
from laxagent.tools.context import ToolContext
from laxagent.tools.errors import ToolError


class ReadFileArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    path: str = Field(min_length=1, description="Absolute sandbox path, e.g. /user/project/1/file.txt or /user/skills/example/SKILL.md; no symlinks or '..'.")
    start_line_no: int = Field(ge=1, description="First line to read, 1-based.")
    start_byte_no: int = Field(ge=1, description="1-based UTF-8 byte offset within the first line, on a character boundary.")
    total_lines: int = Field(ge=1, description="Lines to read; capped at 2000 lines and 50 KiB.")


class ReadFileError(ToolError):
    pass


async def read_file(context: ToolContext, args: ReadFileArgs) -> dict:
    response = await run_cancellable_sandbox(
        run_file_tool, "read_file", layout.get_user_sandbox_mounts(context.user_id), args.model_dump(),
    )
    if response["error_code"] is not None:
        raise ReadFileError(response["error_code"], result=response["result"])
    return response["result"]
