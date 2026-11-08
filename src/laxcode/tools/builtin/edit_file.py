import errno
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from laxcode.tools.errors import ToolError
from laxcode.tools.workspace import (
    atomic_replace, encode_text, file_error_code, file_path, open_parent, read_bytes,
)


class TextEdit(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    old_text: str = Field(min_length=1, description="Unique original text to replace; include enough context.")
    new_text: str = Field(description="Literal replacement, including indentation and line endings; empty deletes.")


class EditFileArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    path: str = Field(min_length=1, description="Existing file relative to workspace; no symlinks or '..'.")
    edits: list[TextEdit] = Field(min_length=1, description="Non-overlapping edits, all matched against the original file.")


class EditFileError(ToolError):
    pass


@dataclass(frozen=True)
class _Line:
    start: int
    body_end: int
    end: int
    key: bytes


@dataclass(frozen=True)
class _Match:
    start: int
    end: int
    original: bytes
    replacement: bytes


def _lines(content: bytes) -> list[_Line]:
    """Index LF/CRLF lines in original byte coordinates, without a phantom EOF line."""
    result, start = [], 0
    while start < len(content):
        newline = content.find(b"\n", start)
        end = len(content) if newline == -1 else newline + 1
        body_end = end if newline == -1 else newline
        if newline != -1 and body_end > start and content[body_end - 1] == 13:
            body_end -= 1
        result.append(_Line(start, body_end, end, content[start:body_end].strip(b" \t")))
        start = end
    return result


def _find_range(content: bytes, old: bytes, lines: list[_Line]) -> tuple[int, int]:
    start = content.find(old)
    if start != -1:
        # Advancing one byte includes overlapping occurrences (e.g. aa in aaa).
        if content.find(old, start + 1) != -1:
            raise EditFileError("EDIT_AMBIGUOUS")
        return start, start + len(old)
    if old.decode("utf-8").isspace():
        raise EditFileError("EDIT_NOT_FOUND")

    wanted = _lines(old)
    keys = [line.key for line in wanted]
    needs_newline = old.endswith(b"\n")
    match = None
    for index in range(len(lines) - len(wanted) + 1):
        if any(lines[index + offset].key != key for offset, key in enumerate(keys)):
            continue
        last = lines[index + len(wanted) - 1]
        if needs_newline and last.body_end == last.end:
            continue
        if match is not None:
            raise EditFileError("EDIT_AMBIGUOUS")
        match = (lines[index].start, last.end if needs_newline else last.body_end)
    if match is None:
        raise EditFileError("EDIT_NOT_FOUND")
    return match


def _locate_edits(content: bytes, edits: list[tuple[bytes, bytes]]) -> list[_Match]:
    lines = _lines(content)
    matches = []
    for old, new in edits:
        start, end = _find_range(content, old, lines)
        matches.append(_Match(start, end, content[start:end], new))
    matches.sort(key=lambda match: match.start)
    if any(previous.end > current.start for previous, current in zip(matches, matches[1:])):
        raise EditFileError("EDIT_OVERLAP")
    return matches


def _apply_matches(content: bytes, matches: list[_Match]) -> bytes:
    for match in reversed(matches):
        # Check the exact original bytes at the recorded offset, not a new global match.
        if content[match.start:match.end] != match.original:
            raise EditFileError("EDIT_POSITION_CHANGED")
        content = content[:match.start] + match.replacement + content[match.end:]
    return content


def edit_file(workspace: str, args: EditFileArgs) -> dict:
    failure = "READ_FAILED"
    try:
        relative = file_path(args.path)
        edits = [(encode_text(edit.old_text), encode_text(edit.new_text)) for edit in args.edits]
        with open_parent(workspace, relative) as directory:
            original = read_bytes(directory, relative.name)
            try:
                original.decode("utf-8")
            except UnicodeDecodeError:
                raise EditFileError("NOT_UTF8_TEXT") from None
            if b"\x00" in original:
                raise EditFileError("NOT_UTF8_TEXT")
            updated = _apply_matches(original, _locate_edits(original, edits))
            changed = updated != original
            if changed:
                failure = "WRITE_FAILED"
                atomic_replace(directory, relative.name, updated, allow_missing=False)
        return {"path": relative.as_posix(), "edits_applied": len(edits),
                "bytes_written": len(updated) if changed else 0}
    except ToolError as exc:
        raise EditFileError(str(exc)) from None
    except OSError as exc:
        code = "FILE_NOT_FOUND" if exc.errno == errno.ENOENT else file_error_code(exc, failure)
        raise EditFileError(code) from None
