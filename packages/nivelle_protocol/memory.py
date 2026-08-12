import re
from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

MemoryCategory = Literal["preference", "project", "workflow", "instruction", "other"]
MemoryContent = Annotated[str, StringConstraints(min_length=1, max_length=500)]
MemoryDecisionReason = Literal[
    "selected",
    "explicitly_attached",
    "inactive",
    "deleted",
    "superseded",
    "duplicate",
    "low_relevance",
    "top_k_limit",
    "sensitive",
    "conflict_lost",
    "explicitly_excluded",
]

_DIRECT_IDENTIFIER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "email address",
        re.compile(
            r"(?i)(?<![A-Z0-9._%+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}"
            r"(?![A-Z0-9._%+-])"
        ),
    ),
    (
        "Korean resident registration number",
        re.compile(r"(?<!\d)\d{6}\s*-?\s*[1-4]\d{6}(?!\d)"),
    ),
    (
        "phone number",
        re.compile(
            r"(?<!\d)(?:(?:\+?82[- .]?)?0(?:1[016789]|2|[3-6][1-5])[- .]?)"
            r"\d{3,4}[- .]?\d{4}(?!\d)"
        ),
    ),
    (
        "IP address",
        re.compile(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)"),
    ),
    (
        "secret or credential",
        re.compile(
            r"(?i)\b(?:api[_ -]?key|access[_ -]?token|password|passwd|private[_ -]?key)"
            r"\s*[:=]"
        ),
    ),
    (
        "direct identifier label",
        re.compile(
            r"(?i)(?:주민등록번호|여권번호|운전면허번호|생년월일|집\s*주소|"
            r"passport\s*(?:number|no\.?|#)|home\s*address)\s*[:=]"
        ),
    ),
)

_TRANSCRIPT_MARKER = re.compile(
    r"(?im)^\s*(?:user|assistant|system|사용자|어시스턴트|시스템)\s*[:：]"
)


def validate_memory_content(value: str) -> str:
    """Accept a concise user-authored note, never a conversation log or direct identifier."""
    if "\n" in value or "\r" in value or _TRANSCRIPT_MARKER.search(value):
        raise ValueError("conversation transcripts cannot be stored as memories")
    normalized = " ".join(value.split())
    if not normalized:
        raise ValueError("memory content cannot be empty")
    for label, pattern in _DIRECT_IDENTIFIER_PATTERNS:
        if pattern.search(normalized):
            raise ValueError(f"memory contains a prohibited {label}")
    return normalized


class MemoryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: MemoryContent
    category: MemoryCategory = "other"
    active: bool = True
    priority: int = Field(50, ge=0, le=100)

    @field_validator("content", mode="before")
    @classmethod
    def validate_content(cls, value: object) -> object:
        if isinstance(value, str):
            return validate_memory_content(value)
        return value


class MemoryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: MemoryContent | None = None
    category: MemoryCategory | None = None
    active: bool | None = None
    priority: int | None = Field(None, ge=0, le=100)

    @field_validator("content", mode="before")
    @classmethod
    def validate_content(cls, value: object) -> object:
        if isinstance(value, str):
            return validate_memory_content(value)
        return value

    @model_validator(mode="after")
    def require_change(self) -> "MemoryUpdate":
        if not self.model_fields_set:
            raise ValueError("at least one memory field must be supplied")
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("memory fields cannot be null")
        return self


class MemoryRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    content: str
    category: MemoryCategory
    active: bool
    priority: int
    created_at: datetime
    updated_at: datetime
    superseded_by: str | None = None
    superseded_at: datetime | None = None


class MemoryContextItem(BaseModel):
    """One auditable retrieval decision for the current model prompt.

    ``id`` and ``content`` remain accepted as validation aliases so an older
    internal caller can be upgraded independently.  New wire payloads use the
    unambiguous ``memory_id`` and safe ``summary`` names.
    """

    model_config = ConfigDict(extra="forbid")

    memory_id: str = Field(validation_alias=AliasChoices("memory_id", "id"))
    summary: str = Field(
        validation_alias=AliasChoices("summary", "content"), min_length=1, max_length=500
    )
    category: MemoryCategory
    priority: int = Field(ge=0, le=100)
    relevance_score: float = Field(ge=0, le=1)
    priority_score: float = Field(0, ge=0, le=1)
    recency_score: float = Field(0, ge=0, le=1)
    final_score: float = Field(0, ge=0, le=1)
    included: bool = True
    reason: MemoryDecisionReason = "selected"

    @property
    def id(self) -> str:
        """Compatibility accessor for pre-2.1 server code."""

        return self.memory_id

    @property
    def content(self) -> str:
        """Compatibility accessor for pre-2.1 prompt code."""

        return self.summary


class MemoryRetrievalSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    top_k: int = Field(5, ge=0, le=10)
    candidate_limit: int = Field(30, ge=1, le=100)
    minimum_relevance: float = Field(0.12, ge=0, le=1)
    relevance_weight: float = Field(0.70, ge=0, le=1)
    priority_weight: float = Field(0.20, ge=0, le=1)
    recency_weight: float = Field(0.10, ge=0, le=1)
    include_recent_user_messages: int = Field(2, ge=0, le=5)
    exact_phrase_boost: float = Field(0.20, ge=0, le=1)
    substring_boost: float = Field(0.10, ge=0, le=1)
    prefix_boost: float = Field(0.08, ge=0, le=1)
    include_inactive: bool = False
    include_deleted: Literal[False] = False

    @model_validator(mode="after")
    def validate_retrieval_settings(self) -> "MemoryRetrievalSettings":
        if self.candidate_limit < max(self.top_k, 1):
            raise ValueError("candidate_limit must be greater than or equal to top_k")
        weight_sum = self.relevance_weight + self.priority_weight + self.recency_weight
        if abs(weight_sum - 1.0) > 1e-6:
            raise ValueError("memory retrieval weights must add up to 1.0")
        return self


def default_memory_retrieval_settings() -> MemoryRetrievalSettings:
    """Typed factory that also keeps Pydantic/mypy plugin versions compatible."""

    return MemoryRetrievalSettings.model_validate({})


class MemorySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phase: Literal[2] = 2
    enabled: bool = True
    automatic_extraction: bool = False
    prompt_top_k: int = Field(5, ge=0, le=10)
    search_limit: int = Field(30, ge=1, le=100)
    search_backend: Literal["sqlite"] = "sqlite"
    embedding_provider: None = None
    memory_retrieval: MemoryRetrievalSettings = Field(
        default_factory=default_memory_retrieval_settings
    )

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_retrieval_settings(cls, value: object) -> object:
        """Give existing ``memory.yaml`` files deterministic 2.1 defaults.

        Old installations only contain ``prompt_top_k`` and ``search_limit``.
        Their explicit limits remain authoritative until the administrator
        saves the expanded settings document.
        """

        if not isinstance(value, dict) or "memory_retrieval" in value:
            return value
        migrated = dict(value)
        top_k = int(migrated.get("prompt_top_k", 5))
        candidate_limit = max(int(migrated.get("search_limit", 30)), max(top_k, 1))
        migrated["memory_retrieval"] = {
            "top_k": top_k,
            "candidate_limit": candidate_limit,
        }
        return migrated
