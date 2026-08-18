"""Typed, backward-compatible server status and generation metrics models."""

from __future__ import annotations

from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from .version import APP_VERSION, PROTOCOL_VERSION, RuntimeIdentity


class GenerationMetrics(BaseModel):
    """Observed generation data; missing backend values remain explicitly null."""

    model_config = ConfigDict(populate_by_name=True)

    prompt_tokens: int | None = Field(
        default=None,
        ge=0,
        validation_alias=AliasChoices("prompt_tokens", "input_tokens"),
    )
    completion_tokens: int | None = Field(
        default=None,
        ge=0,
        validation_alias=AliasChoices("completion_tokens", "output_tokens"),
    )
    total_tokens: int | None = Field(default=None, ge=0)
    tokens_per_second: float | None = Field(default=None, ge=0)
    first_token_latency_ms: float | None = Field(default=None, ge=0)
    total_latency_ms: float | None = Field(default=None, ge=0)
    finish_reason: str | None = None
    interrupted: bool = False
    model: str | None = None
    request_id: str | None = None

    @property
    def input_tokens(self) -> int | None:
        """Compatibility spelling used by an early Phase 2.1 draft."""

        return self.prompt_tokens

    @property
    def output_tokens(self) -> int | None:
        """Compatibility spelling used by an early Phase 2.1 draft."""

        return self.completion_tokens


class MemoryDatabaseStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: str
    backend: str = "sqlite"
    search_backend: str = "sqlite_hybrid"
    active_count: int | None = Field(default=None, ge=0)
    inactive_count: int | None = Field(default=None, ge=0)


class EmbeddingStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: str = "unavailable"
    provider: str | None = None
    reason: str | None = "not_configured"


class LLMBackendStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: str
    reachable: bool | None = None
    available: bool | None = None
    url: str | None = None
    status_code: int | None = None
    details: Any = None
    loaded_model: str | None = None
    configured_model: str | None = None
    engine: str | None = None
    quantization: str | None = None
    models_error: str | None = None
    error: str | None = None


class GatewayComponentStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: str
    uptime_seconds: float | None = Field(default=None, ge=0)


class StatusComponents(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gateway: GatewayComponentStatus
    llm: LLMBackendStatus
    memory_database: MemoryDatabaseStatus
    embedding: EmbeddingStatus


class NetworkCandidateStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    interface_index: int
    ipv4: str
    kind: str
    eligible: bool
    reason: str
    gateway: str | None = None
    has_default_route: bool = False
    route_metric: int | None = None
    interface_metric: int | None = None


class SelectedNetworkInterfaceStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    interface_index: int
    kind: str
    ipv4: str
    gateway: str | None = None
    effective_metric: int = Field(ge=0)


class GatewayNetworkStatus(BaseModel):
    """Runtime addresses; configured and auto-detected values stay distinct."""

    model_config = ConfigDict(extra="forbid")

    bind_host: str
    bind_port: int = Field(ge=1, le=65535)
    bind_endpoint: str
    advertised_host: str | None = None
    advertised_endpoint: str | None = None
    advertised_source: str
    selected_interface: SelectedNetworkInterfaceStatus | None = None
    detection_error: str | None = None
    candidates: list[NetworkCandidateStatus] = Field(default_factory=list)


class ServerStatus(BaseModel):
    """Authenticated Nivelle Core status shared with Nivelle Link."""

    model_config = ConfigDict(extra="forbid")

    version: str = APP_VERSION
    app_version: str = APP_VERSION
    protocol_version: str = PROTOCOL_VERSION
    server_id: str | None = None
    client_id: str | None = None
    runtime: RuntimeIdentity | None = None
    version_info: RuntimeIdentity | None = None
    gateway: str = "running"
    pairing_required: bool
    model_name: str | None = None
    configured_model_name: str | None = None
    model_role: str | None = None
    model_mode: str | None = None
    assistant_state: str = "idle"
    llama_server: LLMBackendStatus | None = None
    active_generations: int = Field(0, ge=0)
    last_error: str | None = None
    memory_database: MemoryDatabaseStatus | None = None
    embedding_model: EmbeddingStatus | None = None
    components: StatusComponents | None = None
    network: GatewayNetworkStatus | None = None
    last_request_metrics: GenerationMetrics | None = None
    uptime_seconds: float
    metrics: dict[str, Any] = Field(default_factory=dict)
    agent: dict[str, Any] = Field(default_factory=dict)
