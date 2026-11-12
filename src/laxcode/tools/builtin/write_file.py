import os
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from laxcode.tools.context import ToolContext
from laxcode.tools.errors import ToolError
from laxcode.tools.workspace import atomic_replace, encode_text, file_error_code, file_path, file_mode, open_parent
from laxcode.tools.file_scope import file_scope, scope_result
from laxcode.skills import parse_metadata


class WriteFileArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    skill_name: str | None = Field(default=None, description="Skill name to modify; JSON null selects workspace; the string 'null' selects a skill named null.")
    operation: Literal["write", "delete"] = Field(default="write", description="Delete is only allowed for skill files.")

    path: str = Field(min_length=1, description="File path relative to the selected workspace or skill; no symlinks or '..'.")
    content: str | None = Field(description="UTF-8 content for write (empty keeps an empty file); null for delete.")

    @model_validator(mode="after")
    def check_operation(self) -> Self:
        if self.operation == "delete":
            if self.skill_name is None or self.content is not None:
                raise ValueError("Delete requires a skill name and null content")
        elif self.content is None:
            raise ValueError("Write requires string content")
        return self


class WriteFileError(ToolError):
    pass


def write_file(context: ToolContext, args: WriteFileArgs) -> dict:
    try:
        relative = file_path(args.path)
        if args.operation == "delete" and relative.as_posix() == "SKILL.md":
            raise WriteFileError("SKILL_ENTRY_REQUIRED")
        content = encode_text(args.content) if args.content is not None else None
        if args.skill_name is not None and relative.as_posix() == "SKILL.md":
            parse_metadata(args.skill_name, content)
        with file_scope(context, args.skill_name, create=args.operation == "write") as root:
            if args.operation == "delete":
                deleted = False
                try:
                    with open_parent(root, relative) as directory:
                        file_mode(directory, relative.name, allow_missing=False)
                        os.unlink(relative.name, dir_fd=directory)
                        deleted = True
                except FileNotFoundError:
                    pass
                return scope_result(root, args.skill_name, {"path": relative.as_posix(), "deleted": deleted})
            with open_parent(root, relative, create=True) as directory:
                atomic_replace(directory, relative.name, content)
            if args.skill_name is None:
                context.record_workspace_path(relative.as_posix())
            return scope_result(root, args.skill_name, {"path": relative.as_posix(), "bytes_written": len(content)})
    except ToolError as exc:
        raise WriteFileError(str(exc)) from None
    except FileNotFoundError:
        if args.operation == "delete":
            return {"path": relative.as_posix(), "deleted": False, "skill_name": args.skill_name, "files": []}
        raise WriteFileError("WRITE_FAILED") from None
    except OSError as exc:
        raise WriteFileError(file_error_code(exc, "WRITE_FAILED")) from None
