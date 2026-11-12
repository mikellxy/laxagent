from pydantic import BaseModel, ConfigDict

from laxcode.tools.context import ToolContext


class ListWrittenFilesArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


def list_written_files(context: ToolContext, args: ListWrittenFilesArgs) -> dict:
    return {"paths": list(context.written_workspace_paths)}
