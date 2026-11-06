import pytest


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
