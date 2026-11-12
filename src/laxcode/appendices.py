"""Attachment file handling; originals stay outside the agent workspace."""
import asyncio
import base64
import io
import warnings
from pathlib import Path
from uuid import uuid4
from zipfile import BadZipFile, ZipFile

from PIL import Image, ImageOps, UnidentifiedImageError

from laxcode.layout import get_appendices_dir
from laxcode.run_errors import make_run_error

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_MESSAGE_BYTES = 20 * 1024 * 1024
MAX_ATTACHMENTS = 5
IMAGE_TYPES = {".png": ("PNG", "image/png"), ".jpg": ("JPEG", "image/jpeg"),
               ".jpeg": ("JPEG", "image/jpeg"), ".webp": ("WEBP", "image/webp"),
               ".gif": ("GIF", "image/gif")}
DOCUMENT_TYPES = {
    ".pdf": "application/pdf", ".txt": "text/plain", ".md": "text/markdown", ".csv": "text/csv",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
OFFICE_PARTS = {".docx": "word/document.xml", ".xlsx": "xl/workbook.xml", ".pptx": "ppt/presentation.xml"}


class InvalidAppendix(ValueError):
    pass


def validate_file(name: str, data: bytes) -> tuple[str, bytes | None]:
    """Validate content as well as extension; return a small image preview when applicable."""
    extension = Path(name).suffix.lower()
    if not data or extension not in IMAGE_TYPES | DOCUMENT_TYPES:
        raise InvalidAppendix("不支持的附件格式或空文件")
    try:
        if extension in IMAGE_TYPES:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data)) as picture:
                    if picture.format != IMAGE_TYPES[extension][0] or getattr(picture, "is_animated", False):
                        raise InvalidAppendix("图片格式不匹配或为动画图片")
                    picture.verify()
                with Image.open(io.BytesIO(data)) as picture:
                    picture = ImageOps.exif_transpose(picture)
                    picture.thumbnail((256, 256))
                    preview = io.BytesIO()
                    picture.convert("RGBA").save(preview, "WEBP", quality=80)
                    return IMAGE_TYPES[extension][1], preview.getvalue()
        if extension in (".txt", ".md", ".csv"):
            text = data.decode("utf-8-sig")
            if "\x00" in text:
                raise InvalidAppendix("文本附件必须是 UTF-8 文本")
        elif extension == ".pdf":
            if not data.startswith(b"%PDF-"):
                raise InvalidAppendix("PDF 内容与扩展名不匹配")
        else:
            with ZipFile(io.BytesIO(data)) as archive:
                manifest = archive.getinfo("[Content_Types].xml")
                if manifest.file_size > 1024 * 1024 or OFFICE_PARTS[extension] not in archive.namelist():
                    raise InvalidAppendix("Office 文件内容与扩展名不匹配")
                if b"<" not in archive.read(manifest):
                    raise InvalidAppendix("Office 文件无效")
    except (OSError, ValueError, KeyError, BadZipFile, UnidentifiedImageError,
            Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise InvalidAppendix("附件内容无效或与扩展名不匹配") from exc
    return DOCUMENT_TYPES[extension], None


def save_files(user_id: str, name: str, data: bytes) -> dict:
    media_type, thumbnail = validate_file(name, data)
    directory = get_appendices_dir(user_id)
    directory.mkdir(parents=True, exist_ok=True)
    storage_name = uuid4().hex
    path = directory / f"{storage_name}{Path(name).suffix.lower()}"
    thumbnail_path = directory / f"{storage_name}.thumb.webp" if thumbnail is not None else None
    values = {"user_id": user_id, "name": name, "path": str(path), "media_type": media_type,
              "size_bytes": len(data), "thumbnail_path": str(thumbnail_path) if thumbnail_path else None}
    try:
        path.write_bytes(data)
        if thumbnail_path:
            thumbnail_path.write_bytes(thumbnail)
    except BaseException:
        remove_files(values)
        raise
    return values


def remove_files(values: dict) -> None:
    for field in ("path", "thumbnail_path"):
        if values.get(field):
            Path(values[field]).unlink(missing_ok=True)


def thumbnail_data_url(path: str | None) -> str | None:
    if path is None:
        return None
    try:
        return "data:image/webp;base64," + base64.b64encode(Path(path).read_bytes()).decode("ascii")
    except OSError:
        return None


async def enrich_history(items: list[dict], user_id: str, repository) -> None:
    refs = [ref for item in items for ref in item.get("appendix", [])]
    rows = await repository.get_appendices(user_id, [ref["id"] for ref in refs]) if refs else {}

    def enrich():
        for item in items:
            if item["role"] != "user":
                continue
            result = []
            for ref in item.get("appendix", []):
                row = rows.get(ref["id"])
                is_image = row.media_type.startswith("image/") if row else Path(ref["name"]).suffix.lower() in IMAGE_TYPES
                result.append({**ref, "kind": "image" if is_image else "file",
                               "thumbnail_data_url": thumbnail_data_url(row.thumbnail_path) if row else None})
            item["appendix"] = result
    await asyncio.to_thread(enrich)


async def materialize_input(messages: list[dict], user_id: str, repository) -> list[dict]:
    """Build a temporary wire request without modifying checkpoint-owned dictionaries."""
    ids = {ref["id"] for message in messages for ref in message.get("appendix", [])}
    if not ids:
        return messages
    rows = await repository.get_appendices(user_id, list(ids))
    if ids - rows.keys():
        raise make_run_error("attachment_read_failed")

    def build():
        try:
            blocks = {}
            for identifier, row in rows.items():
                data_url = f"data:{row.media_type};base64," + base64.b64encode(Path(row.path).read_bytes()).decode("ascii")
                blocks[identifier] = ({"type": "input_image", "image_url": data_url}
                                      if row.media_type.startswith("image/") else
                                      {"type": "input_file", "filename": row.name, "file_data": data_url})
            result = []
            for message in messages:
                item = {key: value for key, value in message.items() if key != "appendix"}
                if message.get("appendix"):
                    content = [{"type": "input_text", "text": message["content"]}] if message["content"].strip() else []
                    item["content"] = content + [blocks[ref["id"]] for ref in message["appendix"]]
                result.append(item)
            return result
        except OSError:
            raise make_run_error("attachment_read_failed") from None
    return await asyncio.to_thread(build)
