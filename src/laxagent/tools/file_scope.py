"""Resolve a file-tool scope without exposing arbitrary filesystem roots."""

from contextlib import contextmanager
from typing import Annotated

from pydantic import AfterValidator

from laxagent.skills import file_tree, skill_directory
from laxagent.tools.context import ToolContext

FileSkillName = Annotated[str | None, AfterValidator(lambda value: None if value == "null" else value)]


@contextmanager
def file_scope(context: ToolContext, skill_name: str | None, *, create=False):
    if skill_name is None:
        yield context.workspace
    else:
        with skill_directory(context.user_id, skill_name, create=create) as directory:
            yield directory


def scope_result(root: str | int, skill_name: str | None, result: dict) -> dict:
    if skill_name is not None:
        result = {**result, "skill_name": skill_name, "files": file_tree(root)}
    return result
