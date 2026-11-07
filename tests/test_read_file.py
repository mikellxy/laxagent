import json
import os

import pytest

from laxcode.tools.builtin.read_file import MAX_BYTES, ReadFileArgs, ReadFileError, read_file
from laxcode.tools.executor import execute_tool
from laxcode.tools.results import format_result


def read(workspace, path="text.txt", line=1, byte=1, lines=2000):
    return read_file(str(workspace), ReadFileArgs(
        path=path, start_line_no=line, start_byte_no=byte, total_lines=lines,
    ))


def test_unicode_long_line_continuation_without_data_loss(tmp_path):
    text = "中文🙂" * 10000 + "\n最后一行"
    (tmp_path / "text.txt").write_text(text)
    line, byte, chunks = 1, 1, []
    while True:
        result = read(tmp_path, line=line, byte=byte)
        chunks.append(result["text"])
        assert len(result["text"].encode()) <= MAX_BYTES
        if result["eof"]:
            break
        line = result["last_line_no"] + int(result["last_line_complete"])
        byte = 1 if result["last_line_complete"] else result["last_byte_no"] + 1
    assert "".join(chunks) == text
    assert len(chunks) > 1
    assert result["last_line_no"] == 2 and result["last_byte_no"] == len("最后一行".encode())


def test_line_cap_crlf_and_offsets(tmp_path):
    (tmp_path / "text.txt").write_bytes("中a\r\n\n尾".encode())
    assert read(tmp_path, byte=4, lines=1) == {
        "text": "a\r\n", "eof": False, "last_line_no": 1,
        "last_line_complete": True, "last_byte_no": 6,
    }
    assert read(tmp_path, line=2, lines=1)["text"] == "\n"
    assert read(tmp_path, line=3)["text"] == "尾"
    (tmp_path / "text.txt").write_bytes(b"x\n" * 2001)
    result = read(tmp_path, lines=5000)
    assert result["last_line_no"] == 2000 and not result["eof"]
    assert read(tmp_path, line=2001)["eof"]


def test_empty_file_and_exact_byte_limit(tmp_path):
    (tmp_path / "text.txt").write_bytes(b"")
    assert read(tmp_path) == {"text": "", "eof": True, "last_line_no": 0,
                             "last_line_complete": True, "last_byte_no": 0}
    (tmp_path / "text.txt").write_bytes(b"a" * MAX_BYTES)
    result = read(tmp_path)
    assert result["eof"] and result["last_line_complete"]
    (tmp_path / "text.txt").write_bytes(b"a" * (MAX_BYTES - 2) + b"\n" + "中".encode())
    result = read(tmp_path)
    assert result["last_line_no"] == 1 and result["last_line_complete"] and not result["eof"]
    assert read(tmp_path, line=2)["text"] == "中"


@pytest.mark.parametrize("line,byte", [(1, 2), (1, 20), (2, 1)])
def test_invalid_cursor(tmp_path, line, byte):
    (tmp_path / "text.txt").write_text("中文\n")
    with pytest.raises(ReadFileError, match="INVALID_POSITION"):
        read(tmp_path, line=line, byte=byte)


@pytest.mark.parametrize("raw", [b"a\x00b", b"\xff", b"\xe4\xb8", b"abc\xe4\n"])
def test_rejects_binary_and_invalid_utf8(tmp_path, raw):
    (tmp_path / "text.txt").write_bytes(raw)
    with pytest.raises(ReadFileError, match="NOT_UTF8_TEXT"):
        read(tmp_path)


def test_workspace_boundary_and_special_files(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (tmp_path / "secret").write_text("secret")
    (root / "link").symlink_to(tmp_path / "secret")
    (root / "directory_link").symlink_to(tmp_path, target_is_directory=True)
    os.mkfifo(root / "fifo")
    for path in ["../secret", str(tmp_path / "secret"), "link", "directory_link/secret", "fifo", "."]:
        with pytest.raises(ReadFileError):
            read(root, path=path)
    (root / "sub").mkdir()
    (root / "sub/text").write_text("safe")
    assert read(root, path="sub/text")["text"] == "safe"


@pytest.mark.anyio
async def test_executor_errors_and_guidance(tmp_path):
    args = {"path": "missing", "start_line_no": 1, "start_byte_no": 1, "total_lines": 1}
    result = await execute_tool("read_file", json.dumps(args), workspace=str(tmp_path))
    assert result == {"error_code": "FILE_NOT_FOUND", "result": {}}
    assert format_result(result).startswith("文件不存在")
    assert "FILE_NOT_FOUND" in format_result(result)
    for invalid in ["{", "[]", json.dumps({**args, "start_byte_no": True}), json.dumps({**args, "extra": 1})]:
        result = await execute_tool("read_file", invalid, workspace=str(tmp_path))
        assert result["error_code"] == "INVALID_ARGUMENTS"
    assert (await execute_tool("missing", "{}", workspace=str(tmp_path)))["error_code"] == "UNKNOWN_TOOL"
