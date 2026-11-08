from laxcode.tools.builtin.read_file import ReadFileArgs, read_file
from laxcode.tools.builtin.write_file import WriteFileArgs, write_file

TOOLS = {"read_file": (ReadFileArgs, read_file), "write_file": (WriteFileArgs, write_file)}


def tool_definitions() -> list[dict]:
    return [{
        "type": "function", "name": "read_file", "strict": True,
        "description": (
            "Read a UTF-8 text file in workspace, at most 2000 lines / 50 KiB. "
            "Offsets count original bytes including LF/CRLF; characters are never split. "
            "If eof=false, continue at last_line_no+1, byte 1 when last_line_complete=true; "
            "otherwise continue at last_line_no, last_byte_no+1. Empty files return line/byte 0."
        ),
        "parameters": ReadFileArgs.model_json_schema(),
    }, {
        "type": "function", "name": "write_file", "strict": True,
        "description": (
            "Create or completely overwrite a UTF-8 text file in workspace, creating missing parent directories. "
            "Use a relative file path without '..' or symlinks. Read existing files before overwriting them. "
            "Content is written exactly as supplied; empty content creates or empties a file."
        ),
        "parameters": WriteFileArgs.model_json_schema(),
    }]
