import json
from pathlib import Path
from typing import Annotated, Self

from pydantic import Field, SecretStr, ValidationError, model_validator

from laxcode.llm_router.config import ConfigModel, LLMConfig


class ServerConfig(ConfigModel):
    port: Annotated[int, Field(ge=1, le=65535)]


class NatsConfig(ConfigModel):
    url: str = Field(default="nats://127.0.0.1:4222", pattern=r"^nats://[^\s/@?#]+:[0-9]+$")


class DatabaseConfig(ConfigModel):
    host: str = Field(min_length=1)
    port: Annotated[int, Field(ge=1, le=65535)]
    name: str = Field(min_length=1)
    user: str = Field(min_length=1)
    password: SecretStr = Field(min_length=1)


class AppConfig(ConfigModel):
    llm: LLMConfig
    api_server: ServerConfig
    llm_router: ServerConfig
    nats: NatsConfig = Field(default_factory=NatsConfig)
    database: DatabaseConfig

    @model_validator(mode="after")
    def check_ports(self) -> Self:
        if self.api_server.port == self.llm_router.port:
            raise ValueError("api_server and llm_router must use different ports")
        return self


def load_config(path: Path) -> AppConfig:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("Cannot read configuration as UTF-8 JSON") from None
    try:
        return AppConfig.model_validate(data)
    except ValidationError as exc:
        # Never include input values: a validation failure can contain API keys.
        details = "; ".join(
            f"{'.'.join(map(str, error['loc'])) or 'config'}: {error['msg']}"
            for error in exc.errors(include_input=False, include_context=False)
        )
        raise ValueError(f"Invalid configuration: {details}") from None
