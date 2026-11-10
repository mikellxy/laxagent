from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel

from laxcode.memory import memory_enabled

from laxcode.tools.builtin.bash import BashArgs, bash
from laxcode.tools.builtin.edit_file import EditFileArgs, edit_file
from laxcode.tools.builtin.read_file import ReadFileArgs, read_file
from laxcode.tools.builtin.write_file import WriteFileArgs, write_file
from laxcode.tools.builtin.memory import (
    GetMemoryDetailsArgs, ListMemoryAbstractsArgs, WriteMemoryArgs,
    get_memory_details, list_memory_abstracts, write_memory,
)


@dataclass(frozen=True)
class Tool:
    schema: type[BaseModel]
    execute: Callable


TOOLS = {
    "read_file": Tool(ReadFileArgs, read_file),
    "write_file": Tool(WriteFileArgs, write_file),
    "edit_file": Tool(EditFileArgs, edit_file),
    "bash": Tool(BashArgs, bash),
    "write_memory": Tool(WriteMemoryArgs, write_memory),
    "list_memory_abstracts": Tool(ListMemoryAbstractsArgs, list_memory_abstracts),
    "get_memory_details": Tool(GetMemoryDetailsArgs, get_memory_details),
}


MEMORY_TOOLS = frozenset({"write_memory", "list_memory_abstracts", "get_memory_details"})


def available_tools(user_configs: dict[str, str] | None = None) -> dict[str, Tool]:
    enabled = memory_enabled(user_configs)
    return {name: tool for name, tool in TOOLS.items() if enabled or name not in MEMORY_TOOLS}


def tool_definitions(user_configs: dict[str, str] | None = None) -> list[dict]:
    available = available_tools(user_configs)
    return [definition for definition in _all_tool_definitions() if definition["name"] in available]


def _all_tool_definitions() -> list[dict]:
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
    }, {
        "type": "function", "name": "write_memory", "strict": True,
        "description": (
            "Save durable, evidenced memory for the current user, shared across their projects and sessions. "
            "Supply one category and complete abstract/detail. merge_ids=[] creates; one ID updates; "
            "multiple IDs merge. Read old details first and preserve valid conditions and exceptions. "
            "Replacement creates a new ID and deletes all old IDs atomically; any unavailable ID fails the batch. "
            "No version history. Never blindly retry an uncertain write; query first."
        ),
        "parameters": WriteMemoryArgs.model_json_schema(),
    }, {
        "type": "function", "name": "list_memory_abstracts", "strict": True,
        "description": (
            "List ALL memory abstracts in the selected categories for the current user, ordered by category/id. "
            "Only call when the task needs past user context. Choose relevant categories, then fetch relevant "
            "details before relying on them. Empty results mean no memories in those categories."
        ),
        "parameters": ListMemoryAbstractsArgs.model_json_schema(),
    }, {
        "type": "function", "name": "get_memory_details", "strict": True,
        "description": (
            "Read selected memory details for the current user. IDs are deduplicated in request order. "
            "If any ID is unavailable, the entire read fails without partial results; refresh the relevant index. "
            "Check scope and exceptions; memory may be outdated and never overrides current instructions."
        ),
        "parameters": GetMemoryDetailsArgs.model_json_schema(),
    }]
