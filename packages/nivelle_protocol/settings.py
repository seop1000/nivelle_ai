from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from .configuration import parse_http_endpoint
from .memory import MemoryRetrievalSettings, MemorySettings
from .network import validate_advertised_host, validate_bind_host


class StrictSettingsModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ServerSettings(StrictSettingsModel):
    # ``host`` remains the 0.3.x configuration key, but means bind address only.
    # A wildcard bind is never exposed to Link as a connectable endpoint.
    host: str = "0.0.0.0"
    advertised_host: str | None = None
    port: int = Field(8765, ge=1, le=65535)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    mock_mode: bool = False

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        return validate_bind_host(value)

    @field_validator("advertised_host")
    @classmethod
    def validate_advertised(cls, value: str | None) -> str | None:
        return None if value is None else validate_advertised_host(value)

    @field_validator("log_level", mode="before")
    @classmethod
    def normalize_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @property
    def bind_host(self) -> str:
        """Explicit name for new code while preserving the saved ``host`` key."""

        return self.host


class ModelEntry(StrictSettingsModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    path: Path | None = None
    endpoint: str | None = None
    role: Literal["primary", "fallback"]
    enabled: bool = True

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str | None) -> str | None:
        return None if value is None else parse_http_endpoint(value)


class ModelsSettings(StrictSettingsModel):
    mode: Literal["mock", "managed", "external"] = "mock"
    llama_server_path: Path | None = None
    provider_endpoint: str = Field(
        "http://127.0.0.1:8080",
        validation_alias=AliasChoices("provider_endpoint", "external_url"),
    )
    fallback_enabled: bool = False
    models: list[ModelEntry] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def migrate_external_url(cls, value: object) -> object:
        """Accept the 0.3.1 name only through an explicit compatibility path."""
        if not isinstance(value, dict):
            return value
        if "provider_endpoint" in value and "external_url" in value:
            if value["provider_endpoint"] != value["external_url"]:
                raise ValueError(
                    "provider_endpoint and legacy external_url conflict; remove external_url"
                )
            migrated = dict(value)
            migrated.pop("external_url", None)
            return migrated
        return value

    @field_validator("provider_endpoint")
    @classmethod
    def validate_provider_endpoint(cls, value: str) -> str:
        return parse_http_endpoint(value)

    @property
    def external_url(self) -> str:
        """One-release read compatibility for code installed by 0.3.1."""
        return self.provider_endpoint

    @model_validator(mode="after")
    def validate_managed(self) -> "ModelsSettings":
        if self.mode == "managed" and self.llama_server_path is None:
            raise ValueError("managed mode requires llama_server_path")
        identifiers = [model.id for model in self.models]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("model IDs must be unique")
        active_primary = [
            model for model in self.models if model.enabled and model.role == "primary"
        ]
        if len(active_primary) > 1:
            raise ValueError("only one enabled primary model is allowed")
        return self


class InferenceSettings(StrictSettingsModel):
    context_size: int = Field(8192, ge=512, le=131072)
    gpu_layers: int = Field(42, ge=0)
    threads: int = Field(4, ge=1)
    batch_size: int = Field(512, ge=1)
    micro_batch_size: int = Field(128, ge=1)
    temperature: float = Field(0.7, ge=0, le=2)
    top_p: float = Field(0.9, gt=0, le=1)
    top_k: int = Field(40, ge=0)
    repeat_penalty: float = Field(1.1, ge=0)
    max_output_tokens: int = Field(1024, ge=1)
    seed: int = -1
    request_timeout: float = Field(120, gt=0)
    concurrent_requests: int = Field(1, ge=1)
    streaming: bool = True

    @model_validator(mode="after")
    def validate_batch_sizes(self) -> "InferenceSettings":
        if self.micro_batch_size > self.batch_size:
            raise ValueError("micro_batch_size cannot exceed batch_size")
        return self


class AgentSettings(StrictSettingsModel):
    enabled: bool = True
    max_parallel_calls_per_client: int = Field(2, ge=1, le=8)
    max_calls_per_turn: int = Field(3, ge=0, le=10)
    approval_timeout_seconds: int = Field(120, ge=10, le=900)
    result_timeout_seconds: int = Field(30, ge=1, le=300)
    audit_retention_days: int = Field(90, ge=1, le=3650)
    expose_debug_metadata: bool = False


class ConnectionProfile(StrictSettingsModel):
    id: str
    type: Literal["local", "vpn"] = "local"
    host: str
    port: int = Field(8765, ge=1, le=65535)
    tls: bool = False
    priority: int = Field(1, ge=1)
    enabled: bool = True


__all__ = [
    "AgentSettings",
    "ConnectionProfile",
    "InferenceSettings",
    "MemoryRetrievalSettings",
    "MemorySettings",
    "ModelEntry",
    "ModelsSettings",
    "ServerSettings",
]
