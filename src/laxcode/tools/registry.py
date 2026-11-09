from laxcode.tools.builtin.bash import BashArgs, bash
from laxcode.tools.builtin.edit_file import EditFileArgs, edit_file
from laxcode.tools.builtin.read_file import ReadFileArgs, read_file
from laxcode.tools.builtin.write_file import WriteFileArgs, write_file

TOOLS = {"read_file": (ReadFileArgs, read_file), "write_file": (WriteFileArgs, write_file),
         "edit_file": (EditFileArgs, edit_file), "bash": (BashArgs, bash)}


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
    }, {
        "type": "function", "name": "edit_file", "strict": True,
        "description": (
            "Edit an existing UTF-8 file in workspace. Read it first. Each nonempty old_text must uniquely "
            "match the original file, and matched byte ranges must not overlap. Exact matching takes priority; "
            "only if absent, whole-line matching ignores leading/trailing spaces and tabs and accepts LF/CRLF. "
            "Include enough context to disambiguate. new_text is written literally without indentation or "
            "newline conversion; empty deletes. All edits succeed together or the file remains unchanged."
        ),
        "parameters": EditFileArgs.model_json_schema(),
    }, {
        "type": "function", "name": "bash", "strict": True,
        "description": (
            "Run a non-interactive Bash command in a fresh Linux bubblewrap sandbox, starting in workspace. "
            "Only workspace changes persist. /tmp is for intermediate files and disappears on exit; "
            "write all final deliverables to workspace. Host network is shared, system tools are read-only. "
            "No shell state or background processes survive a call. timeout_seconds=null uses 120s (max 300s). "
            "Nonzero command exits are returned with output. Each output stream retains at most 32 KiB "
            "of source bytes (head and tail) plus a truncation marker. Never automatically retry side effects."
        ),
        "parameters": BashArgs.model_json_schema(),
    }]
