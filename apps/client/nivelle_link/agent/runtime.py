from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from nivelle_protocol.tools import (
    TOOL_REGISTRY,
    ClientCapabilities,
    ToolCapability,
    ToolPlatform,
    ToolProtocolError,
    ToolRequest,
    ToolResult,
    ToolStatus,
)
from nivelle_protocol.tools import (
    ApprovalMode as ProtocolApprovalMode,
)
from nivelle_protocol.version import PROTOCOL_VERSION
from pydantic import ValidationError

from .active_window import (
    ActiveWindowProvider,
    WindowsActiveWindowProvider,
    get_active_window,
)
from .application import default_application_launcher, open_application
from .approvals import ApprovalManager, exact_target_for
from .audit import AuditLog
from .errors import AgentError
from .folder import default_folder_launcher, open_folder
from .idempotency import IdempotencyCache
from .models import (
    AgentPolicy,
    AgentToolRequest,
    ApprovalGrant,
    ApprovalMode,
    ApprovalSource,
)
from .note import NoteWriter
from .policy import PolicyStore
from .protocol_adapter import make_tool_result, normalize_result_payload, to_agent_request
from .reminder import ReminderStore
from .search import CancellationSignal, search_files
from .system_status import SystemStatusProvider
from .text_file import read_text_file

IMPLEMENTED_TOOLS = frozenset(
    {
        "get_system_status",
        "get_active_window",
        "open_application",
        "open_folder",
        "search_files",
        "read_text_file",
        "create_note",
        "set_reminder",
    }
)


class _CombinedCancellation:
    def __init__(self, *signals: CancellationSignal | None) -> None:
        self.signals = signals

    def is_set(self) -> bool:
        return any(signal is not None and signal.is_set() for signal in self.signals)


class AgentRuntime:
    """Authoritative local execution boundary for the eight Phase 3 tools."""

    def __init__(
        self,
        *,
        data_directory: Path,
        client_id: str,
        session_id: str,
        client_display_name: str,
        link_version: str,
        application_launcher: Callable[[Path], int | None] = default_application_launcher,
        folder_launcher: Callable[[Path], None] = default_folder_launcher,
        active_window_provider: ActiveWindowProvider | None = None,
        system_status_provider: SystemStatusProvider | None = None,
        reminder_now: Any | None = None,
    ) -> None:
        self.data_directory = data_directory
        self.client_id = client_id
        self.session_id = session_id
        self.client_display_name = client_display_name
        self.link_version = link_version
        self.policy_store = PolicyStore(data_directory / "agent-policy.json")
        self.approvals = ApprovalManager(data_directory / "agent-approvals.json")
        self.idempotency = IdempotencyCache(data_directory / "agent-idempotency.json")
        self.audit = AuditLog(data_directory / "agent-audit.json")
        self.notes = NoteWriter(data_directory / "Nivelle Notes", self.idempotency)
        self.reminders = ReminderStore(data_directory / "agent-reminders.db", now=reminder_now)
        self.application_launcher = application_launcher
        self.folder_launcher = folder_launcher
        self.active_window_provider = active_window_provider or WindowsActiveWindowProvider()
        self.system_status_provider = system_status_provider or SystemStatusProvider()
        self.started_monotonic = time.monotonic()
        self._shutdown = threading.Event()

    def load_policy(self) -> AgentPolicy:
        return self.policy_store.load()

    def capabilities(
        self,
        *,
        app_version: str | None = None,
        protocol_version: str = PROTOCOL_VERSION,
    ) -> ClientCapabilities:
        policy = self.load_policy()
        capabilities: list[ToolCapability] = []
        for definition in TOOL_REGISTRY:
            capability = ToolCapability.from_definition(
                definition,
                enabled=policy.agent_enabled and definition.name in policy.enabled_tools,
                implementation_available=definition.name in IMPLEMENTED_TOOLS,
            )
            configured_approval = policy.approval_defaults.get(
                definition.name,
                ApprovalMode.ALLOW_ONCE,
            )
            configured_timeout = policy.tool_timeouts_ms.get(definition.name)
            capability_payload = capability.model_dump(mode="python")
            capability_payload["default_approval_mode"] = ProtocolApprovalMode(
                configured_approval.value
            )
            if configured_timeout is not None:
                capability_payload["default_timeout_ms"] = min(
                    configured_timeout,
                    definition.maximum_timeout_ms,
                )
            capability_payload["persistent_approval_supported"] = (
                definition.persistent_approval_supported
                and definition.name in policy.persistent_approval_tools
            )
            capabilities.append(ToolCapability.model_validate(capability_payload))
        return ClientCapabilities(
            client_id=UUID(self.client_id),
            session_id=UUID(self.session_id),
            platform=ToolPlatform.WINDOWS,
            app_version=app_version or self.link_version,
            protocol_version=protocol_version,
            tools=capabilities,
            advertised_at=datetime.now(UTC),
        )

    def record_local_ui_approval(
        self, request: ToolRequest, mode: ProtocolApprovalMode
    ) -> ApprovalGrant:
        """Persist a decision originating from an explicit local UI action."""

        local_request = to_agent_request(request)
        return self.approvals.grant(
            local_request,
            ApprovalMode(mode.value),
            source=ApprovalSource.USER_UI,
            policy=self.load_policy(),
        )

    def revoke_approval(self, approval_id: str) -> bool:
        return self.approvals.revoke(approval_id)

    @staticmethod
    def _audit_target(request: AgentToolRequest) -> str:
        target = exact_target_for(request)
        digest = hashlib.sha256(target.encode("utf-8")).hexdigest()[:16]
        return f"{request.tool_name}:{digest}"

    def _execute_handler(
        self,
        request: AgentToolRequest,
        *,
        policy: AgentPolicy,
        cancellation: CancellationSignal | None,
        timeout_seconds: float,
    ) -> tuple[dict[str, Any], bool]:
        if request.tool_name == "get_system_status":
            return (
                self.system_status_provider.snapshot(
                    client_display_name=self.client_display_name,
                    app_version=self.link_version,
                    started_monotonic=self.started_monotonic,
                ),
                False,
            )
        if request.tool_name == "get_active_window":
            return get_active_window(self.active_window_provider), False
        if request.tool_name == "open_application":
            return open_application(
                request,
                policy=policy,
                idempotency=self.idempotency,
                launcher=self.application_launcher,
            )
        if request.tool_name == "open_folder":
            return self.idempotency.execute_once(
                idempotency_key=request.idempotency_key,
                tool_name=request.tool_name,
                arguments=request.arguments,
                operation=lambda: open_folder(
                    request.arguments, policy=policy, launcher=self.folder_launcher
                ),
            )
        if request.tool_name == "search_files":
            combined = _CombinedCancellation(cancellation, self._shutdown)
            return (
                search_files(
                    request.arguments,
                    policy=policy,
                    cancellation=combined,
                    timeout_seconds=timeout_seconds,
                ),
                False,
            )
        if request.tool_name == "read_text_file":
            combined = _CombinedCancellation(cancellation, self._shutdown)
            return (
                read_text_file(
                    request.arguments,
                    policy=policy,
                    cancellation=combined,
                ),
                False,
            )
        if request.tool_name == "create_note":
            return self.notes.create(request, request.arguments)
        if request.tool_name == "set_reminder":
            return self.reminders.create(request, request.arguments)
        raise AgentError("unsupported_tool", "This client does not implement the requested tool.")

    def execute(
        self,
        request: ToolRequest,
        *,
        approval_id: str | None = None,
        cancellation: CancellationSignal | None = None,
    ) -> ToolResult:
        started_at = datetime.now(UTC)
        local_request: AgentToolRequest | None = None
        try:
            definition = TOOL_REGISTRY.require(request.tool_name, request.tool_version)
            local_request = to_agent_request(request)
            if str(request.target_client_id) != self.client_id:
                raise AgentError("permission_denied", "The tool request targets another client.")
            if str(request.target_session_id) != self.session_id:
                raise AgentError("client_disconnected", "The target client session is no longer active.")
            if self._shutdown.is_set():
                raise AgentError("client_disconnected", "Nivelle Agent is shutting down.")
            policy = self.load_policy()
            if not policy.agent_enabled:
                raise AgentError("tool_disabled", "Nivelle Agent is disabled locally.")
            if request.tool_name not in policy.enabled_tools:
                raise AgentError("tool_disabled", "This tool is disabled by local policy.")
            approval_mode = policy.approval_defaults.get(
                request.tool_name, ApprovalMode.ALLOW_ONCE
            )
            if approval_mode is not ApprovalMode.NOT_REQUIRED:
                self.approvals.authorize(
                    local_request, policy=policy, approval_id=approval_id
                )
            policy_timeout_ms = policy.tool_timeouts_ms.get(
                request.tool_name, definition.default_timeout_ms
            )
            timeout_ms = min(request.timeout_ms, definition.maximum_timeout_ms, policy_timeout_ms)
            raw_result, replayed = self._execute_handler(
                local_request,
                policy=policy,
                cancellation=cancellation,
                timeout_seconds=timeout_ms / 1_000,
            )
            normalized = normalize_result_payload(
                request.tool_name, raw_result, replayed=replayed
            )
            completed_at = datetime.now(UTC)
            safe_summary = (
                "The local action was already completed; its saved result was reused."
                if replayed
                else "The requested local tool completed successfully."
            )
            result = make_tool_result(
                request,
                status=ToolStatus.COMPLETED,
                started_at=started_at,
                completed_at=completed_at,
                result=normalized,
                safe_summary=safe_summary,
            )
            self.audit.append(
                local_request,
                status="completed",
                target_summary=self._audit_target(local_request),
                started_at=started_at,
                completed_at=completed_at,
                result_summary=safe_summary,
            )
            return result
        except AgentError as exc:
            error = exc
        except (ValidationError, ToolProtocolError, ValueError) as exc:
            error = AgentError("validation_failed", "The local tool request is invalid.")
            error.__cause__ = exc
        except Exception as exc:
            error = AgentError("execution_failed", "The local tool could not be completed.")
            error.__cause__ = exc

        completed_at = datetime.now(UTC)
        if error.code == "cancelled":
            status = ToolStatus.CANCELLED
        elif error.code == "timed_out":
            status = ToolStatus.TIMED_OUT
        elif error.code in {"permission_denied", "approval_denied", "approval_expired"}:
            status = ToolStatus.DENIED
        elif error.code in {"client_disconnected", "client_offline"}:
            status = ToolStatus.CLIENT_DISCONNECTED
        else:
            status = ToolStatus.FAILED
        result = make_tool_result(
            request,
            status=status,
            started_at=started_at,
            completed_at=completed_at,
            result=None,
            safe_summary=error.safe_message,
            error_code=error.code,
            error_message=error.safe_message,
            retryable=error.retryable,
        )
        if local_request is not None:
            self.audit.append(
                local_request,
                status=status.value,
                target_summary=self._audit_target(local_request),
                started_at=started_at,
                completed_at=completed_at,
                result_summary=error.safe_message,
                error_code=error.code,
            )
        return result

    def handle_tool_request(
        self,
        request: ToolRequest,
        *,
        approval_id: str | None = None,
        cancellation: CancellationSignal | None = None,
    ) -> ToolResult:
        return self.execute(
            request, approval_id=approval_id, cancellation=cancellation
        )

    def shutdown(self) -> None:
        self._shutdown.set()
