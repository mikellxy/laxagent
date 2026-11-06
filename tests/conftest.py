import os
from uuid import uuid4

import nats
import pytest

from laxcode.runtime.events import EventStore


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def run_store():
    async with EventStore.connect(os.getenv("NATS_URL", "nats://127.0.0.1:4222")) as store:
        run_id = uuid4().int % (2**63 - 1) + 1
        yield store, run_id
        try:
            await store.js.delete_stream(store.stream_name(run_id))
        except nats.js.errors.NotFoundError:
            pass


@pytest.fixture
def config_data():
    return {
        "llm": {
            "default_model": "example:model-name",
            "provider_list": [{
                "name": "example",
                "base_url": "https://provider.example.com/v1",
                "api_key": "sk-private-test",
                "model_list": [{
                    "name": "model-name",
                    "limit": {"context": 128000, "output": 16000},
                }],
            }],
        },
        "api_server": {"port": 8000},
        "llm_router": {"port": 8001},
    }
