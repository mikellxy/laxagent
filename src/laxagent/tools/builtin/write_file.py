from pathlib import Path, PurePosixPath
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from laxagent import layout
from laxagent.sandbox.file_tools import run_file_tool
from laxagent.tools.builtin.bash import run_cancellable_sandbox
from laxagent.tools.context import ToolContext
from laxagent.tools.errors import ToolError


class WriteFileArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    operation: Literal["write", "delete"] = Field(default="write", description="Create/overwrite or delete an ordinary file; directories cannot be deleted.")
    path: str = Field(min_length=1, description="Absolute sandbox path, e.g. /user/project/1/file.txt or /user/skills/example/SKILL.md; no symlinks or '..'.")
    content: str | None = Field(description="UTF-8 content for write (empty keeps an empty file); null for delete.")

    @model_validator(mode="after")
    def check_operation(self) -> Self:
        if self.operation == "delete":
            if self.content is not None:
                raise ValueError("Delete requires null content")
        elif self.content is None:
            raise ValueError("Write requires string content")
        return self


class WriteFileError(ToolError):
    pass


async def write_file(context: ToolContext, args: WriteFileArgs) -> dict:
    response = await run_cancellable_sandbox(
        run_file_tool, "write_file", layout.get_user_sandbox_mounts(context.user_id), args.model_dump(),
    )
    if response["error_code"] is not None:
        raise WriteFileError(response["error_code"], result=response["result"])
    result = response["result"]
    if args.operation == "write":
        # Execution does not depend on workspace. Only the preview candidate list
        # needs the current project; writes to skills/other projects stay out of it.
        workspace = layout.get_sandbox_project_workdir(Path(context.workspace).name)
        path = PurePosixPath(result["path"])
        if path.is_relative_to(workspace):
            context.record_workspace_path(path.relative_to(workspace).as_posix())
    return result
