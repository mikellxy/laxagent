import json
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, PrivateAttr, SecretStr, ValidationError, model_validator

from laxagent.llm_router.config import ConfigModel, LLMConfig


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


class AuthConfig(ConfigModel):
    jwt_secret: SecretStr

    @model_validator(mode="after")
    def check_secret(self) -> Self:
        if len(self.jwt_secret.get_secret_value().encode("utf-8")) < 32:
            raise ValueError("jwt_secret must contain at least 32 bytes")
        return self


class FrontendConfig(ConfigModel):
    dist_dir: str = Field(min_length=1)


class WebSearchConfig(ConfigModel):
    api_key: SecretStr = Field(min_length=1)
    timeout_seconds: float = Field(default=30, gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def check_key(self) -> Self:
        key = self.api_key.get_secret_value()
        if not key.isascii() or not key.isprintable() or not key.strip() or any(char.isspace() for char in key):
            raise ValueError("api_key must contain non-whitespace ASCII characters")
        return self


class AppConfig(ConfigModel):
    _builtin_skills_dir: Path = PrivateAttr(default_factory=lambda: Path("config/builtin_skills").resolve())
    recursion_limit: Literal[-1] | Annotated[int, Field(gt=0)] = -1
    cancel_timeout_seconds: float = Field(default=30, gt=0, allow_inf_nan=False)
    llm: LLMConfig
    api_server: ServerConfig
    llm_router: ServerConfig
    nats: NatsConfig = Field(default_factory=NatsConfig)
    database: DatabaseConfig
    auth: AuthConfig
    frontend: FrontendConfig | None = None
    web_search: WebSearchConfig | None = None

    @property
    def builtin_skills_dir(self) -> Path:
        return self._builtin_skills_dir

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
        config = AppConfig.model_validate(data)
        config._builtin_skills_dir = path.resolve().parent / "builtin_skills"
        if config.frontend is not None:
            config.frontend.dist_dir = str((path.resolve().parent / config.frontend.dist_dir).resolve())
        return config
    except ValidationError as exc:
        # Never include input values: a validation failure can contain API keys.
        details = "; ".join(
            f"{'.'.join(map(str, error['loc'])) or 'config'}: {error['msg']}"
            for error in exc.errors(include_input=False, include_context=False)
        )
        raise ValueError(f"Invalid configuration: {details}") from None
