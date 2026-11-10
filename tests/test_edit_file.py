import errno
import importlib
import json
import os
import stat
from dataclasses import replace

import pytest

from laxcode.tools.builtin.edit_file import EditFileArgs, EditFileError, TextEdit, edit_file
from laxcode.tools.executor import execute_tool
from laxcode.tools.registry import TOOLS, tool_definitions
from laxcode.tools.results import format_result

module = importlib.import_module("laxcode.tools.builtin.edit_file")
filesystem = importlib.import_module("laxcode.tools.workspace")


def edit(workspace, pairs, path="text.txt"):
    return edit_file(str(workspace), EditFileArgs(
        path=path, edits=[TextEdit(old_text=old, new_text=new) for old, new in pairs],
    ))


def test_unordered_adjacent_edits_unicode_and_new_duplicate(tmp_path):
    target = tmp_path / "text.txt"
    target.write_bytes("中firstSECOND🙂last".encode())
    result = edit(tmp_path, [("last", "first"), ("first", "longer replacement"), ("SECOND", "")], "./text.txt")
    expected = "中longer replacement🙂first".encode()
    assert target.read_bytes() == expected
    assert result == {"path": "text.txt", "edits_applied": 3, "bytes_written": len(expected)}


@pytest.mark.parametrize("original,old,new,expected", [
    ("a\n  foo \t\nend", "foo", "bar", "a\n  bar \t\nend"),  # exact substring preserves padding
    ("  foo\n\tfoo \t", "  foo", "bar", "bar\n\tfoo \t"),  # one exact beats multiple fuzzy matches
    ("header\r\n    foo  \r\n\t\tbar\t\r\ntail  ", "\tfoo\n bar", " NEW\n\tBODY ",
     "header\r\n NEW\n\tBODY \r\ntail  "),
    ("\tfoo \r\n\tbar \r\ntail", " foo\n bar\n", "new\n", "new\ntail"),
    ("\tfoo \n\tbar \ntail", " foo\r\n bar\r\n", "new\r\n", "new\r\ntail"),
    ("\tfoo \r\n", " foo", "", "\r\n"),  # excluding the final newline
    ("\tfoo \r\n", " foo\n", "", ""),  # including the final newline
    ("\tfoo \r\n\r\n", " foo\n\n", "x", "x"),
    ("\tfoo \r\n \t\r\n\tbar", " foo\n\n bar", "x", "x"),
    ("\tfoo \t", " foo", "x", "x"),
    ("a\t b", "\t ", " ", "a b"),  # pure whitespace exact matching is allowed
])
def test_exact_and_whitespace_matching_preserve_other_bytes(tmp_path, original, old, new, expected):
    target = tmp_path / "text.txt"
    target.write_bytes(original.encode())
    edit(tmp_path, [(old, new)])
    assert target.read_bytes() == expected.encode()


@pytest.mark.parametrize("original,pairs,code", [
    ("aaa", [("aa", "x")], "EDIT_AMBIGUOUS"),
    ("foo foo", [("foo", "x")], "EDIT_AMBIGUOUS"),
    ("\tfoo\n    foo ", [(" foo  ", "x")], "EDIT_AMBIGUOUS"),
    ("abcde", [("abc", "x"), ("cde", "y")], "EDIT_OVERLAP"),
    ("abcde", [("abcde", "x"), ("bc", "y")], "EDIT_OVERLAP"),
    ("abcde", [("abc", "x"), ("abc", "y")], "EDIT_OVERLAP"),
    ("\tfoo \nbar", [(" foo  ", "x"), ("foo", "y")], "EDIT_OVERLAP"),
    ("first last", [("first", "NEW"), ("missing", "y")], "EDIT_NOT_FOUND"),
    ("first last", [("first", "NEW"), ("NEW", "y")], "EDIT_NOT_FOUND"),
    ("x foo y", [("  foo  ", "x")], "EDIT_NOT_FOUND"),  # fallback is whole-line only
    ("a  b", [(" a b ", "x")], "EDIT_NOT_FOUND"),
    ("\tFoo", [(" foo ", "x")], "EDIT_NOT_FOUND"),
    ("\u00a0foo\u00a0", [(" foo ", "x")], "EDIT_NOT_FOUND"),
    ("\t‘foo’", [(" 'foo' ", "x")], "EDIT_NOT_FOUND"),
    ("\tfoo\n\nbar", [(" foo\nbar ", "x")], "EDIT_NOT_FOUND"),
    ("\tfoo\nbar", [(" foo\n\nbar ", "x")], "EDIT_NOT_FOUND"),
    ("\tfoo", [(" foo\n", "x")], "EDIT_NOT_FOUND"),
    ("\tfoo\n", [(" foo\n\n", "x")], "EDIT_NOT_FOUND"),
    ("\t", [("  ", "x")], "EDIT_NOT_FOUND"),  # no pure-whitespace fuzzy matching
    ("", [("old", "new")], "EDIT_NOT_FOUND"),
])
def test_matching_failures_leave_entire_file_unchanged(tmp_path, original, pairs, code):
    target = tmp_path / "text.txt"
    target.write_bytes(original.encode())
    before = target.stat()
    with pytest.raises(EditFileError, match=f"^{code}$"):
        edit(tmp_path, pairs)
    assert target.read_bytes() == original.encode()
    assert target.stat().st_ino == before.st_ino
    assert not list(tmp_path.glob(".laxcode-write-*"))


def test_no_change_does_not_replace_file(tmp_path, monkeypatch):
    target = tmp_path / "text.txt"
    target.write_bytes(b"original")
    before = target.stat()

    def unexpected(*args, **kwargs):
        pytest.fail("No-op edits must not write")

    monkeypatch.setattr(module, "atomic_replace", unexpected)
    assert edit(tmp_path, [("original", "original")]) == {
        "path": "text.txt", "edits_applied": 1, "bytes_written": 0,
    }
    assert target.stat() == before


def test_each_original_offset_is_checked_before_replacement(tmp_path, monkeypatch):
    target = tmp_path / "text.txt"
    target.write_bytes("中first last".encode())
    locate = module._locate_edits

    def corrupt(content, edits):
        matches = locate(content, edits)
        # The later replacement runs first, then the earlier guard fails. Nothing is written.
        matches[0] = replace(matches[0], start=matches[0].start + 1)
        return matches

    monkeypatch.setattr(module, "_locate_edits", corrupt)
    with pytest.raises(EditFileError, match="EDIT_POSITION_CHANGED"):
        edit(tmp_path, [("first", "x"), ("last", "longer")])
    assert target.read_bytes() == "中first last".encode()


@pytest.mark.parametrize("raw", [b"old\xff", b"old\x00", b"old\xe4\xb8"])
def test_invalid_file_text_is_not_edited(tmp_path, raw):
    target = tmp_path / "text.txt"
    target.write_bytes(raw)
    with pytest.raises(EditFileError, match="NOT_UTF8_TEXT"):
        edit(tmp_path, [("old", "new")])
    assert target.read_bytes() == raw


@pytest.mark.parametrize("path,code", [
    ("/outside", "INVALID_PATH"), ("../outside", "INVALID_PATH"), ("a/../outside", "INVALID_PATH"),
    (".", "INVALID_PATH"), ("a/", "INVALID_PATH"), ("a/.", "INVALID_PATH"),
    ("a/\x00", "INVALID_PATH"), ("missing", "FILE_NOT_FOUND"), ("missing/text", "FILE_NOT_FOUND"),
])
def test_invalid_paths_and_missing_files_never_create_directories(tmp_path, path, code):
    before = list(tmp_path.iterdir())
    with pytest.raises(EditFileError, match=code):
        edit(tmp_path, [("old", "new")], path)
    assert list(tmp_path.iterdir()) == before


def test_symlinks_special_files_and_hard_links(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.write_bytes(b"old")
    (root / "link").symlink_to(outside)
    (root / "dangling").symlink_to(tmp_path / "missing")
    (root / "parent_link").symlink_to(tmp_path, target_is_directory=True)
    (root / "directory").mkdir()
    os.mkfifo(root / "fifo")
    for path, code in [("link", "INVALID_PATH"), ("dangling", "INVALID_PATH"),
                       ("parent_link/outside", "INVALID_PATH"), ("directory", "NOT_A_FILE"),
                       ("fifo", "NOT_A_FILE")]:
        with pytest.raises(EditFileError, match=code):
            edit(root, [("old", "new")], path)
    (tmp_path / "root_link").symlink_to(root, target_is_directory=True)
    with pytest.raises(EditFileError, match="INVALID_PATH"):
        edit(tmp_path / "root_link", [("old", "new")])
    os.link(outside, root / "text.txt")
    os.chmod(outside, 0o6751)
    edit(root, [("old", "new")])
    assert outside.read_bytes() == b"old"
    assert (root / "text.txt").read_bytes() == b"new"
    assert stat.S_IMODE((root / "text.txt").stat().st_mode) == 0o751


@pytest.mark.parametrize("operation", ["write", "fsync", "replace"])
def test_write_failure_preserves_file_and_cleans_temporary(tmp_path, monkeypatch, operation):
    target = tmp_path / "text.txt"
    target.write_bytes(b"old")
    real_write = os.write

    def fail(*args, **kwargs):
        if operation == "write":
            real_write(args[0], b"partial")
        raise OSError(errno.ENOSPC, "private path")

    monkeypatch.setattr(filesystem.os, operation, fail)
    with pytest.raises(EditFileError, match="^WRITE_FAILED$"):
        edit(tmp_path, [("old", "new")])
    assert target.read_bytes() == b"old"
    assert not list(tmp_path.glob(".laxcode-write-*"))


def test_target_swapped_to_symlink_before_commit(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "text.txt"
    target.write_bytes(b"old")
    outside = tmp_path / "outside"
    outside.write_bytes(b"secret")
    real_sync = os.fsync

    def swap(fd):
        real_sync(fd)
        target.unlink()
        target.symlink_to(outside)

    monkeypatch.setattr(filesystem.os, "fsync", swap)
    with pytest.raises(EditFileError, match="INVALID_PATH"):
        edit(root, [("old", "new")])
    assert outside.read_bytes() == b"secret"
    assert list(root.iterdir()) == [target]


@pytest.mark.anyio
async def test_strict_arguments_errors_and_tool_definition(tmp_path):
    target = tmp_path / "text.txt"
    target.write_bytes(b"old")
    args = {"path": "text.txt", "edits": [{"old_text": "old", "new_text": "new"}]}
    invalid = ["{", "[]", "null", json.dumps({**args, "extra": 1}), json.dumps({"path": "text.txt"}),
               json.dumps({**args, "path": 1}), json.dumps({**args, "edits": []})]
    for item in [{"old_text": "", "new_text": "x"}, {"old_text": "old"},
                 {"old_text": "old", "new_text": True}, {"old_text": 1, "new_text": "x"},
                 {"old_text": "old", "new_text": "x", "extra": 1}]:
        invalid.append(json.dumps({**args, "edits": [item]}))
    for arguments in invalid:
        assert await execute_tool("edit_file", arguments, workspace=str(tmp_path)) == {
            "error_code": "INVALID_ARGUMENTS", "result": {},
        }
    for item, code in [({"old_text": "old", "new_text": "\ud800"}, "NOT_UTF8_TEXT"),
                       ({"old_text": "old", "new_text": "\x00"}, "NOT_UTF8_TEXT"),
                       ({"old_text": "o\x00ld", "new_text": "x"}, "NOT_UTF8_TEXT"),
                       ({"old_text": "missing", "new_text": "x"}, "EDIT_NOT_FOUND")]:
        result = await execute_tool("edit_file", json.dumps({**args, "edits": [item]}), workspace=str(tmp_path))
        assert result == {"error_code": code, "result": {}}
        assert not format_result(result).startswith("{")
    assert target.read_bytes() == b"old"
    assert await execute_tool("edit_file", json.dumps(args), workspace=str(tmp_path)) == {
        "error_code": None, "result": {"path": "text.txt", "edits_applied": 1, "bytes_written": 3},
    }
    definitions = {item["name"]: item for item in tool_definitions({"enable_memory": "1"})}
    assert definitions.keys() == TOOLS.keys()
    definition = definitions["edit_file"]
    assert definition["strict"] is True
    schema = definition["parameters"]
    assert schema["required"] == ["path", "edits"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["edits"]["minItems"] == 1
    assert schema["$defs"]["TextEdit"]["required"] == ["old_text", "new_text"]
    assert schema["$defs"]["TextEdit"]["additionalProperties"] is False
