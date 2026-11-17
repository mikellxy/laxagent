from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field

from laxagent.tools.builtin.bash import run_cancellable_sandbox
from laxagent.tools.context import ToolContext
from laxagent.tools.errors import ToolError


class TextEdit(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    old_text: str = Field(min_length=1, description="Unique original text to replace; include enough context.")
    new_text: str = Field(description="Literal replacement, including indentation and line endings; empty deletes.")


class EditFileArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    path: str = Field(min_length=1, description="Existing absolute sandbox path, e.g. /workspace/file.txt; no symlinks or '..'.")
    edits: list[TextEdit] = Field(min_length=1, description="Non-overlapping edits, all matched against the original file.")


class EditFileError(ToolError):
    pass


async def edit_file(context: ToolContext, args: EditFileArgs) -> dict:
    response = await run_cancellable_sandbox(
        context.backend.run_file_tool, "edit_file", args.model_dump(),
    )
    if response["error_code"] is not None:
        raise EditFileError(response["error_code"], result=response["result"])
    result = response["result"]
    # Workspace is used only for preview candidates, never passed to the sandbox.
    workspace = PurePosixPath(context.workspace)
    path = PurePosixPath(result["path"])
    if path.is_relative_to(workspace):
        context.record_workspace_path(path.relative_to(workspace).as_posix())
    return result
