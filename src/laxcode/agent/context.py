from dataclasses import dataclass

from openai import AsyncOpenAI

from laxcode.runtime.events import EventStore


@dataclass
class AgentContext:
    client: AsyncOpenAI
    events: EventStore
