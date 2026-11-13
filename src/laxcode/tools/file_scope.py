"""Resolve a file-tool scope without exposing arbitrary filesystem roots."""

from contextlib import contextmanager

from laxcode.skills import file_tree, skill_directory
from laxcode.tools.context import ToolContext
from laxcode.tools.errors import ToolError


@contextmanager
def file_scope(context: ToolContext, skill_name: str | None, *, create=False):
    # A common model mistake must not silently create a skill directory and
    # report a successful workspace deliverable. Reject before touching disk.
    if skill_name == "null":
        raise ToolError("INVALID_FILE_SCOPE")
    if skill_name is None:
        yield context.workspace
    else:
        with skill_directory(context.user_id, skill_name, create=create) as directory:
            yield directory


def scope_result(root: str | int, skill_name: str | None, result: dict) -> dict:
    if skill_name is not None:
        result = {**result, "skill_name": skill_name, "files": file_tree(root)}
    return result
