from dataclasses import dataclass

from laxcode.storage.repositories import Repository


@dataclass(frozen=True)
class ToolContext:
    """Trusted dependencies supplied by the graph, never by model arguments."""

    user_id: str
    repository: Repository
    workspace: str
