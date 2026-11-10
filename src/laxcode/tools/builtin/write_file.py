from pydantic import BaseModel, ConfigDict, Field

from laxcode.tools.context import ToolContext
from laxcode.tools.errors import ToolError
from laxcode.tools.workspace import atomic_replace, encode_text, file_error_code, file_path, open_parent


class WriteFileArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    path: str = Field(min_length=1, description="File path relative to workspace; no symlinks or '..'.")
    content: str = Field(description="Complete UTF-8 text content, replacing any existing file; may be empty.")


class WriteFileError(ToolError):
    pass


def write_file(context: ToolContext, args: WriteFileArgs) -> dict:
    try:
        relative = file_path(args.path)
        content = encode_text(args.content)
        with open_parent(context.workspace, relative, create=True) as directory:
            atomic_replace(directory, relative.name, content)
        return {"path": relative.as_posix(), "bytes_written": len(content)}
    except ToolError as exc:
        raise WriteFileError(str(exc)) from None
    except OSError as exc:
        raise WriteFileError(file_error_code(exc, "WRITE_FAILED")) from None
