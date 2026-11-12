"""Bounded previews and descriptor-relative reads of current workspace files."""
import codecs
import errno
import io
import os
import stat
import warnings
from pathlib import Path
from typing import BinaryIO

from PIL import Image, UnidentifiedImageError

from laxcode import layout
from laxcode.tools.errors import ToolError
from laxcode.tools.workspace import file_path, file_mode, open_parent

SHORT_BYTES = 64 * 1024
TEXT_BYTES = 2 * 1024 * 1024
IMAGE_BYTES = 20 * 1024 * 1024
IMAGE_TYPES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "GIF": "image/gif"}
LANGUAGES = {
    ".py": "python", ".js": "javascript", ".jsx": "jsx", ".ts": "typescript", ".tsx": "tsx",
    ".json": "json", ".html": "html", ".htm": "html", ".xml": "xml", ".svg": "xml",
    ".md": "markdown", ".css": "css", ".sh": "bash", ".yml": "yaml", ".yaml": "yaml",
    ".toml": "toml", ".sql": "sql", ".go": "go", ".rs": "rust", ".java": "java",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".txt": "text", ".csv": "csv",
}


class WorkspaceFileError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(message)


def open_file(user_id: str, project_id: int, path: str) -> tuple[str, BinaryIO]:
    """Open from trusted home, including every user/project ancestor, without symlinks."""
    try:
        relative = file_path(path)
        workspace = layout.get_project_workdir(user_id, project_id).relative_to(Path.home())
        with open_parent(str(Path.home()), workspace / relative) as directory:
            file_mode(directory, relative.name, allow_missing=False)
            fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise WorkspaceFileError(400, "仅支持普通文件")
                file = os.fdopen(fd, "rb")
            except BaseException:
                os.close(fd)
                raise
        return relative.as_posix(), file
    except ToolError:
        raise WorkspaceFileError(400, "无效的 workspace 文件路径") from None
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.EACCES, errno.EPERM):
            raise WorkspaceFileError(404, "文件不存在或无法读取") from None
        if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.ENAMETOOLONG):
            raise WorkspaceFileError(400, "无效的 workspace 文件路径") from None
        raise WorkspaceFileError(503, "文件读取失败，请重试") from None


def decode_text(data: bytes, *, final: bool) -> str:
    if b"\x00" in data:
        raise ValueError("Binary content")
    return codecs.getincrementaldecoder("utf-8-sig")().decode(data, final=final)


def image_type(data: bytes) -> str | None:
    # Do not let browser MIME guessing turn arbitrary content into an active document.
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                media_type = IMAGE_TYPES.get(image.format)
                if not media_type:
                    return None
                image.verify()
                return media_type
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombWarning,
            Image.DecompressionBombError):
        return None


def looks_like_image(data: bytes) -> bool:
    return (data.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a"))
            or (data.startswith(b"RIFF") and data[8:12] == b"WEBP"))


def preview(file: BinaryIO, path: str) -> dict:
    size = os.fstat(file.fileno()).st_size
    file.seek(0)
    prefix = file.read(SHORT_BYTES + 1)
    result = {"path": path, "name": Path(path).name, "size": size, "kind": "download",
              "language": None, "text": None, "truncated": False, "can_expand": False}
    if looks_like_image(prefix):
        if size <= IMAGE_BYTES:
            file.seek(0)
            data = file.read(IMAGE_BYTES + 1)
            if len(data) <= IMAGE_BYTES and image_type(data):
                result.update(kind="image", can_expand=True)
        return result
    # PDF/ZIP/Office are downloads, even when their first bytes happen to be UTF-8.
    if prefix.startswith((b"%PDF-", b"PK\x03\x04")):
        return result
    truncated = len(prefix) > SHORT_BYTES
    try:
        text = decode_text(prefix[:SHORT_BYTES], final=not truncated)
    except (ValueError, UnicodeError):
        return result
    result.update(kind="text", text=text, truncated=truncated, can_expand=size <= TEXT_BYTES,
                  language=LANGUAGES.get(Path(path).suffix.lower(), "text"))
    return result


def preview_content(file: BinaryIO, path: str) -> tuple[bytes, str]:
    info = preview(file, path)
    if info["kind"] == "download":
        raise WorkspaceFileError(415, "此文件不支持预览，请下载查看")
    limit = IMAGE_BYTES if info["kind"] == "image" else TEXT_BYTES
    if info["size"] > limit:
        raise WorkspaceFileError(413, "文件超过预览上限，请下载查看")
    file.seek(0)
    data = file.read(limit + 1)
    if len(data) > limit:
        raise WorkspaceFileError(413, "文件超过预览上限，请下载查看")
    if info["kind"] == "image":
        media_type = image_type(data)
        if media_type:
            return data, media_type
    else:
        try:
            decode_text(data, final=True)
            return data, "text/plain; charset=utf-8"
        except (ValueError, UnicodeError):
            pass
    raise WorkspaceFileError(415, "此文件不支持预览，请下载查看")
