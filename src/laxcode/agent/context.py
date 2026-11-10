from dataclasses import dataclass

from openai import AsyncOpenAI

from laxcode.runtime.events import EventStore
from laxcode.storage.repositories import Repository


@dataclass
class AgentContext:
    client: AsyncOpenAI
    events: EventStore
    repository: Repository
    recursion_limit: int = -1
