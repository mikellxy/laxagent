import errno
import os
import secrets
import stat
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from laxcode.tools.errors import ToolError


class WriteFileArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    path: str = Field(min_length=1, description="File path relative to workspace; no symlinks or '..'.")
    content: str = Field(description="Complete UTF-8 text content, replacing any existing file; may be empty.")


class WriteFileError(ToolError):
    pass


def _file_mode(directory: int, name: str) -> int:
    try:
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return 0o600
    if stat.S_ISLNK(info.st_mode):
        raise WriteFileError("INVALID_PATH")
    if not stat.S_ISREG(info.st_mode):
        raise WriteFileError("NOT_A_FILE")
    return info.st_mode & 0o777


def write_file(workspace: str, args: WriteFileArgs) -> dict:
    relative = Path(args.path)
    if (relative.is_absolute() or ".." in relative.parts or not relative.parts
            or "\x00" in args.path or args.path.endswith(("/", "/."))):
        raise WriteFileError("INVALID_PATH")
    try:
        os.fsencode(args.path)
    except UnicodeEncodeError:
        raise WriteFileError("INVALID_PATH") from None
    try:
        content = args.content.encode("utf-8")
    except UnicodeEncodeError:
        raise WriteFileError("NOT_UTF8_TEXT") from None
    if b"\x00" in content:
        raise WriteFileError("NOT_UTF8_TEXT")

    directory, fd, temporary = None, None, None
    try:
        # Anchor every operation to directory descriptors, never resolved path strings.
        directory = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in relative.parts[:-1]:
            try:
                os.mkdir(part, mode=0o700, dir_fd=directory)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        name = relative.parts[-1]
        mode = _file_mode(directory, name)
        for _ in range(10):
            candidate = f".laxcode-write-{secrets.token_hex(16)}.tmp"
            if candidate == name:
                continue
            try:
                fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
            except FileExistsError:
                continue
            temporary = candidate
            break
        else:
            raise WriteFileError("WRITE_FAILED")
        remaining = memoryview(content)
        while remaining:
            written = os.write(fd, remaining)
            if written == 0:
                raise WriteFileError("WRITE_FAILED")
            remaining = remaining[written:]
        os.fchmod(fd, mode)
        os.fsync(fd)
        closing, fd = fd, None
        os.close(closing)
        # Recheck targets changed during writing; replace itself never follows a link.
        _file_mode(directory, name)
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        temporary = None
        return {"path": relative.as_posix(), "bytes_written": len(content)}
    except OSError as exc:
        code = {errno.EACCES: "ACCESS_DENIED", errno.EPERM: "ACCESS_DENIED",
                errno.EROFS: "ACCESS_DENIED", errno.ELOOP: "INVALID_PATH",
                errno.ENOTDIR: "INVALID_PATH", errno.EISDIR: "NOT_A_FILE",
                errno.ENAMETOOLONG: "INVALID_PATH"}.get(exc.errno, "WRITE_FAILED")
        raise WriteFileError(code) from None
    finally:
        # Cleanup must not obscure the original failure or a successful replacement.
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if temporary is not None:
            try:
                os.unlink(temporary, dir_fd=directory)
            except OSError:
                pass
        if directory is not None:
            try:
                os.close(directory)
            except OSError:
                pass
