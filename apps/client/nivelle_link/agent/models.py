from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PureWindowsPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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
    ALWAYS_EXACT_TARGET = "allow_always_exact"


class ApprovalSource(StrEnum):
    USER_UI = "user_ui"
    CHAT = "chat"
    PERSONA = "persona"
    MEMORY = "memory"
    TOOL_RESULT = "tool_result"
    SERVER = "server"


_FORBIDDEN_APPLICATION_EXECUTABLES = frozenset(
    {
        "bash.exe",
        "cmd.exe",
        "cscript.exe",
        "mshta.exe",
        "msiexec.exe",
        "node.exe",
        "perl.exe",
        "php.exe",
        "powershell.exe",
        "pwsh.exe",
        "py.exe",
        "reg.exe",
        "regsvr32.exe",
        "ruby.exe",
        "rundll32.exe",
        "schtasks.exe",
        "sc.exe",
        "sh.exe",
        "shutdown.exe",
        "taskkill.exe",
        "wscript.exe",
        "wsl.exe",
        "wt.exe",
    }
)


def is_forbidden_application_executable(value: Path | str) -> bool:
    """Reject shells, interpreters, script hosts, and installer entry points."""

    name = PureWindowsPath(str(value)).name.casefold()
    stem = name.removesuffix(".exe")
    return (
        name in _FORBIDDEN_APPLICATION_EXECUTABLES
        or stem.startswith(("python", "pypy", "setup", "unins"))
        or stem in {"install", "installer"}
        or stem.endswith(("-installer", "_installer"))
    )


class RegisteredApplication(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=100)
    executable_path: Path
    enabled: bool = True

    @field_validator("executable_path")
    @classmethod
    def reject_dangerous_executables(cls, value: Path) -> Path:
        if is_forbidden_application_executable(value):
            raise ValueError("Shells, interpreters, script hosts, and installers are forbidden")
        return value


class FilesystemRoot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=100)
    path: Path
    allow_search: bool = False
    allow_read: bool = False
    allow_open_folder: bool = False


class AgentLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_read_file_bytes: int = Field(default=1_048_576, ge=1, le=5_242_880)
    hard_read_file_bytes: int = Field(default=5_242_880, ge=1, le=5_242_880)
    default_return_characters: int = Field(default=32_000, ge=1, le=100_000)
    hard_return_characters: int = Field(default=100_000, ge=1, le=100_000)
    default_return_lines: int = Field(default=500, ge=1, le=5_000)
    hard_return_lines: int = Field(default=5_000, ge=1, le=5_000)
    default_max_results: int = Field(default=50, ge=1, le=200)
    hard_max_results: int = Field(default=200, ge=1, le=200)
    default_max_depth: int = Field(default=8, ge=0, le=8)
    audit_retention: int = Field(default=1_000, ge=1, le=10_000)
    idempotency_retention: int = Field(default=1_000, ge=1, le=10_000)


class AgentPolicy(BaseModel):
    """Local-only policy. Unknown keys fail closed so secrets are not accepted."""

    model_config = ConfigDict(extra="forbid")

    policy_version: str = Field(default="1", min_length=1, max_length=64)
    agent_enabled: bool = False
    enabled_tools: set[str] = Field(default_factory=lambda: {"get_system_status"})
    applications: dict[str, RegisteredApplication] = Field(default_factory=dict)
    filesystem_roots: dict[str, FilesystemRoot] = Field(default_factory=dict)
    denied_paths: list[Path] = Field(default_factory=list)
    allow_hidden_files: bool = False
    allow_system_files: bool = False
    allow_network_paths: bool = False
    allow_reparse_points: bool = False
    allow_direct_paths: bool = False
    session_approval_ttl_seconds: int = Field(default=3_600, ge=30, le=86_400)
    persistent_approval_tools: set[str] = Field(
        default_factory=lambda: {"open_application", "open_folder"}
    )
    approval_defaults: dict[str, ApprovalMode] = Field(
        default_factory=lambda: {
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
    tool_timeouts_ms: dict[str, int] = Field(
        default_factory=lambda: {
            "get_system_status": 5_000,
            "get_active_window": 5_000,
            "open_application": 10_000,
            "open_folder": 10_000,
            "search_files": 10_000,
            "read_text_file": 10_000,
            "create_note": 10_000,
            "set_reminder": 10_000,
        }
    )
    limits: AgentLimits = Field(default_factory=AgentLimits)

    @field_validator("enabled_tools")
    @classmethod
    def validate_enabled_tools(cls, value: set[str]) -> set[str]:
        return {item.strip() for item in value if item.strip()}

    @field_validator("tool_timeouts_ms")
    @classmethod
    def validate_tool_timeouts(cls, value: dict[str, int]) -> dict[str, int]:
        if any(timeout < 100 or timeout > 300_000 for timeout in value.values()):
            raise ValueError("Tool timeouts must be between 100 and 300000 milliseconds")
        return value

    @model_validator(mode="after")
    def validate_approval_defaults(self) -> AgentPolicy:
        for tool_name, mode in self.approval_defaults.items():
            if tool_name != "get_system_status" and mode is ApprovalMode.NOT_REQUIRED:
                raise ValueError("Only get_system_status may omit per-call approval")
            if tool_name in {"create_note", "set_reminder"} and mode is not ApprovalMode.ALLOW_ONCE:
                raise ValueError("LOCAL_WRITE tools must require one-time approval")
        return self


class AgentToolRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    tool_call_id: str = Field(min_length=1, max_length=128)
    request_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    conversation_id: str = Field(min_length=1, max_length=128)
    user_message_id: str | None = None
    target_client_id: str = Field(min_length=1, max_length=128)
    target_session_id: str = Field(min_length=1, max_length=128)
    tool_name: str = Field(min_length=1, max_length=100)
    tool_version: str = Field(default="1.0", min_length=1, max_length=32)
    arguments: dict[str, Any] = Field(default_factory=dict)
    risk_level: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    timeout_ms: int = Field(default=10_000, ge=1, le=120_000)
    user_intent_summary: str = Field(default="", max_length=1_000)


class AgentExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_call_id: str
    request_id: str
    target_client_id: str
    target_session_id: str
    tool_name: str
    status: Literal["completed", "failed", "cancelled", "timed_out"]
    started_at: datetime
    completed_at: datetime
    duration_ms: int = Field(ge=0)
    result: dict[str, Any] | None = None
    safe_summary: str
    truncated: bool = False
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = False
    replayed: bool = False


class ApprovalGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approval_id: str
    tool_call_id: str
    request_id: str
    idempotency_key_hash: str
    policy_fingerprint: str
    client_id: str
    session_id: str | None
    tool_name: str
    tool_version: str
    exact_target: str
    argument_scope_hash: str
    policy_version: str
    mode: ApprovalMode
    created_at: datetime
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None
    remaining_uses: int | None = None


class AuditRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    audit_id: str
    tool_call_id: str
    request_id: str
    idempotency_key_hash: str
    client_id: str
    session_id: str
    tool_name: str
    tool_version: str
    risk_level: str
    status: str
    target_summary: str
    arguments_summary: dict[str, Any]
    started_at: datetime
    completed_at: datetime
    duration_ms: int = Field(ge=0)
    result_summary: str
    error_code: str | None = None
