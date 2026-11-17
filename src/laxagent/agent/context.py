from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from laxagent.runtime.profile import StandardProfile, DockerEvalProfile
from laxagent.sandbox.backend import ExecutionBackend

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

    profile: StandardProfile | DockerEvalProfile = field(default_factory=StandardProfile)
    workspace: str = ""
    backend: ExecutionBackend | None = None
    skill_dir: Path | None = None
    sandbox_skill_dir: PurePosixPath | None = None
