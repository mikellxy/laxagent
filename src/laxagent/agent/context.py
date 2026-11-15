from dataclasses import dataclass, field
from pathlib import Path

from openai import AsyncOpenAI

from laxagent.runtime.events import EventStore
from laxagent.storage.repositories import Repository
from laxagent.web_search import WebSearchService
from laxagent.llm_router.config import LLMConfig


@dataclass
class AgentContext:
    client: AsyncOpenAI
    events: EventStore
    repository: Repository
    recursion_limit: int = -1
    resume_time: int = 0
    builtin_skills_dir: Path | None = None
    web_search: WebSearchService | None = None
    llm: LLMConfig | None = None
    diagnostic_secrets: tuple[str, ...] = field(default=(), repr=False)
