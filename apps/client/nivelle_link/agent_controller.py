from __future__ import annotations

import asyncio
import json
import threading
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from nivelle_protocol.tools import (
    TOOL_REGISTRY,
    ClientCapabilities,
    ToolApprovalDecision,
    ToolErrorCode,
    ToolEvent,
    ToolEventType,
    ToolRequest,
    ToolResult,
    ToolStatus,
)
from nivelle_protocol.tools import (
    ApprovalMode as ProtocolApprovalMode,
)
from nivelle_protocol.tools import (
    RiskLevel as ProtocolRiskLevel,
)
from pydantic import ValidationError

from .agent import (
    AgentError,
    AgentPolicy,
    AgentRuntime,
    ApprovalMode,
    WindowsPathValidator,
    argument_scope_hash,
    exact_target_for,
)
from .agent.models import AgentToolRequest, ApprovalGrant
from .agent.protocol_adapter import to_agent_request

EventSender = Callable[[dict[str, Any]], Awaitable[None]]
ApprovalPresenter = Callable[[dict[str, Any]], object]
StatusPresenter = Callable[[str, str, str | None], object]


class AgentController:
    """Bridge the Agent wire protocol to the authoritative local runtime."""

    def __init__(
        self,
        *,
        data_directory: Path,
        client_id: str,
        session_id: str,
        client_display_name: str,
        link_version: str,
        send_event: EventSender,
        show_approval: ApprovalPresenter,
        update_status: StatusPresenter,
        approval_timeout_seconds: int = 120,
        max_parallel_tasks: int = 2,
        runtime: AgentRuntime | None = None,
    ) -> None:
        self.runtime = runtime or AgentRuntime(
            data_directory=data_directory,
            client_id=client_id,
            session_id=session_id,
            client_display_name=client_display_name,
            link_version=link_version,
        )
        if (
            self.runtime.client_id != client_id
            or self.runtime.session_id != session_id
        ):
            raise ValueError("The Agent runtime identity does not match this controller session.")
        self.client_id = client_id
        self.session_id = session_id
        self.send_event = send_event
        self.show_approval = show_approval
        self.update_status = update_status
        self.approval_timeout_seconds = max(10, min(approval_timeout_seconds, 900))
        self._parallel = asyncio.Semaphore(max(1, max_parallel_tasks))
        self._pending: dict[str, ToolRequest] = {}
        self._fingerprints: OrderedDict[str, str] = OrderedDict()
        self._terminal_messages: OrderedDict[
            str, ToolEvent | ToolResult
        ] = OrderedDict()
        self._cancellations: dict[str, threading.Event] = {}
        self._active_requests: dict[str, ToolRequest] = {}
        self._closed = False

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def capabilities(self, *, app_version: str, protocol_version: str) -> ClientCapabilities:
        return self.runtime.capabilities(
            app_version=app_version,
            protocol_version=protocol_version,
        )

    async def advertise(self, *, app_version: str, protocol_version: str) -> None:
        await self._send(self.capabilities(app_version=app_version, protocol_version=protocol_version))

    async def handle_server_event(self, value: dict[str, Any]) -> None:
        if (
            self._closed
            or not isinstance(value, dict)
            or value.get("type") != "tool.request"
        ):
            return
        try:
            request = ToolRequest.model_validate(value)
            TOOL_REGISTRY.validate_request(request)
        except (ValidationError, ValueError):
            # A malformed request may not contain trustworthy correlation IDs.
            # It is rejected before any local policy lookup or side effect.
            return

        # Never answer a request for another Link identity. Its correlation IDs
        # are not valid on this authenticated channel, so even a rejection event
        # would be unsafe to send back.
        if (
            str(request.target_client_id) != self.client_id
            or str(request.target_session_id) != self.session_id
        ):
            return

        call_id = str(request.tool_call_id)
        fingerprint = self._fingerprint(request)
        previous_fingerprint = self._fingerprints.get(call_id)
        if previous_fingerprint is not None:
            if previous_fingerprint != fingerprint:
                return
            cached = self._terminal_messages.get(call_id)
            if cached is not None:
                await self._send(cached)
            return
        self._remember_fingerprint(call_id, fingerprint)

        validation_error = self._local_validation_error(request)
        if validation_error is not None:
            error_code, message = validation_error
            event = await self._send_event(
                request,
                ToolEventType.VALIDATION_FAILED,
                ToolStatus.VALIDATION_FAILED,
                error_code=error_code,
                error_message=message,
            )
            self._remember_terminal(event)
            self.update_status(call_id, "validation_failed", message)
            return

        definition = TOOL_REGISTRY.require(request.tool_name, request.tool_version)
        policy = self.runtime.load_policy()
        approval_mode = policy.approval_defaults.get(
            request.tool_name,
            ApprovalMode.ALLOW_ONCE,
        )
        if approval_mode is ApprovalMode.NOT_REQUIRED:
            await self._execute(request, approval_id=None)
            return

        # Core already used the advertised default approval mode to enter
        # AWAITING_APPROVAL. approval_required and queued are therefore local UI
        # states, not Link-originated wire events.
        self.update_status(call_id, "awaiting_approval", "로컬 승인 대기 중")
        local_request = to_agent_request(request)
        existing = self._matching_reusable_approval(local_request)
        if existing is not None:
            await self._send_approved(request, existing)
            await self._execute(request, approval_id=existing.approval_id)
            return

        try:
            approval_payload = self._approval_payload(
                request, definition.display_name
            )
        except AgentError as exc:
            try:
                error_code = ToolErrorCode(exc.code)
            except ValueError:
                error_code = ToolErrorCode.VALIDATION_FAILED
            event = await self._send_event(
                request,
                ToolEventType.VALIDATION_FAILED,
                ToolStatus.VALIDATION_FAILED,
                error_code=error_code,
                error_message=exc.safe_message,
            )
            self._remember_terminal(event)
            self.update_status(call_id, "validation_failed", exc.safe_message)
            return
        self._pending[call_id] = request
        self.show_approval(approval_payload)

    async def decide(self, tool_call_id: str, decision: str) -> None:
        if decision == "cancel":
            self.cancel(tool_call_id)
            return
        request = self._pending.pop(tool_call_id, None)
        if request is None or self._closed:
            return
        local_request = to_agent_request(request)
        policy = self.runtime.load_policy()
        if decision == "deny_expired":
            event = await self._send_event(
                request,
                ToolEventType.TIMED_OUT,
                ToolStatus.TIMED_OUT,
                error_code=ToolErrorCode.APPROVAL_EXPIRED,
                error_message="The local approval request expired.",
            )
            self._remember_terminal(event)
            self.update_status(tool_call_id, "timed_out", "승인 시간이 만료되었습니다.")
            return
        if decision == "deny":
            approval = self._denial_decision(local_request, policy.policy_version)
            event = await self._send_event(
                request,
                ToolEventType.DENIED,
                ToolStatus.DENIED,
                approval=approval,
                error_code=ToolErrorCode.APPROVAL_DENIED,
                error_message="The user denied this local tool request.",
            )
            self._remember_terminal(event)
            self.update_status(tool_call_id, "denied", "사용자가 거부했습니다.")
            return

        mode_by_decision = {
            "allow_once": ProtocolApprovalMode.ALLOW_ONCE,
            "allow_session": ProtocolApprovalMode.ALLOW_SESSION,
            "allow_always_exact": ProtocolApprovalMode.ALLOW_ALWAYS_EXACT,
        }
        mode = mode_by_decision.get(decision)
        if mode is None:
            await self._deny_invalid_decision(request, local_request, policy.policy_version)
            return
        try:
            grant = await asyncio.to_thread(
                self.runtime.record_local_ui_approval,
                request,
                mode,
            )
        except Exception:
            await self._deny_invalid_decision(request, local_request, policy.policy_version)
            return
        if self._closed:
            return
        await self._send_approved(request, grant)
        await self._execute(request, approval_id=grant.approval_id)

    async def _deny_invalid_decision(
        self,
        request: ToolRequest,
        local_request: AgentToolRequest,
        policy_version: str,
    ) -> None:
        approval = self._denial_decision(local_request, policy_version)
        event = await self._send_event(
            request,
            ToolEventType.DENIED,
            ToolStatus.DENIED,
            approval=approval,
            error_code=ToolErrorCode.PERMISSION_DENIED,
            error_message="The requested approval mode is not permitted by local policy.",
        )
        self._remember_terminal(event)
        self.update_status(str(request.tool_call_id), "denied", "로컬 정책이 허용하지 않습니다.")

    async def _send_approved(self, request: ToolRequest, grant: ApprovalGrant) -> None:
        decision = ToolApprovalDecision(
            approval_id=UUID(grant.approval_id),
            mode=ProtocolApprovalMode(grant.mode.value),
            decided_at=grant.created_at,
            expires_at=grant.expires_at,
            exact_target=grant.exact_target,
            normalized_argument_scope=grant.argument_scope_hash,
            policy_version=grant.policy_version,
            reason="Approved through the local Nivelle Link UI.",
        )
        await self._send_event(
            request,
            ToolEventType.APPROVED,
            ToolStatus.APPROVED,
            approval=decision,
        )
        self.update_status(str(request.tool_call_id), "approved", "로컬 승인이 확인되었습니다.")

    async def _execute(self, request: ToolRequest, *, approval_id: str | None) -> None:
        call_id = str(request.tool_call_id)
        cancellation = threading.Event()
        self._cancellations[call_id] = cancellation
        self._active_requests[call_id] = request
        self.update_status(call_id, "queued", "실행 대기 중")
        try:
            async with self._parallel:
                if self._closed:
                    return
                if cancellation.is_set():
                    await self._send_cancelled(request)
                    return
                await self._send_event(request, ToolEventType.STARTED, ToolStatus.RUNNING)
                if self._closed:
                    return
                if cancellation.is_set():
                    await self._send_cancelled(request)
                    return
                self.update_status(call_id, "running", "실행 중")
                result = await asyncio.to_thread(
                    self.runtime.handle_tool_request,
                    request,
                    approval_id=approval_id,
                    cancellation=cancellation,
                )
            if self._closed:
                return
            if result.status is ToolStatus.DENIED:
                # A local grant can be revoked while execution is starting. Once
                # RUNNING has been recorded, represent that race as a failure.
                payload = result.model_dump(mode="python")
                payload["status"] = ToolStatus.FAILED
                result = ToolResult.model_validate(payload)
            self._remember_terminal(result)
            await self._send(result)
            label = {
                ToolStatus.COMPLETED: "완료",
                ToolStatus.CANCELLED: "취소됨",
                ToolStatus.TIMED_OUT: "시간 초과",
                ToolStatus.CLIENT_DISCONNECTED: "클라이언트 연결 끊김",
            }.get(result.status, "실패")
            self.update_status(call_id, result.status.value, label)
        finally:
            cancellation.set()
            self._cancellations.pop(call_id, None)
            self._active_requests.pop(call_id, None)

    async def _send_cancelled(self, request: ToolRequest) -> None:
        event = await self._send_event(
            request,
            ToolEventType.CANCELLED,
            ToolStatus.CANCELLED,
            error_code=ToolErrorCode.CANCELLED,
            error_message="The user cancelled this local tool request.",
        )
        self._remember_terminal(event)
        self.update_status(str(request.tool_call_id), "cancelled", "사용자가 취소했습니다.")

    def _local_validation_error(
        self, request: ToolRequest
    ) -> tuple[ToolErrorCode, str] | None:
        policy = self.runtime.load_policy()
        if not policy.agent_enabled or request.tool_name not in policy.enabled_tools:
            return ToolErrorCode.TOOL_DISABLED, "The tool is disabled by local policy."
        return None

    def _matching_reusable_approval(
        self, request: AgentToolRequest
    ) -> ApprovalGrant | None:
        definition = TOOL_REGISTRY.require(request.tool_name, request.tool_version)
        if definition.risk_level is ProtocolRiskLevel.LOCAL_WRITE:
            # Defense in depth for a hand-edited or legacy approval store.
            return None
        policy = self.runtime.load_policy()
        target = exact_target_for(request)
        scope = argument_scope_hash(request.arguments)
        for grant in self.runtime.approvals.list_active(policy):
            if grant.mode is ApprovalMode.ALLOW_ONCE:
                continue
            if (
                grant.client_id == request.target_client_id
                and grant.tool_name == request.tool_name
                and grant.tool_version == request.tool_version
                and grant.exact_target == target
                and grant.argument_scope_hash == scope
                and (
                    grant.mode is not ApprovalMode.ALLOW_SESSION
                    or grant.session_id == request.target_session_id
                )
            ):
                return grant
        return None

    def _approval_payload(self, request: ToolRequest, display_name: str) -> dict[str, Any]:
        arguments = dict(request.arguments)
        definition = TOOL_REGISTRY.require(request.tool_name, request.tool_version)
        policy = self.runtime.load_policy()
        modes = ["deny", "allow_once"]
        if definition.risk_level is not ProtocolRiskLevel.LOCAL_WRITE:
            modes.append("allow_session")
        if (
            definition.persistent_approval_supported
            and request.tool_name in policy.persistent_approval_tools
        ):
            modes.append("allow_always_exact")
        preview: dict[str, str] = {}
        if request.tool_name == "create_note":
            preview = {
                "title": str(arguments.get("title") or ""),
                "content": str(arguments.get("content") or ""),
            }
        elif request.tool_name == "set_reminder":
            preview = {
                "title": str(arguments.get("title") or ""),
                "content": str(arguments.get("reminder_text") or ""),
            }
        target_summary = self._readable_target_summary(request, policy)
        display_arguments = dict(arguments)
        if request.tool_name in {"open_folder", "read_text_file"}:
            display_arguments.pop("path", None)
            display_arguments.pop("path_ref", None)
        return {
            "tool_call_id": str(request.tool_call_id),
            "request_id": str(request.request_id),
            "display_name": display_name,
            "tool_name": request.tool_name,
            "action_summary": self._action_summary(request.tool_name, arguments),
            "target_client_id": self.client_id,
            "target_client_name": self.runtime.client_display_name,
            "target_summary": target_summary,
            "risk_level": request.risk_level.value,
            "arguments": display_arguments,
            "preview": preview,
            "approval_modes": modes,
            "cancellation_supported": definition.cancellation_supported,
            "expires_in_seconds": self.approval_timeout_seconds,
            "user_intent_summary": request.user_intent_summary,
        }

    @staticmethod
    def _readable_target_summary(
        request: ToolRequest, policy: AgentPolicy
    ) -> str:
        arguments = request.arguments
        if request.tool_name in {"open_folder", "read_text_file"}:
            validator = WindowsPathValidator(policy)
            path_ref = arguments.get("path_ref")
            if isinstance(path_ref, str) and path_ref:
                root_id, target = validator.resolve_path_ref(path_ref)
            else:
                root_id, target = None, Path(str(arguments.get("path") or ""))
            validated = validator.validate(
                target,
                root_id=root_id,
                expected_type=(
                    "directory" if request.tool_name == "open_folder" else "file"
                ),
                reject_sensitive=True,
            )
            root = policy.filesystem_roots[validated.root_id]
            relative = validated.relative_path or "."
            return f"{root.display_name} [{validated.root_id}] / {relative}"
        if request.tool_name == "search_files":
            root_id = str(arguments.get("root_id") or "")
            search_root = policy.filesystem_roots.get(root_id)
            if search_root is None:
                raise AgentError("path_not_allowed", "The requested search root is unavailable.")
            return f"{search_root.display_name} [{root_id}]"
        if request.tool_name == "open_application":
            application_id = str(arguments.get("application_id") or "")
            application = policy.applications.get(application_id)
            if application is None or not application.enabled:
                raise AgentError(
                    "target_not_found", "The requested application is not enabled locally."
                )
            return f"{application.display_name} [{application_id}]"
        return exact_target_for(to_agent_request(request))

    @staticmethod
    def _action_summary(tool_name: str, arguments: dict[str, Any]) -> str:
        summaries = {
            "get_active_window": "현재 활성 창의 제목과 프로세스 메타데이터를 읽습니다.",
            "open_application": f"등록된 앱 '{arguments.get('application_id', '-')}'을 엽니다.",
            "open_folder": "허용된 로컬 폴더를 엽니다.",
            "search_files": f"허용된 루트에서 '{arguments.get('query', '')}' 이름을 검색합니다.",
            "read_text_file": "허용된 텍스트 파일의 지정 범위를 읽습니다.",
            "create_note": f"Nivelle Notes에 '{arguments.get('title', '')}' 메모를 만듭니다.",
            "set_reminder": f"'{arguments.get('title', '')}' 알림을 저장합니다.",
        }
        return summaries.get(tool_name, "등록된 로컬 도구를 실행합니다.")

    @staticmethod
    def _denial_decision(
        request: AgentToolRequest, policy_version: str
    ) -> ToolApprovalDecision:
        return ToolApprovalDecision(
            approval_id=uuid4(),
            mode=ProtocolApprovalMode.DENY,
            decided_at=datetime.now(UTC),
            exact_target=exact_target_for(request),
            normalized_argument_scope=argument_scope_hash(request.arguments),
            policy_version=policy_version,
            reason="Denied through the local Nivelle Link UI.",
        )

    async def _send_event(
        self,
        request: ToolRequest,
        event_type: ToolEventType,
        status: ToolStatus,
        *,
        approval: ToolApprovalDecision | None = None,
        error_code: ToolErrorCode | None = None,
        error_message: str | None = None,
    ) -> ToolEvent:
        event = ToolEvent(
            type=event_type,
            tool_call_id=request.tool_call_id,
            request_id=request.request_id,
            target_client_id=request.target_client_id,
            target_session_id=request.target_session_id,
            status=status,
            occurred_at=datetime.now(UTC),
            approval=approval,
            error_code=error_code,
            error_message=error_message,
        )
        await self._send(event)
        return event

    async def _send(self, value: ClientCapabilities | ToolEvent | ToolResult) -> None:
        if self._closed:
            return
        await self.send_event(value.model_dump(mode="json"))

    @staticmethod
    def _fingerprint(request: ToolRequest) -> str:
        return json.dumps(
            request.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def _remember_fingerprint(self, call_id: str, fingerprint: str) -> None:
        self._fingerprints[call_id] = fingerprint
        self._fingerprints.move_to_end(call_id)
        while len(self._fingerprints) > 512:
            removable = next(
                (
                    key
                    for key in self._fingerprints
                    if key != call_id
                    and key not in self._pending
                    and key not in self._cancellations
                ),
                None,
            )
            if removable is None:
                break
            self._fingerprints.pop(removable, None)
            self._terminal_messages.pop(removable, None)

    def _remember_terminal(self, value: ToolEvent | ToolResult) -> None:
        call_id = str(value.tool_call_id)
        self._terminal_messages[call_id] = value
        self._terminal_messages.move_to_end(call_id)
        while len(self._terminal_messages) > 256:
            expired_call_id, _ = self._terminal_messages.popitem(last=False)
            if (
                expired_call_id not in self._pending
                and expired_call_id not in self._cancellations
            ):
                self._fingerprints.pop(expired_call_id, None)

    def set_enabled(self, enabled: bool) -> None:
        policy = self.runtime.load_policy()
        self.runtime.policy_store.save(policy.model_copy(update={"agent_enabled": enabled}))

    def revoke(self, approval_id: str) -> bool:
        return self.runtime.revoke_approval(approval_id)

    def cancel(self, tool_call_id: str) -> bool:
        """Request cancellation only for an advertised cancellable implementation."""

        if self._closed:
            return False
        request = self._active_requests.get(tool_call_id)
        cancellation = self._cancellations.get(tool_call_id)
        if request is None or cancellation is None:
            return False
        definition = TOOL_REGISTRY.require(request.tool_name, request.tool_version)
        if not definition.cancellation_supported or cancellation.is_set():
            return False
        cancellation.set()
        self.update_status(tool_call_id, "cancelling", "취소 요청 중")
        return True

    def snapshot(self, *, connected_core: str | None) -> dict[str, Any]:
        policy = self.runtime.load_policy()
        capabilities = {
            item.tool_name: item
            for item in self.runtime.capabilities(
                app_version=self.runtime.link_version,
                protocol_version=self.capabilities_protocol_version,
            ).tools
        }
        approvals = self.runtime.approvals.list_active(policy)
        audit = self.runtime.audit.list_recent()
        return {
            "enabled": policy.agent_enabled,
            "connected_core": connected_core,
            "client_id": self.client_id,
            "session_id": self.session_id,
            "enabled_tool_count": len(policy.enabled_tools) if policy.agent_enabled else 0,
            "pending_approval_count": self.pending_count,
            "recent_failure_count": sum(item.status != "completed" for item in audit[-20:]),
            "tools": [
                {
                    "name": definition.name,
                    "enabled": capabilities[definition.name].enabled,
                    "risk_level": definition.risk_level.value,
                    "approval_mode": capabilities[definition.name].default_approval_mode.value,
                    "available": capabilities[definition.name].implementation_available,
                    "timeout_ms": capabilities[definition.name].default_timeout_ms,
                }
                for definition in TOOL_REGISTRY
            ],
            "applications": [
                {
                    "application_id": application_id,
                    "display_name": item.display_name,
                    "executable_path": str(item.executable_path),
                    "enabled": item.enabled,
                    "persistent_approval": "open_application" in policy.persistent_approval_tools,
                }
                for application_id, item in sorted(policy.applications.items())
            ],
            "roots": [
                {
                    "root_id": root_id,
                    "display_name": item.display_name,
                    "path": str(item.path),
                    "allow_search": item.allow_search,
                    "allow_read": item.allow_read,
                    "allow_open": item.allow_open_folder,
                }
                for root_id, item in sorted(policy.filesystem_roots.items())
            ],
            "approvals": [
                {
                    "approval_id": item.approval_id,
                    "tool_name": item.tool_name,
                    "scope": item.exact_target,
                    "mode": item.mode.value,
                    "created_at": item.created_at.isoformat(),
                    "last_used_at": (
                        item.last_used_at.isoformat() if item.last_used_at else None
                    ),
                }
                for item in approvals
            ],
            "audit": [
                {
                    "created_at": item.completed_at.isoformat(),
                    "tool_name": item.tool_name,
                    "status": item.status,
                    "target_summary": item.target_summary,
                    "duration_ms": item.duration_ms,
                    "error_code": item.error_code,
                }
                for item in audit[-100:]
            ],
        }

    @property
    def capabilities_protocol_version(self) -> str:
        # The runtime stores the Link version but protocol version is supplied by
        # the caller when advertising. The wire module enforces this constant.
        from nivelle_protocol.version import PROTOCOL_VERSION

        return PROTOCOL_VERSION

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        active_call_ids = tuple(self._cancellations)
        for cancellation in self._cancellations.values():
            cancellation.set()
        self.runtime.shutdown()
        for call_id in dict.fromkeys((*self._pending, *active_call_ids)):
            self.update_status(call_id, "client_disconnected", "Agent 연결이 종료되었습니다.")
        self._pending.clear()


__all__ = ["AgentController"]
