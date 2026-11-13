from dataclasses import dataclass, field
from openai import AsyncOpenAI

from laxagent.storage.repositories import Repository
from laxagent.web_search import WebSearchService


@dataclass(frozen=True)
class ToolContext:
    """Trusted dependencies supplied by the graph, never by model arguments."""

    user_id: str
    repository: Repository
    workspace: str
    loaded_skills: dict[str, dict] = field(default_factory=dict)
    written_workspace_paths: list[str] = field(default_factory=list)
    web_search: WebSearchService | None = None
    client: AsyncOpenAI | None = None
    appendix_model: str | None = None
    appendix_supports_multimodal: bool = False

    def record_workspace_path(self, path: str) -> None:
        if path not in self.written_workspace_paths:
            self.written_workspace_paths.append(path)
