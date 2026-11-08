"""Descriptor-relative file access and atomic writes within a trusted workspace."""

import errno
import os
import secrets
import stat
from contextlib import contextmanager, suppress
from pathlib import Path

from laxcode.tools.errors import ToolError


def file_path(path: str) -> Path:
    relative = Path(path)
    if (relative.is_absolute() or ".." in relative.parts or not relative.parts
            or "\x00" in path or path.endswith(("/", "/."))):
        raise ToolError("INVALID_PATH")
    try:
        os.fsencode(path)
    except UnicodeEncodeError:
        raise ToolError("INVALID_PATH") from None
    return relative


def encode_text(text: str) -> bytes:
    try:
        content = text.encode("utf-8")
    except UnicodeEncodeError:
        raise ToolError("NOT_UTF8_TEXT") from None
    if b"\x00" in content:
        raise ToolError("NOT_UTF8_TEXT")
    return content


def file_error_code(exc: OSError, fallback: str) -> str:
    return {errno.EACCES: "ACCESS_DENIED", errno.EPERM: "ACCESS_DENIED",
            errno.EROFS: "ACCESS_DENIED", errno.ELOOP: "INVALID_PATH",
            errno.ENOTDIR: "INVALID_PATH", errno.EISDIR: "NOT_A_FILE",
            errno.ENAMETOOLONG: "INVALID_PATH"}.get(exc.errno, fallback)


@contextmanager
def open_parent(workspace: str, relative: Path, *, create: bool = False):
    directory = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in relative.parts[:-1]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=directory)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            previous, directory = directory, child
            os.close(previous)
        yield directory
    finally:
        with suppress(OSError):
            os.close(directory)


def file_mode(directory: int, name: str, *, allow_missing: bool) -> int:
    try:
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        if allow_missing:
            return 0o600
        raise
    if stat.S_ISLNK(info.st_mode):
        raise ToolError("INVALID_PATH")
    if not stat.S_ISREG(info.st_mode):
        raise ToolError("NOT_A_FILE")
    return info.st_mode & 0o777


def read_bytes(directory: int, name: str) -> bytes:
    # Check before opening to reject sockets/devices; fstat checks again after open.
    file_mode(directory, name, allow_missing=False)
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(fd, "rb") as file:
        if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
            raise ToolError("NOT_A_FILE")
        return file.read()


def atomic_replace(directory: int, name: str, content: bytes, *, allow_missing: bool = True) -> None:
    mode = file_mode(directory, name, allow_missing=allow_missing)
    fd, temporary = None, None
    try:
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
            raise ToolError("WRITE_FAILED")
        remaining = memoryview(content)
        while remaining:
            written = os.write(fd, remaining)
            if written == 0:
                raise ToolError("WRITE_FAILED")
            remaining = remaining[written:]
        os.fchmod(fd, mode)
        os.fsync(fd)
        closing, fd = fd, None
        os.close(closing)
        # Recheck targets changed during writing; replace itself never follows a link.
        file_mode(directory, name, allow_missing=allow_missing)
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        temporary = None
    finally:
        # Cleanup must not obscure the original failure or a successful replacement.
        if fd is not None:
            with suppress(OSError):
                os.close(fd)
        if temporary is not None:
            with suppress(OSError):
                os.unlink(temporary, dir_fd=directory)
