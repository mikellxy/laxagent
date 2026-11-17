from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, Field

from laxagent import layout
from laxagent.sandbox.file_tools import run_file_tool
from laxagent.tools.builtin.bash import run_cancellable_sandbox
from laxagent.tools.context import ToolContext
from laxagent.tools.errors import ToolError


class TextEdit(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    old_text: str = Field(min_length=1, description="Unique original text to replace; include enough context.")
    new_text: str = Field(description="Literal replacement, including indentation and line endings; empty deletes.")


class EditFileArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    path: str = Field(min_length=1, description="Existing absolute sandbox path, e.g. /user/project/1/file.txt or /user/skills/example/SKILL.md; no symlinks or '..'.")
    edits: list[TextEdit] = Field(min_length=1, description="Non-overlapping edits, all matched against the original file.")


class EditFileError(ToolError):
    pass


async def edit_file(context: ToolContext, args: EditFileArgs) -> dict:
    response = await run_cancellable_sandbox(
        run_file_tool, "edit_file", layout.get_user_sandbox_mounts(context.user_id), args.model_dump(),
    )
    if response["error_code"] is not None:
        raise EditFileError(response["error_code"], result=response["result"])
    result = response["result"]
    # Workspace is used only for preview candidates, never passed to the sandbox.
    workspace = layout.get_sandbox_project_workdir(Path(context.workspace).name)
    path = PurePosixPath(result["path"])
    if path.is_relative_to(workspace):
        context.record_workspace_path(path.relative_to(workspace).as_posix())
    return result
