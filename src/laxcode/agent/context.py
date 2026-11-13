from dataclasses import dataclass
from pathlib import Path

from openai import AsyncOpenAI

from laxcode.runtime.events import EventStore
from laxcode.storage.repositories import Repository
from laxcode.web_search import WebSearchService


@dataclass
class AgentContext:
    client: AsyncOpenAI
    events: EventStore
    repository: Repository
    recursion_limit: int = -1
    resume_time: int = 0
    builtin_skills_dir: Path | None = None
    web_search: WebSearchService | None = None
