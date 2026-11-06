from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, SecretStr, model_validator


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class ModelLimit(ConfigModel):
    context: Annotated[int, Field(gt=0)]
    output: Annotated[int, Field(gt=0)]

    @model_validator(mode="after")
    def check_output(self) -> Self:
        if self.output > self.context:
            raise ValueError("output must not exceed context")
        return self


class ModelConfig(ConfigModel):
    name: Annotated[str, Field(min_length=1, pattern=r"^\S+$")]
    limit: ModelLimit


class ProviderConfig(ConfigModel):
    name: Annotated[str, Field(min_length=1, pattern=r"^[^:\s]+$")]
    base_url: HttpUrl = Field(repr=False)
    api_key: SecretStr = Field(min_length=1)
    model_list: Annotated[list[ModelConfig], Field(min_length=1)]

    @model_validator(mode="after")
    def check_provider(self) -> Self:
        if self.base_url.username or self.base_url.password or self.base_url.query or self.base_url.fragment:
            raise ValueError("base_url must not contain credentials, query or fragment")
        names = [model.name for model in self.model_list]
        if len(names) != len(set(names)):
            raise ValueError("model names must be unique within a provider")
        return self


class LLMConfig(ConfigModel):
    default_model: str
    provider_list: Annotated[list[ProviderConfig], Field(min_length=1)]

    @model_validator(mode="after")
    def check_models(self) -> Self:
        names = [provider.name for provider in self.provider_list]
        if len(names) != len(set(names)):
            raise ValueError("provider names must be unique")
        models = {
            f"{provider.name}:{model.name}"
            for provider in self.provider_list
            for model in provider.model_list
        }
        if self.default_model not in models:
            raise ValueError("default_model must refer to a configured model")
        return self
