import errno
import importlib
import json
import os
import stat

import pytest

from laxcode.tools.builtin.read_file import ReadFileArgs, read_file
from laxcode.tools.builtin.write_file import WriteFileArgs, WriteFileError, write_file
from laxcode.tools.executor import execute_tool
from tool_helpers import tool_context
from laxcode.tools.registry import TOOLS, tool_definitions
from laxcode.tools.results import format_result

module = importlib.import_module("laxcode.tools.workspace")


def write(workspace, path="text.txt", content="中文🙂\r\nsecond\nlast"):
    return write_file(tool_context(workspace), WriteFileArgs(path=path, content=content))


def test_create_overwrite_empty_and_read_back(tmp_path):
    for content in ["中文🙂\r\nsecond\nlast" * 100, "短\r\n尾", ""]:
        result = write(tmp_path, "./sub//nested/text.txt", content)
        assert result == {"path": "sub/nested/text.txt", "bytes_written": len(content.encode())}
        assert (tmp_path / result["path"]).read_bytes() == content.encode()
        assert read_file(tool_context(tmp_path), ReadFileArgs(
            path=result["path"], start_line_no=1, start_byte_no=1, total_lines=2000,
        ))["text"] == content
    assert sorted(p.name for p in (tmp_path / "sub/nested").iterdir()) == ["text.txt"]


@pytest.mark.parametrize("path", ["/outside.txt", "../outside.txt", "sub/../text", ".", "./", "sub/", "sub/.",
                                  "sub/\x00text"])
def test_invalid_paths_have_no_side_effects(tmp_path, path):
    # sub/. denotes a directory, even though pathlib normalizes away the final dot.
    before = list(tmp_path.iterdir())
    with pytest.raises(WriteFileError, match="INVALID_PATH"):
        write(tmp_path, path)
    assert list(tmp_path.iterdir()) == before


@pytest.mark.parametrize("content", ["a\x00b", "\ud800"])
def test_invalid_content_does_not_create_directories(tmp_path, content):
    before = list(tmp_path.iterdir())
    with pytest.raises(WriteFileError, match="NOT_UTF8_TEXT"):
        write(tmp_path, "new/nested/text", content)
    assert list(tmp_path.iterdir()) == before


def test_symlinks_special_files_and_workspace_root(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_bytes(b"secret")
    (root / "file_link").symlink_to(outside / "secret")
    (root / "directory_link").symlink_to(outside, target_is_directory=True)
    (root / "dangling").symlink_to(outside / "missing")
    (root / "internal").write_bytes(b"internal")
    (root / "internal_link").symlink_to(root / "internal")
    (root / "directory").mkdir()
    os.mkfifo(root / "fifo")
    for path, code in [("file_link", "INVALID_PATH"), ("directory_link/new/text", "INVALID_PATH"),
                       ("dangling", "INVALID_PATH"), ("internal_link", "INVALID_PATH"),
                       ("directory", "NOT_A_FILE"), ("fifo", "NOT_A_FILE"),
                       ("internal/child", "INVALID_PATH")]:
        with pytest.raises(WriteFileError, match=code):
            write(root, path)
    assert (outside / "secret").read_bytes() == b"secret"
    assert sorted(p.name for p in outside.iterdir()) == ["secret"]
    assert (root / "internal").read_bytes() == b"internal"
    (tmp_path / "root_link").symlink_to(root, target_is_directory=True)
    with pytest.raises(WriteFileError, match="INVALID_PATH"):
        write(tmp_path / "root_link")


def test_hard_link_and_permissions(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    original = tmp_path / "outside"
    original.write_bytes(b"original")
    os.link(original, root / "linked")
    os.chmod(original, 0o6751)
    write(root, "linked", "replacement")
    assert original.read_bytes() == b"original"
    assert stat.S_IMODE((root / "linked").stat().st_mode) == 0o751
    write(root, "new/nested/text")
    assert stat.S_IMODE((root / "new").stat().st_mode) == 0o700
    assert stat.S_IMODE((root / "new/nested").stat().st_mode) == 0o700
    assert stat.S_IMODE((root / "new/nested/text").stat().st_mode) == 0o600


def test_parent_symlink_swap_never_writes_outside(tmp_path, monkeypatch):
    root, outside = tmp_path / "workspace", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "sub").mkdir()
    original_open = os.open

    def swap(path, flags, *args, **kwargs):
        if path == "sub":
            (root / "sub").rename(root / "saved")
            (root / "sub").symlink_to(outside, target_is_directory=True)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(module.os, "open", swap)
    with pytest.raises(WriteFileError, match="INVALID_PATH"):
        write(root, "sub/text")
    assert list(outside.iterdir()) == []
    assert list((root / "saved").iterdir()) == []


@pytest.mark.parametrize("operation", ["write", "fsync", "replace"])
@pytest.mark.parametrize("existing", [False, True])
def test_failure_preserves_target_and_cleans_temporary(tmp_path, monkeypatch, operation, existing):
    parent = tmp_path / "new/nested"
    target = parent / "text"
    if existing:
        parent.mkdir(parents=True)
        target.write_bytes(b"old content")
    original_write = os.write

    def fail(*args, **kwargs):
        if operation == "write":
            original_write(args[0], b"partial")
        raise OSError(errno.ENOSPC, "private host path")

    monkeypatch.setattr(module.os, operation, fail)
    with pytest.raises(WriteFileError, match="^WRITE_FAILED$"):
        write(tmp_path, "new/nested/text")
    assert parent.is_dir()
    assert list(parent.iterdir()) == ([target] if existing else [])
    if existing:
        assert target.read_bytes() == b"old content"


def test_short_writes_and_atomic_visibility(tmp_path, monkeypatch):
    target = tmp_path / "text.txt"
    target.write_bytes(b"old")
    original_write = os.write

    def partial(fd, content):
        assert target.read_bytes() == b"old"
        return original_write(fd, content[:2])

    monkeypatch.setattr(module.os, "write", partial)
    write(tmp_path, content="中文🙂")
    assert target.read_bytes() == "中文🙂".encode()


def test_target_symlink_swap_before_replace(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.write_bytes(b"secret")
    target = root / "text.txt"
    target.write_bytes(b"old")
    original_sync = os.fsync

    def swap(fd):
        original_sync(fd)
        target.unlink()
        target.symlink_to(outside)

    monkeypatch.setattr(module.os, "fsync", swap)
    with pytest.raises(WriteFileError, match="INVALID_PATH"):
        write(root)
    assert outside.read_bytes() == b"secret"
    assert list(root.iterdir()) == [target]


@pytest.mark.anyio
async def test_executor_validation_results_and_guidance(tmp_path, monkeypatch):
    args = {"path": "file", "content": "hello"}
    before = list(tmp_path.iterdir())
    for invalid in ["{", "[]", "null", json.dumps({"path": "file"}), json.dumps({"content": "hi"}),
                    json.dumps({**args, "path": ""}), json.dumps({**args, "content": True}),
                    json.dumps({**args, "path": 1}), json.dumps({**args, "path": "sub/\ud800"}),
                    json.dumps({**args, "extra": 1})]:
        assert await execute_tool("write_file", invalid, context=tool_context(tmp_path)) == {
            "error_code": "INVALID_ARGUMENTS", "result": {},
        }
    assert list(tmp_path.iterdir()) == before
    result = await execute_tool("write_file", json.dumps(args), context=tool_context(tmp_path))
    assert result == {"error_code": None, "result": {"path": "file", "bytes_written": 5}}
    for arguments, code in [({**args, "path": "../outside"}, "INVALID_PATH"),
                            ({**args, "content": "\ud800"}, "NOT_UTF8_TEXT")]:
        result = await execute_tool("write_file", json.dumps(arguments), context=tool_context(tmp_path))
        assert result == {"error_code": code, "result": {}}
        assert not format_result(result).startswith("{")

    def deny(*args, **kwargs):
        raise PermissionError(errno.EACCES, "private host path")

    monkeypatch.setattr(module.os, "replace", deny)
    result = await execute_tool("write_file", json.dumps(args), context=tool_context(tmp_path))
    assert result == {"error_code": "ACCESS_DENIED", "result": {}}
    assert "private host path" not in format_result(result)


def test_definitions_match_registry():
    definitions = {item["name"]: item for item in tool_definitions({"enable_memory": "1"})}
    assert definitions.keys() == TOOLS.keys()
    definition = definitions["write_file"]
    assert definition["strict"] is True
    assert definition["parameters"]["additionalProperties"] is False
    assert definition["parameters"]["required"] == ["path", "content"]
