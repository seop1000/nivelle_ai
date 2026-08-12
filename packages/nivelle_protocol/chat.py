from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .memory import MemoryContextItem
from .version import PROTOCOL_VERSION


class RuntimeConnectionContext(BaseModel):
    """Client-known connection facts used only as conversational context."""

    model_config = ConfigDict(extra="forbid")

    profile_id: str = Field(min_length=1, max_length=100)
    connection_type: Literal["local", "vpn"]
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(ge=1, le=65535)
    tls: bool
    client_version: str | None = Field(default=None, min_length=1, max_length=50)
    latency_ms: float | None = Field(default=None, ge=0, le=3_600_000)

    @field_validator("profile_id", "host", "client_version", mode="before")
    @classmethod
    def strip_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class ChatRequest(BaseModel):
    type: Literal["chat.request"] = "chat.request"
    protocol_version: str = PROTOCOL_VERSION
    request_id: UUID
    client_message_id: UUID | None = None
    retry_of_client_message_id: UUID | None = None
    conversation_id: UUID | None = None
    content: str = Field(min_length=1, max_length=100_000)
    runtime_context: RuntimeConnectionContext | None = None

    @model_validator(mode="after")
    def provide_client_message_id(self) -> "ChatRequest":
        # Older clients sent only request_id. Treating it as the message ID keeps
        # those requests idempotent without making the new wire field mandatory.
        if self.client_message_id is None:
            self.client_message_id = self.request_id
        return self


class ChatCancel(BaseModel):
    type: Literal["chat.cancel"] = "chat.cancel"
    protocol_version: str = PROTOCOL_VERSION
    request_id: UUID


class RetrievalContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend: str = Field(min_length=1, max_length=50)
    top_k: int = Field(ge=0, le=100)
    candidate_count: int = Field(ge=0)


class ChatContextPayload(BaseModel):
    """Legacy payload accepted by clients that still emit ``chat.context``."""

    model_config = ConfigDict(extra="forbid")

    conversation_id: UUID
    memories: list[MemoryContextItem] = Field(default_factory=list)


class AssistantContextPayload(ChatContextPayload):
    """Auditable Phase 2.1 context emitted before the first response delta."""

    user_message_id: UUID
    assistant_message_id: UUID
    client_message_id: UUID
    query: str = Field(min_length=1, max_length=100_000)
    retrieval: RetrievalContext


class CompletedAssistantMessage(BaseModel):
    """Canonical persisted assistant row carried by a completion event."""

    model_config = ConfigDict(extra="allow")

    id: UUID
    role: Literal["assistant"]
    content: str
    state: Literal["completed"]


class AssistantCompletedPayload(BaseModel):
    """Terminal payload whose redundant IDs must all identify one message."""

    model_config = ConfigDict(extra="forbid")

    conversation_id: UUID
    client_message_id: UUID
    message_id: UUID
    assistant_message_id: UUID
    message: CompletedAssistantMessage
    finish_reason: str | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_canonical_message_id(self) -> "AssistantCompletedPayload":
        if not (
            self.message_id == self.assistant_message_id == self.message.id
        ):
            raise ValueError(
                "message_id, assistant_message_id, and message.id must be identical"
            )
        return self


class ServerEvent(BaseModel):
    type: str
    protocol_version: str = PROTOCOL_VERSION
    request_id: UUID | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
