import codecs
import errno
import os
import stat
from contextlib import contextmanager
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from laxagent.tools.context import ToolContext
from laxagent.tools.errors import ToolError
from laxagent.tools.file_scope import file_scope

MAX_LINES = 2000
MAX_BYTES = 50 * 1024


class ReadFileArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    skill_name: str | None = Field(default=None, description="Skill name to read; JSON null selects workspace; the string 'null' is invalid and the operation will be rejected.")

    path: str = Field(min_length=1, description="File path relative to the selected workspace or skill; no symlinks or '..'.")
    start_line_no: int = Field(ge=1, description="First line to read, 1-based.")
    start_byte_no: int = Field(ge=1, description="1-based UTF-8 byte offset within the first line, on a character boundary.")
    total_lines: int = Field(ge=1, description="Lines to read; capped at 2000 lines and 50 KiB.")


class ReadFileError(ToolError):
    pass


@contextmanager
def open_workspace_file(workspace: str, path: str):
    relative = Path(path)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts or "\x00" in path:
        raise ReadFileError("INVALID_PATH")
    # Open relative to directory descriptors: symlink swaps cannot escape the workspace.
    directory = os.dup(workspace) if isinstance(workspace, int) else os.open(
        workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    try:
        for part in relative.parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(relative.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, "rb") as file:
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ReadFileError("NOT_A_FILE")
            if isinstance(workspace, int) and info.st_nlink != 1:
                raise ReadFileError("INVALID_SKILL_FILE")
            yield file
    finally:
        os.close(directory)


def read_file(context: ToolContext, args: ReadFileArgs) -> dict:
    try:
        with file_scope(context, args.skill_name) as root, open_workspace_file(root, args.path) as file:
            for _ in range(args.start_line_no - 1):
                while True:
                    chunk = file.readline(8192)
                    if not chunk:
                        raise ReadFileError("INVALID_POSITION")
                    if chunk.endswith(b"\n"):
                        break
            remaining = args.start_byte_no - 1
            while remaining:
                chunk = file.readline(min(8192, remaining))
                if not chunk or chunk.endswith(b"\n"):
                    raise ReadFileError("INVALID_POSITION")
                remaining -= len(chunk)
            if not file.peek(1):
                if args.start_line_no != 1 or args.start_byte_no != 1:
                    raise ReadFileError("INVALID_POSITION")
                return {"text": "", "eof": True, "last_line_no": 0,
                        "last_line_complete": True, "last_byte_no": 0}
            if file.peek(1)[0] & 0xC0 == 0x80:
                raise ReadFileError("INVALID_POSITION")
            parts, size = [], 0
            last_line, last_byte, complete = 0, 0, False
            for index in range(min(args.total_lines, MAX_LINES)):
                raw = file.readline(MAX_BYTES - size)
                if not raw:
                    break
                if b"\x00" in raw:
                    raise ReadFileError("NOT_UTF8_TEXT")
                decoder = codecs.getincrementaldecoder("utf-8")()
                text = decoder.decode(raw, final=not file.peek(1))
                pending = decoder.getstate()[0]
                if pending:
                    file.seek(-len(pending), os.SEEK_CUR)
                    raw = raw[:-len(pending)]
                if not raw:
                    break
                parts.append(text)
                size += len(raw)
                last_line = args.start_line_no + index
                last_byte = len(raw) + (args.start_byte_no - 1 if index == 0 else 0)
                complete = raw.endswith(b"\n") or not file.peek(1)
                if not complete or size == MAX_BYTES:
                    break
            return {"text": "".join(parts), "eof": not bool(file.peek(1)),
                    "last_line_no": last_line, "last_line_complete": complete,
                    "last_byte_no": last_byte}
    except UnicodeDecodeError:
        raise ReadFileError("NOT_UTF8_TEXT") from None
    except OSError as exc:
        code = {errno.ENOENT: "FILE_NOT_FOUND", errno.EACCES: "ACCESS_DENIED",
                errno.EPERM: "ACCESS_DENIED", errno.ELOOP: "INVALID_PATH",
                errno.ENOTDIR: "INVALID_PATH"}.get(exc.errno, "READ_FAILED")
        raise ReadFileError(code) from None
