"""Strict Phase 3 tool protocol and the closed Nivelle Agent registry.

The models in this module describe data crossing the Core/Link boundary.  They
do not execute anything.  In particular, the registry intentionally contains
only the eight Phase 3 tools and has no generic command, executable-path,
delete, overwrite, or automation capability.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator, Mapping
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Literal, Self
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .version import PROTOCOL_VERSION

TOOL_VERSION = "1.0"
MIN_TOOL_TIMEOUT_MS = 100
HARD_MAX_TOOL_TIMEOUT_MS = 300_000
HARD_MAX_RESULT_SIZE_BYTES = 5 * 1024 * 1024
HARD_MAX_ARGUMENT_SIZE_BYTES = 256 * 1024

_TOOL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_TOOL_VERSION_PATTERN = re.compile(r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$")
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_APP_VERSION_PATTERN = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,63}$")
_BUILTIN_TIMEZONES = frozenset({"UTC", "Etc/UTC", "Asia/Seoul"})


class StrictToolModel(BaseModel):
    """Base class for protocol objects that must reject unknown fields."""

    model_config = ConfigDict(
        extra="forbid", validate_assignment=True, revalidate_instances="always"
    )


class RiskLevel(StrEnum):
    SAFE_STATUS = "SAFE_STATUS"
    LOCAL_READ = "LOCAL_READ"
    INTERACTIVE = "INTERACTIVE"
    LOCAL_WRITE = "LOCAL_WRITE"
    UNSUPPORTED_DANGEROUS = "UNSUPPORTED_DANGEROUS"


class ApprovalMode(StrEnum):
    NOT_REQUIRED = "not_required"
    DENY = "deny"
    ALLOW_ONCE = "allow_once"
    ALLOW_SESSION = "allow_session"
    ALLOW_ALWAYS_EXACT = "allow_always_exact"


class ToolStatus(StrEnum):
    PROPOSED = "proposed"
    VALIDATED = "validated"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    VALIDATION_FAILED = "validation_failed"
    DENIED = "denied"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    CLIENT_DISCONNECTED = "client_disconnected"


class ToolEventType(StrEnum):
    PROPOSED = "tool.proposed"
    VALIDATION_FAILED = "tool.validation_failed"
    REQUEST = "tool.request"
    APPROVAL_REQUIRED = "tool.approval_required"
    APPROVED = "tool.approved"
    DENIED = "tool.denied"
    QUEUED = "tool.queued"
    STARTED = "tool.started"
    PROGRESS = "tool.progress"
    COMPLETED = "tool.completed"
    FAILED = "tool.failed"
    CANCELLED = "tool.cancelled"
    TIMED_OUT = "tool.timed_out"
    CLIENT_DISCONNECTED = "tool.client_disconnected"


class ToolErrorCode(StrEnum):
    VALIDATION_FAILED = "validation_failed"
    PERMISSION_DENIED = "permission_denied"
    APPROVAL_DENIED = "approval_denied"
    APPROVAL_EXPIRED = "approval_expired"
    UNSUPPORTED_TOOL = "unsupported_tool"
    TOOL_DISABLED = "tool_disabled"
    CLIENT_OFFLINE = "client_offline"
    TARGET_NOT_FOUND = "target_not_found"
    PATH_NOT_ALLOWED = "path_not_allowed"
    SENSITIVE_PATH = "sensitive_path"
    DUPLICATE_REQUEST = "duplicate_request"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    EXECUTION_FAILED = "execution_failed"
    RESULT_TOO_LARGE = "result_too_large"
    CLIENT_DISCONNECTED = "client_disconnected"


class ToolPlatform(StrEnum):
    WINDOWS = "windows"


class IdempotencyBehavior(StrEnum):
    READ_ONLY = "read_only"
    AT_MOST_ONCE = "at_most_once"


TERMINAL_TOOL_STATUSES: frozenset[ToolStatus] = frozenset(
    {
        ToolStatus.COMPLETED,
        ToolStatus.VALIDATION_FAILED,
        ToolStatus.DENIED,
        ToolStatus.FAILED,
        ToolStatus.CANCELLED,
        ToolStatus.TIMED_OUT,
        ToolStatus.CLIENT_DISCONNECTED,
    }
)

LEGAL_TOOL_TRANSITIONS: Mapping[ToolStatus, frozenset[ToolStatus]] = MappingProxyType(
    {
        ToolStatus.PROPOSED: frozenset(
            {
                ToolStatus.VALIDATED,
                ToolStatus.VALIDATION_FAILED,
                ToolStatus.CLIENT_DISCONNECTED,
            }
        ),
        ToolStatus.VALIDATED: frozenset(
            {
                ToolStatus.AWAITING_APPROVAL,
                # SAFE_STATUS uses the explicit no-approval execution path.
                ToolStatus.QUEUED,
                ToolStatus.CLIENT_DISCONNECTED,
            }
        ),
        ToolStatus.AWAITING_APPROVAL: frozenset(
            {
                ToolStatus.APPROVED,
                ToolStatus.DENIED,
                ToolStatus.TIMED_OUT,
                ToolStatus.CLIENT_DISCONNECTED,
            }
        ),
        ToolStatus.APPROVED: frozenset({ToolStatus.QUEUED, ToolStatus.CLIENT_DISCONNECTED}),
        ToolStatus.QUEUED: frozenset(
            {ToolStatus.RUNNING, ToolStatus.CANCELLED, ToolStatus.CLIENT_DISCONNECTED}
        ),
        ToolStatus.RUNNING: frozenset(
            {
                ToolStatus.COMPLETED,
                ToolStatus.FAILED,
                ToolStatus.CANCELLED,
                ToolStatus.TIMED_OUT,
                ToolStatus.CLIENT_DISCONNECTED,
            }
        ),
        **{status: frozenset() for status in TERMINAL_TOOL_STATUSES},
    }
)


class ToolProtocolError(ValueError):
    """Base exception for registry and state-machine validation failures."""


class DuplicateToolRegistrationError(ToolProtocolError):
    pass


class UnknownToolError(ToolProtocolError):
    pass


class UnsupportedToolVersionError(ToolProtocolError):
    pass


class InvalidToolStateTransitionError(ToolProtocolError):
    pass


def validate_tool_state_transition(current: ToolStatus, target: ToolStatus) -> None:
    """Raise if ``current -> target`` is not a legal durable state transition."""

    if target not in LEGAL_TOOL_TRANSITIONS[current]:
        raise InvalidToolStateTransitionError(
            f"Invalid tool state transition: {current.value} -> {target.value}"
        )


def is_valid_tool_state_transition(current: ToolStatus, target: ToolStatus) -> bool:
    """Return whether a durable tool call may move between the two states."""

    return target in LEGAL_TOOL_TRANSITIONS[current]


def _validate_protocol_version(value: str) -> str:
    value = value.strip()
    if value != PROTOCOL_VERSION:
        raise ValueError(f"Unsupported protocol version {value!r}; expected {PROTOCOL_VERSION!r}")
    return value


def _validate_aware_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a UTC offset")
    return value


def _validate_json_size(value: object, *, maximum: int, label: str) -> object:
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be finite JSON data") from exc
    if len(encoded) > maximum:
        raise ValueError(f"{label} exceeds the {maximum}-byte protocol limit")
    return value


def _clean_summary(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("text must contain a non-whitespace character")
    return _reject_unsafe_controls(value)


def _reject_unsafe_controls(value: str) -> str:
    if any(ord(character) < 32 and character not in "\t\r\n" for character in value):
        raise ValueError("text contains unsafe control characters")
    return value


class ProtocolToolMessage(StrictToolModel):
    protocol_version: str = PROTOCOL_VERSION

    _supported_protocol = field_validator("protocol_version")(_validate_protocol_version)


class ToolRequest(ProtocolToolMessage):
    type: Literal["tool.request"] = "tool.request"
    tool_call_id: UUID
    request_id: UUID
    idempotency_key: UUID
    conversation_id: UUID
    user_message_id: UUID
    target_client_id: UUID
    target_session_id: UUID
    tool_name: str = Field(min_length=1, max_length=64, pattern=_TOOL_NAME_PATTERN.pattern)
    tool_version: str = Field(min_length=3, max_length=32, pattern=_TOOL_VERSION_PATTERN.pattern)
    arguments: dict[str, Any]
    risk_level: RiskLevel
    created_at: datetime
    timeout_ms: int = Field(ge=MIN_TOOL_TIMEOUT_MS, le=HARD_MAX_TOOL_TIMEOUT_MS)
    user_intent_summary: str = Field(min_length=1, max_length=1_000)

    _aware_created_at = field_validator("created_at")(_validate_aware_datetime)
    _safe_intent = field_validator("user_intent_summary")(_clean_summary)

    @field_validator("arguments")
    @classmethod
    def bound_arguments(cls, value: dict[str, Any]) -> dict[str, Any]:
        _validate_json_size(value, maximum=HARD_MAX_ARGUMENT_SIZE_BYTES, label="arguments")
        return value


class ToolResult(ProtocolToolMessage):
    type: Literal["tool.result"] = "tool.result"
    result_id: UUID
    source_tool: str = Field(min_length=1, max_length=64, pattern=_TOOL_NAME_PATTERN.pattern)
    trusted: Literal[False] = False
    tool_call_id: UUID
    request_id: UUID
    target_client_id: UUID
    target_session_id: UUID
    tool_name: str = Field(min_length=1, max_length=64, pattern=_TOOL_NAME_PATTERN.pattern)
    tool_version: str = Field(min_length=3, max_length=32, pattern=_TOOL_VERSION_PATTERN.pattern)
    status: ToolStatus
    started_at: datetime | None
    completed_at: datetime
    duration_ms: int | None = Field(default=None, ge=0, le=HARD_MAX_TOOL_TIMEOUT_MS)
    result: dict[str, Any] | None = None
    safe_summary: str = Field(min_length=1, max_length=4_000)
    truncated: bool = False
    original_size: int | None = Field(default=None, ge=0)
    returned_size: int | None = Field(default=None, ge=0)
    omitted_count: int | None = Field(default=None, ge=0)
    error_code: ToolErrorCode | None = None
    error_message: str | None = Field(default=None, min_length=1, max_length=2_000)
    retryable: bool = False

    _aware_completed_at = field_validator("completed_at")(_validate_aware_datetime)
    _safe_summary = field_validator("safe_summary")(_clean_summary)

    @field_validator("error_message")
    @classmethod
    def safe_error_message(cls, value: str | None) -> str | None:
        return None if value is None else _clean_summary(value)

    @field_validator("started_at")
    @classmethod
    def aware_started_at(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _validate_aware_datetime(value)

    @field_validator("result")
    @classmethod
    def bound_result(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is not None:
            _validate_json_size(value, maximum=HARD_MAX_RESULT_SIZE_BYTES, label="result")
        return value

    @model_validator(mode="after")
    def validate_terminal_result(self) -> Self:
        allowed = {
            ToolStatus.COMPLETED,
            ToolStatus.DENIED,
            ToolStatus.FAILED,
            ToolStatus.CANCELLED,
            ToolStatus.TIMED_OUT,
            ToolStatus.CLIENT_DISCONNECTED,
        }
        if self.status not in allowed:
            raise ValueError("ToolResult status must be an execution-terminal status")
        if self.source_tool != self.tool_name:
            raise ValueError("source_tool must match tool_name")
        if self.completed_at and self.started_at and self.completed_at < self.started_at:
            raise ValueError("completed_at cannot precede started_at")
        if self.status is ToolStatus.COMPLETED:
            if self.result is None:
                raise ValueError("completed tool results require result data")
            if self.error_code is not None or self.error_message is not None:
                raise ValueError("completed tool results cannot contain an error")
            if self.retryable:
                raise ValueError("a completed result cannot be retryable")
        else:
            if self.result is not None:
                raise ValueError("non-success tool results cannot contain result data")
            if self.error_code is None or self.error_message is None:
                raise ValueError("non-success tool results require an error code and message")
            expected_codes: Mapping[ToolStatus, frozenset[ToolErrorCode]] = {
                ToolStatus.DENIED: frozenset(
                    {
                        ToolErrorCode.PERMISSION_DENIED,
                        ToolErrorCode.APPROVAL_DENIED,
                        ToolErrorCode.APPROVAL_EXPIRED,
                    }
                ),
                ToolStatus.CANCELLED: frozenset({ToolErrorCode.CANCELLED}),
                ToolStatus.TIMED_OUT: frozenset(
                    {ToolErrorCode.TIMED_OUT, ToolErrorCode.APPROVAL_EXPIRED}
                ),
                ToolStatus.CLIENT_DISCONNECTED: frozenset(
                    {ToolErrorCode.CLIENT_DISCONNECTED, ToolErrorCode.CLIENT_OFFLINE}
                ),
            }
            status_codes = expected_codes.get(self.status)
            if status_codes is not None and self.error_code not in status_codes:
                raise ValueError(f"{self.status.value} result has an incompatible error code")
        if self.truncated:
            if self.original_size is None or self.returned_size is None:
                raise ValueError("truncated results require original_size and returned_size")
            if self.returned_size > self.original_size:
                raise ValueError("returned_size cannot exceed original_size")
        if (
            self.original_size is not None
            and self.returned_size is not None
            and self.returned_size > self.original_size
        ):
            raise ValueError("returned_size cannot exceed original_size")
        return self


class ToolApprovalDecision(StrictToolModel):
    approval_id: UUID
    mode: ApprovalMode
    decided_at: datetime
    expires_at: datetime | None = None
    exact_target: str | None = Field(default=None, min_length=1, max_length=1_000)
    normalized_argument_scope: str | None = Field(default=None, min_length=1, max_length=4_000)
    policy_version: str = Field(min_length=1, max_length=64)
    reason: str | None = Field(default=None, min_length=1, max_length=1_000)

    _aware_decided_at = field_validator("decided_at")(_validate_aware_datetime)

    @field_validator("exact_target", "normalized_argument_scope", "reason")
    @classmethod
    def safe_optional_text(cls, value: str | None) -> str | None:
        return None if value is None else _clean_summary(value)

    @field_validator("expires_at")
    @classmethod
    def aware_expiration(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _validate_aware_datetime(value)

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        if self.expires_at is not None and self.expires_at <= self.decided_at:
            raise ValueError("expires_at must be after decided_at")
        if self.mode is ApprovalMode.ALLOW_ALWAYS_EXACT and (
            self.exact_target is None or self.normalized_argument_scope is None
        ):
            raise ValueError("persistent approval requires an exact target and argument scope")
        return self


class ToolProgress(StrictToolModel):
    sequence: int = Field(ge=1)
    completed_units: int = Field(ge=0)
    total_units: int | None = Field(default=None, ge=1)
    unit: str = Field(default="items", min_length=1, max_length=32)
    message: str | None = Field(default=None, min_length=1, max_length=500)

    @field_validator("message")
    @classmethod
    def safe_message(cls, value: str | None) -> str | None:
        return None if value is None else _clean_summary(value)

    @model_validator(mode="after")
    def validate_units(self) -> Self:
        if self.total_units is not None and self.completed_units > self.total_units:
            raise ValueError("completed_units cannot exceed total_units")
        return self


_EVENT_STATUS: Mapping[ToolEventType, ToolStatus] = MappingProxyType(
    {
        ToolEventType.PROPOSED: ToolStatus.PROPOSED,
        ToolEventType.VALIDATION_FAILED: ToolStatus.VALIDATION_FAILED,
        ToolEventType.REQUEST: ToolStatus.VALIDATED,
        ToolEventType.APPROVAL_REQUIRED: ToolStatus.AWAITING_APPROVAL,
        ToolEventType.APPROVED: ToolStatus.APPROVED,
        ToolEventType.DENIED: ToolStatus.DENIED,
        ToolEventType.QUEUED: ToolStatus.QUEUED,
        ToolEventType.STARTED: ToolStatus.RUNNING,
        ToolEventType.PROGRESS: ToolStatus.RUNNING,
        ToolEventType.COMPLETED: ToolStatus.COMPLETED,
        ToolEventType.FAILED: ToolStatus.FAILED,
        ToolEventType.CANCELLED: ToolStatus.CANCELLED,
        ToolEventType.TIMED_OUT: ToolStatus.TIMED_OUT,
        ToolEventType.CLIENT_DISCONNECTED: ToolStatus.CLIENT_DISCONNECTED,
    }
)

_ERROR_EVENTS = frozenset(
    {
        ToolEventType.VALIDATION_FAILED,
        ToolEventType.DENIED,
        ToolEventType.FAILED,
        ToolEventType.CANCELLED,
        ToolEventType.TIMED_OUT,
        ToolEventType.CLIENT_DISCONNECTED,
    }
)


class ToolEvent(ProtocolToolMessage):
    type: ToolEventType
    tool_call_id: UUID
    request_id: UUID
    target_client_id: UUID
    target_session_id: UUID
    status: ToolStatus
    occurred_at: datetime
    approval: ToolApprovalDecision | None = None
    progress: ToolProgress | None = None
    error_code: ToolErrorCode | None = None
    error_message: str | None = Field(default=None, min_length=1, max_length=2_000)

    _aware_occurred_at = field_validator("occurred_at")(_validate_aware_datetime)

    @field_validator("error_message")
    @classmethod
    def safe_error_message(cls, value: str | None) -> str | None:
        return None if value is None else _clean_summary(value)

    @model_validator(mode="after")
    def validate_event_shape(self) -> Self:
        expected_status = _EVENT_STATUS[self.type]
        if self.status is not expected_status:
            raise ValueError(f"{self.type.value} requires status={expected_status.value}")
        if self.type is ToolEventType.PROGRESS:
            if self.progress is None:
                raise ValueError("tool.progress requires progress data")
        elif self.progress is not None:
            raise ValueError("progress data is allowed only on tool.progress")
        if self.type in {ToolEventType.APPROVED, ToolEventType.DENIED}:
            if self.approval is None:
                raise ValueError(f"{self.type.value} requires an approval decision")
            if self.type is ToolEventType.DENIED and self.approval.mode is not ApprovalMode.DENY:
                raise ValueError("tool.denied requires decision=deny")
            if self.type is ToolEventType.APPROVED and self.approval.mode is ApprovalMode.DENY:
                raise ValueError("tool.approved cannot contain decision=deny")
        elif self.approval is not None:
            raise ValueError("approval data is allowed only on approved or denied events")
        if self.type in _ERROR_EVENTS:
            if self.error_code is None or self.error_message is None:
                raise ValueError(f"{self.type.value} requires error details")
        elif self.error_code is not None or self.error_message is not None:
            raise ValueError("error details are allowed only on failure events")
        expected_error_codes: Mapping[ToolEventType, frozenset[ToolErrorCode]] = {
            ToolEventType.VALIDATION_FAILED: frozenset(
                {
                    ToolErrorCode.VALIDATION_FAILED,
                    ToolErrorCode.UNSUPPORTED_TOOL,
                    ToolErrorCode.TOOL_DISABLED,
                    ToolErrorCode.TARGET_NOT_FOUND,
                    ToolErrorCode.PATH_NOT_ALLOWED,
                    ToolErrorCode.SENSITIVE_PATH,
                    ToolErrorCode.DUPLICATE_REQUEST,
                }
            ),
            ToolEventType.DENIED: frozenset(
                {ToolErrorCode.PERMISSION_DENIED, ToolErrorCode.APPROVAL_DENIED}
            ),
            ToolEventType.CANCELLED: frozenset({ToolErrorCode.CANCELLED}),
            ToolEventType.TIMED_OUT: frozenset(
                {ToolErrorCode.TIMED_OUT, ToolErrorCode.APPROVAL_EXPIRED}
            ),
            ToolEventType.CLIENT_DISCONNECTED: frozenset({ToolErrorCode.CLIENT_DISCONNECTED}),
        }
        allowed_error_codes = expected_error_codes.get(self.type)
        if allowed_error_codes is not None and self.error_code not in allowed_error_codes:
            raise ValueError(f"{self.type.value} has an incompatible error code")
        return self


def validate_tool_event_transition(previous: ToolStatus, event: ToolEvent) -> None:
    """Validate the state change represented by an event.

    Progress is a bounded observation while the call remains RUNNING and is the
    only event that is intentionally allowed to keep the current state.
    """

    if event.type is ToolEventType.PROGRESS and previous is ToolStatus.RUNNING:
        return
    validate_tool_state_transition(previous, event.status)


class EmptyToolArguments(StrictToolModel):
    """A deliberately empty argument object for metadata-only status tools."""


class GetSystemStatusArguments(EmptyToolArguments):
    pass


class DiskVolumeStatus(StrictToolModel):
    volume_name: str = Field(min_length=1, max_length=100)
    total_bytes: int = Field(ge=0)
    free_bytes: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_capacity(self) -> Self:
        if self.free_bytes > self.total_bytes:
            raise ValueError("free_bytes cannot exceed total_bytes")
        return self


class BatteryStatus(StrictToolModel):
    percent: float | None = Field(default=None, ge=0, le=100)
    plugged_in: bool | None = None
    seconds_remaining: int | None = Field(default=None, ge=0)


class SafeNetworkSummary(StrictToolModel):
    connected: bool | None = None
    connection_type: Literal["ethernet", "wifi", "vpn", "other", "unknown"] | None = None
    internet_reachable: bool | None = None


class GetSystemStatusResult(StrictToolModel):
    operating_system: str | None = Field(default=None, max_length=200)
    client_display_name: str = Field(min_length=1, max_length=100)
    cpu_usage_percent: float | None = Field(default=None, ge=0, le=100)
    ram_usage_percent: float | None = Field(default=None, ge=0, le=100)
    ram_total_bytes: int | None = Field(default=None, ge=0)
    ram_available_bytes: int | None = Field(default=None, ge=0)
    disk_volumes: list[DiskVolumeStatus] = Field(default_factory=list, max_length=64)
    battery: BatteryStatus | None = None
    network: SafeNetworkSummary | None = None
    link_uptime_seconds: float = Field(ge=0)
    link_version: str = Field(min_length=1, max_length=64)
    unsupported_metrics: list[str] = Field(default_factory=list, max_length=32)

    @field_validator("operating_system")
    @classmethod
    def safe_operating_system(cls, value: str | None) -> str | None:
        return None if value is None else _reject_unsafe_controls(value)

    @field_validator("client_display_name")
    @classmethod
    def safe_client_display_name(cls, value: str) -> str:
        return _reject_unsafe_controls(value)

    @field_validator("unsupported_metrics")
    @classmethod
    def safe_unsupported_metrics(cls, value: list[str]) -> list[str]:
        return [_clean_summary(item) for item in value]

    @model_validator(mode="after")
    def validate_ram(self) -> Self:
        if (
            self.ram_total_bytes is not None
            and self.ram_available_bytes is not None
            and self.ram_available_bytes > self.ram_total_bytes
        ):
            raise ValueError("ram_available_bytes cannot exceed ram_total_bytes")
        return self


class GetActiveWindowArguments(EmptyToolArguments):
    pass


class GetActiveWindowResult(StrictToolModel):
    source_tool: Literal["get_active_window"] = "get_active_window"
    trusted: Literal[False] = False
    result_id: UUID
    window_found: bool
    title: str | None = Field(default=None, max_length=1_000)
    process_name: str | None = Field(default=None, max_length=260)
    process_id: int | None = Field(default=None, ge=0, le=4_294_967_295)
    executable_basename: str | None = Field(default=None, max_length=260)
    timestamp: datetime

    _aware_timestamp = field_validator("timestamp")(_validate_aware_datetime)

    @field_validator("title", "process_name", "executable_basename")
    @classmethod
    def safe_window_metadata(cls, value: str | None) -> str | None:
        return None if value is None else _reject_unsafe_controls(value)

    @model_validator(mode="after")
    def validate_no_window(self) -> Self:
        metadata = (self.title, self.process_name, self.process_id, self.executable_basename)
        if not self.window_found and any(value is not None for value in metadata):
            raise ValueError("window metadata must be null when no foreground window exists")
        return self


class OpenApplicationArguments(StrictToolModel):
    application_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN.pattern)


class OpenApplicationResult(StrictToolModel):
    application_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN.pattern)
    launched: Literal[True] = True
    process_id: int | None = Field(default=None, ge=0, le=4_294_967_295)
    already_executed: bool = False


class OpenFolderArguments(StrictToolModel):
    path_ref: str | None = Field(default=None, min_length=1, max_length=512)
    path: str | None = Field(default=None, min_length=1, max_length=32_767)

    @model_validator(mode="after")
    def require_one_path_source(self) -> Self:
        if (self.path_ref is None) == (self.path is None):
            raise ValueError("provide exactly one of path_ref or path")
        return self


class OpenFolderResult(StrictToolModel):
    source_tool: Literal["open_folder"] = "open_folder"
    trusted: Literal[False] = False
    result_id: UUID
    path_ref: str = Field(min_length=1, max_length=512)
    safe_path_summary: str = Field(min_length=1, max_length=1_000)
    opened: Literal[True] = True
    already_executed: bool = False

    @field_validator("path_ref", "safe_path_summary")
    @classmethod
    def safe_folder_metadata(cls, value: str) -> str:
        return _reject_unsafe_controls(value)


class SearchFilesArguments(StrictToolModel):
    query: str = Field(min_length=1, max_length=200)
    root_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN.pattern)
    extensions: list[str] = Field(default_factory=list, max_length=32)
    include_directories: bool = False
    max_results: int = Field(default=50, ge=1, le=200)
    max_depth: int = Field(default=8, ge=0, le=8)

    _safe_query = field_validator("query")(_clean_summary)

    @field_validator("extensions")
    @classmethod
    def normalize_extensions(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for extension in value:
            candidate = extension.strip().lower().removeprefix(".")
            if not re.fullmatch(r"[a-z0-9][a-z0-9_+-]{0,15}", candidate):
                raise ValueError(f"invalid file extension: {extension!r}")
            if candidate not in normalized:
                normalized.append(candidate)
        return normalized


class SearchFileItem(StrictToolModel):
    path_ref: str = Field(min_length=1, max_length=512)
    name: str = Field(min_length=1, max_length=260)
    relative_path: str = Field(min_length=1, max_length=32_767)
    type: Literal["file", "directory"]
    size_bytes: int | None = Field(default=None, ge=0)
    modified_at: datetime | None = None

    @field_validator("path_ref", "name", "relative_path")
    @classmethod
    def safe_path_metadata(cls, value: str) -> str:
        return _reject_unsafe_controls(value)

    @field_validator("modified_at")
    @classmethod
    def aware_modified_at(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _validate_aware_datetime(value)

    @model_validator(mode="after")
    def validate_item_type(self) -> Self:
        if self.type == "directory" and self.size_bytes is not None:
            raise ValueError("directory search results cannot report a file size")
        return self


class SearchFilesResult(StrictToolModel):
    source_tool: Literal["search_files"] = "search_files"
    trusted: Literal[False] = False
    result_id: UUID
    root_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN.pattern)
    query: str = Field(min_length=1, max_length=200)
    items: list[SearchFileItem] = Field(max_length=200)
    truncated: bool
    original_size: int | None = Field(default=None, ge=0)
    returned_size: int = Field(ge=0)
    omitted_count: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_limits(self) -> Self:
        if self.returned_size != len(self.items):
            raise ValueError("returned_size must equal the number of returned items")
        if self.original_size is not None and self.returned_size > self.original_size:
            raise ValueError("returned_size cannot exceed original_size")
        if self.truncated and self.original_size is None and self.omitted_count is None:
            raise ValueError("truncation requires original_size or omitted_count")
        return self


class ReadTextFileArguments(StrictToolModel):
    path_ref: str | None = Field(default=None, min_length=1, max_length=512)
    path: str | None = Field(default=None, min_length=1, max_length=32_767)
    start_line: int = Field(default=1, ge=1)
    max_lines: int = Field(default=1_000, ge=1, le=10_000)
    max_characters: int = Field(default=32_000, ge=1, le=100_000)

    @model_validator(mode="after")
    def require_one_path_source(self) -> Self:
        if (self.path_ref is None) == (self.path is None):
            raise ValueError("provide exactly one of path_ref or path")
        return self


class ReadTextFileResult(StrictToolModel):
    source_tool: Literal["read_text_file"] = "read_text_file"
    trusted: Literal[False] = False
    result_id: UUID
    path_ref: str = Field(min_length=1, max_length=512)
    content: str = Field(max_length=100_000)
    encoding: str | None = Field(default=None, max_length=64)
    encoding_uncertain: bool
    start_line: int = Field(ge=1)
    returned_lines: int = Field(ge=0, le=10_000)
    has_more: bool
    truncated: bool
    original_size: int | None = Field(default=None, ge=0)
    returned_size: int = Field(ge=0)
    omitted_count: int | None = Field(default=None, ge=0)

    @field_validator("path_ref", "content")
    @classmethod
    def safe_read_data(cls, value: str) -> str:
        return _reject_unsafe_controls(value)

    @model_validator(mode="after")
    def validate_content_size(self) -> Self:
        actual_size = len(self.content.encode("utf-8"))
        if self.returned_size != actual_size:
            raise ValueError("returned_size must equal UTF-8 content size")
        if self.original_size is not None and self.returned_size > self.original_size:
            raise ValueError("returned_size cannot exceed original_size")
        return self


class CreateNoteArguments(StrictToolModel):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(max_length=100_000)
    format: Literal["txt", "md"] = "txt"

    _safe_title = field_validator("title")(_clean_summary)

    @field_validator("content")
    @classmethod
    def reject_nul_content(cls, value: str) -> str:
        return _reject_unsafe_controls(value)


class CreateNoteResult(StrictToolModel):
    note_id: UUID
    title: str = Field(min_length=1, max_length=200)
    format: Literal["txt", "md"]
    path_ref: str = Field(min_length=1, max_length=512)
    safe_path_summary: str = Field(min_length=1, max_length=1_000)
    size_bytes: int = Field(ge=0)
    already_executed: bool = False


class SetReminderArguments(StrictToolModel):
    title: str = Field(min_length=1, max_length=200)
    reminder_text: str = Field(min_length=1, max_length=10_000)
    scheduled_at: datetime
    timezone: str = Field(min_length=1, max_length=64)

    _safe_title = field_validator("title")(_clean_summary)
    _safe_reminder_text = field_validator("reminder_text")(_reject_unsafe_controls)
    _aware_scheduled_at = field_validator("scheduled_at")(_validate_aware_datetime)

    @model_validator(mode="after")
    def require_future_time(self) -> Self:
        if self.scheduled_at <= datetime.now(UTC):
            raise ValueError("scheduled_at must be in the future")
        return self

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        value = value.strip()
        # Windows does not ship the IANA database used by ``zoneinfo``.  Keep
        # the product's supported local zone and UTC available even when the
        # optional tzdata wheel is absent; all other names must resolve through
        # an installed IANA database.
        if value in _BUILTIN_TIMEZONES:
            return value
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
        return value


class SetReminderResult(StrictToolModel):
    reminder_id: UUID
    title: str = Field(min_length=1, max_length=200)
    scheduled_at: datetime
    timezone: str = Field(min_length=1, max_length=64)
    created: Literal[True] = True
    already_executed: bool = False

    _aware_scheduled_at = field_validator("scheduled_at")(_validate_aware_datetime)


ToolArgumentsModel = (
    GetSystemStatusArguments
    | GetActiveWindowArguments
    | OpenApplicationArguments
    | OpenFolderArguments
    | SearchFilesArguments
    | ReadTextFileArguments
    | CreateNoteArguments
    | SetReminderArguments
)

ToolResultModel = (
    GetSystemStatusResult
    | GetActiveWindowResult
    | OpenApplicationResult
    | OpenFolderResult
    | SearchFilesResult
    | ReadTextFileResult
    | CreateNoteResult
    | SetReminderResult
)


class ToolDefinition(StrictToolModel):
    """One immutable server/client definition in the shared closed registry."""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        revalidate_instances="always",
        arbitrary_types_allowed=True,
        frozen=True,
    )

    name: str = Field(min_length=1, max_length=64, pattern=_TOOL_NAME_PATTERN.pattern)
    version: str = Field(min_length=3, max_length=32, pattern=_TOOL_VERSION_PATTERN.pattern)
    display_name: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=1_000)
    argument_schema: type[BaseModel] = Field(exclude=True)
    result_schema: type[BaseModel] = Field(exclude=True)
    risk_level: RiskLevel
    default_approval_mode: ApprovalMode
    supported_platforms: frozenset[ToolPlatform] = Field(min_length=1)
    default_timeout_ms: int = Field(ge=MIN_TOOL_TIMEOUT_MS, le=HARD_MAX_TOOL_TIMEOUT_MS)
    maximum_timeout_ms: int = Field(ge=MIN_TOOL_TIMEOUT_MS, le=HARD_MAX_TOOL_TIMEOUT_MS)
    maximum_result_size_bytes: int = Field(ge=1, le=HARD_MAX_RESULT_SIZE_BYTES)
    maximum_result_items: int | None = Field(default=None, ge=1, le=10_000)
    maximum_result_characters: int | None = Field(default=None, ge=1, le=1_000_000)
    maximum_result_lines: int | None = Field(default=None, ge=1, le=100_000)
    cancellation_supported: bool
    idempotency_behavior: IdempotencyBehavior
    persistent_approval_supported: bool
    client_implementation_id: str = Field(
        min_length=1, max_length=128, pattern=r"^nivelle_agent\.[a-z][a-z0-9_]{0,63}$"
    )

    @model_validator(mode="after")
    def validate_definition_limits(self) -> Self:
        if self.default_timeout_ms > self.maximum_timeout_ms:
            raise ValueError("default timeout cannot exceed maximum timeout")
        if self.risk_level is RiskLevel.UNSUPPORTED_DANGEROUS:
            raise ValueError("dangerous tools cannot be registered")
        if self.risk_level is RiskLevel.LOCAL_WRITE and self.persistent_approval_supported:
            raise ValueError("LOCAL_WRITE tools cannot support persistent approval")
        return self

    def arguments_json_schema(self) -> dict[str, Any]:
        return self.argument_schema.model_json_schema()

    def result_json_schema(self) -> dict[str, Any]:
        return self.result_schema.model_json_schema()

    def model_tool_definition(self) -> dict[str, Any]:
        """Return a strict OpenAI-compatible function definition."""

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.arguments_json_schema(),
                "strict": True,
            },
        }


class ToolCapability(StrictToolModel):
    tool_name: str = Field(min_length=1, max_length=64, pattern=_TOOL_NAME_PATTERN.pattern)
    tool_version: str = Field(min_length=3, max_length=32, pattern=_TOOL_VERSION_PATTERN.pattern)
    enabled: bool
    implementation_available: bool
    risk_level: RiskLevel
    default_approval_mode: ApprovalMode
    default_timeout_ms: int = Field(ge=MIN_TOOL_TIMEOUT_MS, le=HARD_MAX_TOOL_TIMEOUT_MS)
    maximum_timeout_ms: int = Field(ge=MIN_TOOL_TIMEOUT_MS, le=HARD_MAX_TOOL_TIMEOUT_MS)
    maximum_result_size_bytes: int = Field(ge=1, le=HARD_MAX_RESULT_SIZE_BYTES)
    maximum_result_items: int | None = Field(default=None, ge=1, le=10_000)
    maximum_result_characters: int | None = Field(default=None, ge=1, le=1_000_000)
    maximum_result_lines: int | None = Field(default=None, ge=1, le=100_000)
    cancellation_supported: bool
    idempotency_behavior: IdempotencyBehavior
    persistent_approval_supported: bool
    client_implementation_id: str = Field(
        min_length=1, max_length=128, pattern=r"^nivelle_agent\.[a-z][a-z0-9_]{0,63}$"
    )

    @model_validator(mode="after")
    def validate_capability(self) -> Self:
        if self.enabled and not self.implementation_available:
            raise ValueError("an unavailable implementation cannot be enabled")
        if self.default_timeout_ms > self.maximum_timeout_ms:
            raise ValueError("default timeout cannot exceed maximum timeout")
        if self.risk_level is RiskLevel.UNSUPPORTED_DANGEROUS:
            raise ValueError("dangerous tool capabilities cannot be advertised")
        if self.risk_level is RiskLevel.LOCAL_WRITE and self.persistent_approval_supported:
            raise ValueError("LOCAL_WRITE tools cannot advertise persistent approval")
        return self

    @classmethod
    def from_definition(
        cls,
        definition: ToolDefinition,
        *,
        enabled: bool,
        implementation_available: bool = True,
    ) -> ToolCapability:
        return cls(
            tool_name=definition.name,
            tool_version=definition.version,
            enabled=enabled,
            implementation_available=implementation_available,
            risk_level=definition.risk_level,
            default_approval_mode=definition.default_approval_mode,
            default_timeout_ms=definition.default_timeout_ms,
            maximum_timeout_ms=definition.maximum_timeout_ms,
            maximum_result_size_bytes=definition.maximum_result_size_bytes,
            maximum_result_items=definition.maximum_result_items,
            maximum_result_characters=definition.maximum_result_characters,
            maximum_result_lines=definition.maximum_result_lines,
            cancellation_supported=definition.cancellation_supported,
            idempotency_behavior=definition.idempotency_behavior,
            persistent_approval_supported=definition.persistent_approval_supported,
            client_implementation_id=definition.client_implementation_id,
        )


class ClientCapabilities(ProtocolToolMessage):
    type: Literal["client.capabilities"] = "client.capabilities"
    client_id: UUID
    session_id: UUID
    platform: ToolPlatform
    app_version: str = Field(min_length=1, max_length=64, pattern=_APP_VERSION_PATTERN.pattern)
    tools: list[ToolCapability] = Field(max_length=64)
    advertised_at: datetime

    _aware_advertised_at = field_validator("advertised_at")(_validate_aware_datetime)

    @model_validator(mode="after")
    def reject_duplicate_tools(self) -> Self:
        names = [capability.tool_name for capability in self.tools]
        if len(names) != len(set(names)):
            raise ValueError("client capabilities contain duplicate tool names")
        return self


_ARGUMENT_SCHEMAS: Mapping[str, type[BaseModel]] = MappingProxyType(
    {
        "get_system_status": GetSystemStatusArguments,
        "get_active_window": GetActiveWindowArguments,
        "open_application": OpenApplicationArguments,
        "open_folder": OpenFolderArguments,
        "search_files": SearchFilesArguments,
        "read_text_file": ReadTextFileArguments,
        "create_note": CreateNoteArguments,
        "set_reminder": SetReminderArguments,
    }
)

_RESULT_SCHEMAS: Mapping[str, type[BaseModel]] = MappingProxyType(
    {
        "get_system_status": GetSystemStatusResult,
        "get_active_window": GetActiveWindowResult,
        "open_application": OpenApplicationResult,
        "open_folder": OpenFolderResult,
        "search_files": SearchFilesResult,
        "read_text_file": ReadTextFileResult,
        "create_note": CreateNoteResult,
        "set_reminder": SetReminderResult,
    }
)

_EXPECTED_RISK: Mapping[str, RiskLevel] = MappingProxyType(
    {
        "get_system_status": RiskLevel.SAFE_STATUS,
        "get_active_window": RiskLevel.LOCAL_READ,
        "open_application": RiskLevel.INTERACTIVE,
        "open_folder": RiskLevel.INTERACTIVE,
        "search_files": RiskLevel.LOCAL_READ,
        "read_text_file": RiskLevel.LOCAL_READ,
        "create_note": RiskLevel.LOCAL_WRITE,
        "set_reminder": RiskLevel.LOCAL_WRITE,
    }
)

_EXPECTED_APPROVAL_MODE: Mapping[str, ApprovalMode] = MappingProxyType(
    {
        "get_system_status": ApprovalMode.NOT_REQUIRED,
        "get_active_window": ApprovalMode.ALLOW_ONCE,
        "open_application": ApprovalMode.ALLOW_ONCE,
        "open_folder": ApprovalMode.ALLOW_ONCE,
        "search_files": ApprovalMode.ALLOW_ONCE,
        "read_text_file": ApprovalMode.ALLOW_ONCE,
        "create_note": ApprovalMode.ALLOW_ONCE,
        "set_reminder": ApprovalMode.ALLOW_ONCE,
    }
)

_EXPECTED_IDEMPOTENCY: Mapping[str, IdempotencyBehavior] = MappingProxyType(
    {
        "get_system_status": IdempotencyBehavior.READ_ONLY,
        "get_active_window": IdempotencyBehavior.READ_ONLY,
        "open_application": IdempotencyBehavior.AT_MOST_ONCE,
        "open_folder": IdempotencyBehavior.AT_MOST_ONCE,
        "search_files": IdempotencyBehavior.READ_ONLY,
        "read_text_file": IdempotencyBehavior.READ_ONLY,
        "create_note": IdempotencyBehavior.AT_MOST_ONCE,
        "set_reminder": IdempotencyBehavior.AT_MOST_ONCE,
    }
)

_PERSISTENT_APPROVAL_TOOLS = frozenset({"open_application", "open_folder"})


class ToolRegistry:
    """Duplicate-safe registry that accepts only the Phase 3 allowlist."""

    def __init__(self, definitions: Iterable[ToolDefinition] = ()) -> None:
        self._definitions: dict[str, ToolDefinition] = {}
        self._frozen = False
        for definition in definitions:
            self.register(definition)

    def __len__(self) -> int:
        return len(self._definitions)

    def __iter__(self) -> Iterator[ToolDefinition]:
        return iter(self._definitions.values())

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self._definitions)

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(self._definitions.values())

    def freeze(self) -> Self:
        self._frozen = True
        return self

    def register(self, definition: ToolDefinition) -> None:
        if self._frozen:
            raise ToolProtocolError("the shared tool registry is frozen")
        if definition.name in self._definitions:
            raise DuplicateToolRegistrationError(f"duplicate tool registry name: {definition.name}")
        if definition.name not in _ARGUMENT_SCHEMAS:
            raise UnknownToolError(f"tool is not in the Phase 3 allowlist: {definition.name}")
        if definition.version != TOOL_VERSION:
            raise UnsupportedToolVersionError(
                f"unsupported {definition.name} version: {definition.version}"
            )
        if definition.argument_schema is not _ARGUMENT_SCHEMAS[definition.name]:
            raise ToolProtocolError(f"unexpected argument schema for {definition.name}")
        if definition.result_schema is not _RESULT_SCHEMAS[definition.name]:
            raise ToolProtocolError(f"unexpected result schema for {definition.name}")
        if definition.risk_level is not _EXPECTED_RISK[definition.name]:
            raise ToolProtocolError(f"incorrect risk classification for {definition.name}")
        if definition.default_approval_mode is not _EXPECTED_APPROVAL_MODE[definition.name]:
            raise ToolProtocolError(f"incorrect approval policy for {definition.name}")
        if definition.idempotency_behavior is not _EXPECTED_IDEMPOTENCY[definition.name]:
            raise ToolProtocolError(f"incorrect idempotency policy for {definition.name}")
        expected_persistent = definition.name in _PERSISTENT_APPROVAL_TOOLS
        if definition.persistent_approval_supported is not expected_persistent:
            raise ToolProtocolError(f"incorrect persistent-approval policy for {definition.name}")
        expected_implementation = f"nivelle_agent.{definition.name}"
        if definition.client_implementation_id != expected_implementation:
            raise ToolProtocolError(
                f"unexpected client implementation identifier for {definition.name}"
            )
        if definition.supported_platforms != frozenset({ToolPlatform.WINDOWS}):
            raise ToolProtocolError("Phase 3 tool implementations are Windows-only")
        self._definitions[definition.name] = definition

    def require(self, name: str, version: str | None = None) -> ToolDefinition:
        try:
            definition = self._definitions[name]
        except KeyError as exc:
            raise UnknownToolError(f"unknown tool: {name}") from exc
        if version is not None and version != definition.version:
            raise UnsupportedToolVersionError(
                f"unsupported {name} version {version!r}; expected {definition.version!r}"
            )
        return definition

    def get(self, name: str, version: str | None = None) -> ToolDefinition:
        """Strict alias for :meth:`require`; unknown tools never return ``None``."""

        return self.require(name, version)

    def validate_request(self, request: ToolRequest) -> BaseModel:
        definition = self.require(request.tool_name, request.tool_version)
        if request.risk_level is not definition.risk_level:
            raise ToolProtocolError("request risk level does not match the registry")
        if request.timeout_ms > definition.maximum_timeout_ms:
            raise ToolProtocolError(
                f"timeout exceeds {request.tool_name} maximum of {definition.maximum_timeout_ms} ms"
            )
        return definition.argument_schema.model_validate(request.arguments)

    def validate_result(self, result: ToolResult) -> BaseModel | None:
        definition = self.require(result.tool_name, result.tool_version)
        if result.result is None:
            return None
        if result.duration_ms is not None and result.duration_ms > definition.maximum_timeout_ms:
            raise ToolProtocolError(f"result duration exceeds {result.tool_name} maximum timeout")
        encoded_size = len(
            json.dumps(
                result.result,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        if encoded_size > definition.maximum_result_size_bytes:
            raise ToolProtocolError(
                f"result exceeds {result.tool_name} maximum of "
                f"{definition.maximum_result_size_bytes} bytes"
            )
        validated = definition.result_schema.model_validate(result.result)
        items = getattr(validated, "items", None)
        if (
            definition.maximum_result_items is not None
            and isinstance(items, list)
            and len(items) > definition.maximum_result_items
        ):
            raise ToolProtocolError("typed result exceeds the registry item limit")
        content = getattr(validated, "content", None)
        if (
            definition.maximum_result_characters is not None
            and isinstance(content, str)
            and len(content) > definition.maximum_result_characters
        ):
            raise ToolProtocolError("typed result exceeds the registry character limit")
        returned_lines = getattr(validated, "returned_lines", None)
        if (
            definition.maximum_result_lines is not None
            and isinstance(returned_lines, int)
            and returned_lines > definition.maximum_result_lines
        ):
            raise ToolProtocolError("typed result exceeds the registry line limit")
        return validated

    def validate_capability(self, capability: ToolCapability) -> ToolDefinition:
        definition = self.require(capability.tool_name, capability.tool_version)
        if capability.risk_level is not definition.risk_level:
            raise ToolProtocolError("capability risk level does not match the registry")
        allowed_approval_modes = {definition.default_approval_mode}
        if definition.default_approval_mode is ApprovalMode.NOT_REQUIRED:
            # The authoritative client may choose a stricter per-call prompt.
            allowed_approval_modes.add(ApprovalMode.ALLOW_ONCE)
        if capability.default_approval_mode not in allowed_approval_modes:
            raise ToolProtocolError("capability approval mode weakens the registry default")
        if capability.maximum_timeout_ms > definition.maximum_timeout_ms:
            raise ToolProtocolError("capability timeout exceeds the registry maximum")
        if capability.default_timeout_ms > capability.maximum_timeout_ms:
            raise ToolProtocolError("capability default timeout exceeds its maximum")
        if capability.maximum_result_size_bytes > definition.maximum_result_size_bytes:
            raise ToolProtocolError("capability result limit exceeds the registry maximum")
        optional_limits = (
            (capability.maximum_result_items, definition.maximum_result_items),
            (capability.maximum_result_characters, definition.maximum_result_characters),
            (capability.maximum_result_lines, definition.maximum_result_lines),
        )
        for advertised_limit, registry_limit in optional_limits:
            if registry_limit is not None and (
                advertised_limit is None or advertised_limit > registry_limit
            ):
                raise ToolProtocolError("capability result limit exceeds the registry maximum")
        if capability.cancellation_supported and not definition.cancellation_supported:
            raise ToolProtocolError("capability advertises unsupported cancellation")
        if (
            capability.persistent_approval_supported
            and not definition.persistent_approval_supported
        ):
            raise ToolProtocolError("capability advertises forbidden persistent approval")
        if capability.client_implementation_id != definition.client_implementation_id:
            raise ToolProtocolError("capability implementation identifier does not match")
        return definition

    def validate_capabilities(self, advertisement: ClientCapabilities) -> None:
        if advertisement.platform is not ToolPlatform.WINDOWS:
            raise ToolProtocolError("Phase 3 capabilities are Windows-only")
        for capability in advertisement.tools:
            self.validate_capability(capability)

    def model_tool_definitions(
        self, capabilities: Iterable[ToolCapability]
    ) -> list[dict[str, Any]]:
        """Expose only enabled implementations advertised by the active client."""

        definitions: list[dict[str, Any]] = []
        seen: set[str] = set()
        for capability in capabilities:
            if capability.tool_name in seen:
                raise ToolProtocolError("duplicate capability supplied for model advertisement")
            seen.add(capability.tool_name)
            definition = self.validate_capability(capability)
            if capability.enabled and capability.implementation_available:
                definitions.append(definition.model_tool_definition())
        return definitions


def _definition(
    *,
    name: str,
    display_name: str,
    description: str,
    risk_level: RiskLevel,
    default_approval_mode: ApprovalMode,
    default_timeout_ms: int,
    maximum_timeout_ms: int,
    maximum_result_size_bytes: int,
    cancellation_supported: bool,
    idempotency_behavior: IdempotencyBehavior,
    persistent_approval_supported: bool,
    maximum_result_items: int | None = None,
    maximum_result_characters: int | None = None,
    maximum_result_lines: int | None = None,
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        version=TOOL_VERSION,
        display_name=display_name,
        description=description,
        argument_schema=_ARGUMENT_SCHEMAS[name],
        result_schema=_RESULT_SCHEMAS[name],
        risk_level=risk_level,
        default_approval_mode=default_approval_mode,
        supported_platforms=frozenset({ToolPlatform.WINDOWS}),
        default_timeout_ms=default_timeout_ms,
        maximum_timeout_ms=maximum_timeout_ms,
        maximum_result_size_bytes=maximum_result_size_bytes,
        maximum_result_items=maximum_result_items,
        maximum_result_characters=maximum_result_characters,
        maximum_result_lines=maximum_result_lines,
        cancellation_supported=cancellation_supported,
        idempotency_behavior=idempotency_behavior,
        persistent_approval_supported=persistent_approval_supported,
        client_implementation_id=f"nivelle_agent.{name}",
    )


TOOL_DEFINITIONS: tuple[ToolDefinition, ...] = (
    _definition(
        name="get_system_status",
        display_name="System status",
        description="Return bounded, non-secret operating-system and hardware status metadata.",
        risk_level=RiskLevel.SAFE_STATUS,
        default_approval_mode=ApprovalMode.NOT_REQUIRED,
        default_timeout_ms=5_000,
        maximum_timeout_ms=10_000,
        maximum_result_size_bytes=65_536,
        cancellation_supported=False,
        idempotency_behavior=IdempotencyBehavior.READ_ONLY,
        persistent_approval_supported=False,
        maximum_result_items=64,
    ),
    _definition(
        name="get_active_window",
        display_name="Active window",
        description="Return foreground-window title and process metadata without capturing content.",
        risk_level=RiskLevel.LOCAL_READ,
        default_approval_mode=ApprovalMode.ALLOW_ONCE,
        default_timeout_ms=3_000,
        maximum_timeout_ms=5_000,
        maximum_result_size_bytes=32_768,
        cancellation_supported=False,
        idempotency_behavior=IdempotencyBehavior.READ_ONLY,
        persistent_approval_supported=False,
    ),
    _definition(
        name="open_application",
        display_name="Open application",
        description="Launch one locally registered application ID without arguments or shell access.",
        risk_level=RiskLevel.INTERACTIVE,
        default_approval_mode=ApprovalMode.ALLOW_ONCE,
        default_timeout_ms=10_000,
        maximum_timeout_ms=15_000,
        maximum_result_size_bytes=16_384,
        cancellation_supported=False,
        idempotency_behavior=IdempotencyBehavior.AT_MOST_ONCE,
        persistent_approval_supported=True,
    ),
    _definition(
        name="open_folder",
        display_name="Open folder",
        description="Open one canonically validated folder within an approved local root.",
        risk_level=RiskLevel.INTERACTIVE,
        default_approval_mode=ApprovalMode.ALLOW_ONCE,
        default_timeout_ms=10_000,
        maximum_timeout_ms=15_000,
        maximum_result_size_bytes=16_384,
        cancellation_supported=False,
        idempotency_behavior=IdempotencyBehavior.AT_MOST_ONCE,
        persistent_approval_supported=True,
    ),
    _definition(
        name="search_files",
        display_name="Search files",
        description="Search names only inside one approved root with bounded depth and results.",
        risk_level=RiskLevel.LOCAL_READ,
        default_approval_mode=ApprovalMode.ALLOW_ONCE,
        default_timeout_ms=10_000,
        maximum_timeout_ms=30_000,
        maximum_result_size_bytes=100_000,
        cancellation_supported=True,
        idempotency_behavior=IdempotencyBehavior.READ_ONLY,
        persistent_approval_supported=False,
        maximum_result_items=200,
    ),
    _definition(
        name="read_text_file",
        display_name="Read text file",
        description="Read a bounded range from one approved non-sensitive text file as untrusted data.",
        risk_level=RiskLevel.LOCAL_READ,
        default_approval_mode=ApprovalMode.ALLOW_ONCE,
        default_timeout_ms=10_000,
        maximum_timeout_ms=30_000,
        maximum_result_size_bytes=500_000,
        cancellation_supported=True,
        idempotency_behavior=IdempotencyBehavior.READ_ONLY,
        persistent_approval_supported=False,
        maximum_result_characters=100_000,
        maximum_result_lines=10_000,
    ),
    _definition(
        name="create_note",
        display_name="Create note",
        description="Create one non-overwriting UTF-8 txt or md file inside Nivelle Notes.",
        risk_level=RiskLevel.LOCAL_WRITE,
        default_approval_mode=ApprovalMode.ALLOW_ONCE,
        default_timeout_ms=10_000,
        maximum_timeout_ms=30_000,
        maximum_result_size_bytes=32_768,
        cancellation_supported=False,
        idempotency_behavior=IdempotencyBehavior.AT_MOST_ONCE,
        persistent_approval_supported=False,
    ),
    _definition(
        name="set_reminder",
        display_name="Set reminder",
        description="Create one Nivelle reminder through the existing schedule store.",
        risk_level=RiskLevel.LOCAL_WRITE,
        default_approval_mode=ApprovalMode.ALLOW_ONCE,
        default_timeout_ms=10_000,
        maximum_timeout_ms=30_000,
        maximum_result_size_bytes=32_768,
        cancellation_supported=False,
        idempotency_behavior=IdempotencyBehavior.AT_MOST_ONCE,
        persistent_approval_supported=False,
    ),
)

TOOL_REGISTRY = ToolRegistry(TOOL_DEFINITIONS).freeze()


class ToolDetail(StrictToolModel):
    """Compatibility summary for the former unavailable-tool chat payload."""

    tool_name: str
    status: Literal["unavailable"] = "unavailable"
    requested_at: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    result: Any = None
    error: str | None = None
    approval_state: Literal["not_applicable"] = "not_applicable"


__all__ = [
    "ApprovalMode",
    "BatteryStatus",
    "ClientCapabilities",
    "CreateNoteArguments",
    "CreateNoteResult",
    "DiskVolumeStatus",
    "DuplicateToolRegistrationError",
    "EmptyToolArguments",
    "GetActiveWindowArguments",
    "GetActiveWindowResult",
    "GetSystemStatusArguments",
    "GetSystemStatusResult",
    "HARD_MAX_RESULT_SIZE_BYTES",
    "HARD_MAX_TOOL_TIMEOUT_MS",
    "IdempotencyBehavior",
    "InvalidToolStateTransitionError",
    "LEGAL_TOOL_TRANSITIONS",
    "MIN_TOOL_TIMEOUT_MS",
    "OpenApplicationArguments",
    "OpenApplicationResult",
    "OpenFolderArguments",
    "OpenFolderResult",
    "ReadTextFileArguments",
    "ReadTextFileResult",
    "RiskLevel",
    "SafeNetworkSummary",
    "SearchFileItem",
    "SearchFilesArguments",
    "SearchFilesResult",
    "SetReminderArguments",
    "SetReminderResult",
    "TERMINAL_TOOL_STATUSES",
    "TOOL_DEFINITIONS",
    "TOOL_REGISTRY",
    "TOOL_VERSION",
    "ToolApprovalDecision",
    "ToolArgumentsModel",
    "ToolCapability",
    "ToolDefinition",
    "ToolDetail",
    "ToolErrorCode",
    "ToolEvent",
    "ToolEventType",
    "ToolPlatform",
    "ToolProgress",
    "ToolProtocolError",
    "ToolRegistry",
    "ToolRequest",
    "ToolResult",
    "ToolResultModel",
    "ToolStatus",
    "UnknownToolError",
    "UnsupportedToolVersionError",
    "is_valid_tool_state_transition",
    "validate_tool_event_transition",
    "validate_tool_state_transition",
]
