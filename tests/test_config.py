import json

import pytest

from laxagent.config import load_config


def test_load_config(config_data, tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config_data))
    config = load_config(path)
    assert config.api_server.port == 8000
    assert config.recursion_limit == -1
    assert config.llm_router.port == 8001
    assert config.llm.default_model == "example:model-name"
    assert config.nats.url == "nats://127.0.0.1:4222"
    assert config.database.name == "laxcode_test"
    assert "test-only-password" not in repr(config)
    assert config.llm.provider_list[0].api_key.get_secret_value() == "sk-private-test"
    assert "sk-private-test" not in repr(config)
    assert config.builtin_skills_dir == path.parent / "builtin_skills"
    assert "builtin_skills_dir" not in config.model_dump()


@pytest.mark.parametrize("limit", [-1, 1, 25])
def test_recursion_limit_config(config_data, tmp_path, limit):
    config_data["recursion_limit"] = limit
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config_data))
    assert load_config(path).recursion_limit == limit


@pytest.mark.parametrize("change", [
    lambda c: c.update(recursion_limit=0),
    lambda c: c.update(recursion_limit=-2),
    lambda c: c.update(recursion_limit=True),
    lambda c: c.update(recursion_limit="25"),
    lambda c: c["api_server"].update(port=True),
    lambda c: c["api_server"].update(port="8000"),
    lambda c: c["api_server"].update(port=0),
    lambda c: c["llm_router"].update(port=65536),
    lambda c: c["llm_router"].update(port=8000),
    lambda c: c["llm"].update(default_model="missing:model"),
    lambda c: c["llm"]["provider_list"].append(c["llm"]["provider_list"][0]),
    lambda c: c["llm"]["provider_list"][0].update(name="bad:name"),
    lambda c: c["llm"]["provider_list"][0].update(api_key=""),
    lambda c: c["llm"]["provider_list"][0].update(base_url="file:///tmp/model"),
    lambda c: c["llm"]["provider_list"][0].update(base_url="https://host/v1?key=sk-private-test"),
    lambda c: c["llm"]["provider_list"][0]["model_list"].append(c["llm"]["provider_list"][0]["model_list"][0]),
    lambda c: c["llm"]["provider_list"][0]["model_list"][0]["limit"].update(context=0),
    lambda c: c["llm"]["provider_list"][0]["model_list"][0]["limit"].update(output=True),
    lambda c: c["llm"]["provider_list"][0]["model_list"][0]["limit"].update(output=128001),
    lambda c: c.update(unknown="sk-private-test"),
    lambda c: c.update(nats={"url": "http://localhost:4222"}),
    lambda c: c.update(nats={"url": "nats://localhost:4222?key=sk-private-test"}),
    lambda c: c.pop("database"),
    lambda c: c.pop("auth"),
    lambda c: c["auth"].update(jwt_secret="short-secret"),
    lambda c: c["database"].update(port=0),
    lambda c: c["database"].update(password=""),
])
def test_rejects_invalid_config_without_exposing_secrets(config_data, tmp_path, change):
    change(config_data)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config_data))
    with pytest.raises(ValueError) as exc:
        load_config(path)
    assert "sk-private-test" not in str(exc.value)


def test_malformed_json_and_missing_file(tmp_path):
    path = tmp_path / "config.json"
    for content in (None, '{"api_key": "sk-private-test", BROKEN'):
        if content is not None:
            path.write_text(content)
        with pytest.raises(ValueError, match="Cannot read configuration"):
            load_config(path)
