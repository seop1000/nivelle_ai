"""Authenticated Nivelle Agent WebSocket session coordination.

The gateway is transport-agnostic: ``app.py`` owns authentication and the
WebSocket receive loop, while this module owns exact session routing,
capability validation, correlated tool traffic, result waiters, and disconnect
reconciliation.  Raw tool arguments/results are never persisted here.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID, uuid4

from nivelle_protocol.tools import (
    TOOL_REGISTRY,
    ApprovalMode,
    ClientCapabilities,
    ToolCapability,
    ToolErrorCode,
    ToolEvent,
    ToolEventType,
    ToolProtocolError,
    ToolRequest,
    ToolResult,
    ToolStatus,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .tool_orchestrator import ToolOrchestrator
from .tool_repository import ClientCapability, server_result_metadata_summary

JsonObject = dict[str, Any]
JsonSender = Callable[[JsonObject], Awaitable[None]]


class AgentGatewayError(RuntimeError):
    """Base error safe to surface as a bounded transport failure."""


class AgentAuthenticationMismatchError(AgentGatewayError):
    pass


class AgentSessionConflictError(AgentGatewayError):
    pass


class AgentSessionNotFoundError(AgentGatewayError):
    pass


class AgentCorrelationError(AgentGatewayError):
    pass


class AgentDuplicateDispatchError(AgentGatewayError):
    pass


class AgentConflictingTerminalError(AgentGatewayError):
    pass


class AgentResultTimeoutError(AgentGatewayError):
    pass


class AgentSessionDisconnectedError(AgentGatewayError):
    pass


class AgentRemoteTerminalError(AgentGatewayError):
    """A client reported a non-success terminal event without a ToolResult."""

    def __init__(self, status: ToolStatus, error_code: ToolErrorCode | None) -> None:
        self.status = status
        self.error_code = error_code
        super().__init__(f"Agent reported terminal status={status.value}")


@dataclass(frozen=True, slots=True)
class AgentSessionHandle:
    """Opaque binding between one authenticated socket and one session ID."""

    client_id: str
    session_id: str
    connection_id: str


@dataclass(slots=True)
class _SessionState:
    handle: AgentSessionHandle
    advertisement: ClientCapabilities
    capabilities: dict[tuple[str, str], ToolCapability]
    sender: JsonSender
    send_lock: asyncio.Lock
    connected_at: datetime


@dataclass(slots=True)
class _PendingCall:
    request: ToolRequest
    future: asyncio.Future[ToolResult]
    deadline_monotonic: float
    awaiting_approval: bool
    timeout_task: asyncio.Task[None] | None = None


@dataclass(frozen=True, slots=True)
class _TerminalRecord:
    result_id: str | None
    request_id: str
    target_client_id: str
    target_session_id: str
    status: str


class _SnapshotModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AgentSessionSnapshot(_SnapshotModel):
    client_id: str
    session_id: str
    platform: str
    app_version: str
    connected_at: datetime
    enabled_tools: tuple[str, ...]
    capability_count: int = Field(ge=0)


class PendingToolCallSnapshot(_SnapshotModel):
    tool_call_id: str
    request_id: str
    target_client_id: str
    target_session_id: str
    tool_name: str
    status: Literal["awaiting_result"] = "awaiting_result"


class AgentGatewaySnapshot(_SnapshotModel):
    sessions: tuple[AgentSessionSnapshot, ...]
    pending_calls: tuple[PendingToolCallSnapshot, ...]
    terminal_dedupe_count: int = Field(ge=0)


class AgentGateway:
    """Manage authenticated Agent sessions and correlated tool traffic."""

    def __init__(
        self,
        orchestrator: ToolOrchestrator,
        *,
        capability_ttl_seconds: int = 300,
        approval_timeout_seconds: float = 120,
        terminal_retention: int = 2_048,
        seen_session_retention: int = 2_048,
    ) -> None:
        if not 30 <= capability_ttl_seconds <= 86_400:
            raise ValueError("capability_ttl_seconds must be between 30 and 86400")
        if not 1 <= terminal_retention <= 10_000:
            raise ValueError("terminal_retention must be between 1 and 10000")
        if not 1 <= seen_session_retention <= 10_000:
            raise ValueError("seen_session_retention must be between 1 and 10000")
        if not 0.05 <= approval_timeout_seconds <= 600:
            raise ValueError("approval_timeout_seconds must be between 0.05 and 600")
        self.orchestrator = orchestrator
        self.capability_ttl_seconds = capability_ttl_seconds
        self.approval_timeout_seconds = approval_timeout_seconds
        self.terminal_retention = terminal_retention
        self.seen_session_retention = seen_session_retention
        self._sessions: dict[tuple[str, str], _SessionState] = {}
        self._pending: dict[str, _PendingCall] = {}
        self._terminal: OrderedDict[str, _TerminalRecord] = OrderedDict()
        self._seen_sessions: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._progress_sequences: dict[str, int] = {}
        self._state_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._dispatch_lock = asyncio.Lock()
        self._result_lock = asyncio.Lock()

    async def register(
        self,
        authenticated_client_id: str,
        payload: object,
        sender: JsonSender,
    ) -> AgentSessionHandle:
        """Validate and register the first capability event on an authenticated socket."""

        advertisement = self._validate_advertisement(authenticated_client_id, payload)
        client_id = str(advertisement.client_id)
        session_id = str(advertisement.session_id)
        key = (client_id, session_id)
        async with self._lifecycle_lock:
            async with self._state_lock:
                if key in self._seen_sessions:
                    raise AgentSessionConflictError(
                        "a session_id cannot be reused after registration"
                    )
                previous = [
                    state.handle
                    for (existing_client, _), state in self._sessions.items()
                    if existing_client == client_id
                ]
            for handle in previous:
                await self._disconnect_without_lifecycle(handle)

            connected_at = datetime.now(UTC)
            await self.orchestrator.advertise_capabilities(
                self._repository_capabilities(advertisement, connected_at),
                connected_at=connected_at.isoformat(),
            )
            handle = AgentSessionHandle(
                client_id=client_id,
                session_id=session_id,
                connection_id=str(uuid4()),
            )
            state = _SessionState(
                handle=handle,
                advertisement=advertisement,
                capabilities=self._capability_map(advertisement),
                sender=sender,
                send_lock=asyncio.Lock(),
                connected_at=connected_at,
            )
            async with self._state_lock:
                self._sessions[key] = state
                self._remember_seen_session_locked(key)
            return handle

    async def refresh_capabilities(
        self, handle: AgentSessionHandle, payload: object
    ) -> ClientCapabilities:
        """Refresh capabilities on the same live socket without changing its session ID."""

        advertisement = self._validate_advertisement(handle.client_id, payload)
        if str(advertisement.session_id) != handle.session_id:
            raise AgentCorrelationError("capability refresh changed target_session_id")
        async with self._lifecycle_lock:
            await self._require_session(handle)
            refreshed_at = datetime.now(UTC)
            await self.orchestrator.advertise_capabilities(
                self._repository_capabilities(advertisement, refreshed_at),
                connected_at=refreshed_at.isoformat(),
            )
            async with self._state_lock:
                state = self._require_session_locked(handle)
                state.advertisement = advertisement
                state.capabilities = self._capability_map(advertisement)
            return advertisement

    async def dispatch(self, request: ToolRequest) -> asyncio.Future[ToolResult]:
        """Send one exact-target request and return its single terminal-result Future.

        No-approval calls are queued before delivery.  Approval-required calls
        are delivered while durably awaiting approval and are queued only after
        the same Agent socket returns ``tool.approved``.
        """

        request = ToolRequest.model_validate(request.model_dump(mode="json"))
        TOOL_REGISTRY.validate_request(request)
        tool_call_id = str(request.tool_call_id)
        async with self._dispatch_lock:
            state, capability = await self._routing_state(request)
            call = await self._correlate_request_with_repository(request)
            if request.timeout_ms > capability.maximum_timeout_ms:
                raise ToolProtocolError("request timeout exceeds advertised client capability")
            async with self._state_lock:
                if tool_call_id in self._pending or tool_call_id in self._terminal:
                    raise AgentDuplicateDispatchError(
                        "tool_call_id was already dispatched and will not be sent again"
                    )
            approval_required = (
                capability.default_approval_mode is not ApprovalMode.NOT_REQUIRED
            )
            current_status = str(call["status"])
            if approval_required:
                if current_status != ToolStatus.AWAITING_APPROVAL.value:
                    raise AgentCorrelationError(
                        "approval-required request must be durably awaiting approval"
                    )
            else:
                if current_status != ToolStatus.VALIDATED.value:
                    raise AgentCorrelationError(
                        "no-approval request must be durably validated before dispatch"
                    )
                queued = await self.orchestrator.queue(
                    tool_call_id,
                    client_id=str(request.target_client_id),
                    session_id=str(request.target_session_id),
                )
                if not queued:
                    raise AgentDuplicateDispatchError(
                        "the durable tool call was already queued and will not be replayed"
                    )
            loop = asyncio.get_running_loop()
            future: asyncio.Future[ToolResult] = loop.create_future()
            future.add_done_callback(self._consume_unretrieved_exception)
            timeout_seconds = (
                self.approval_timeout_seconds
                if approval_required
                else request.timeout_ms / 1_000
            )
            pending = _PendingCall(
                request=request,
                future=future,
                deadline_monotonic=loop.time() + timeout_seconds,
                awaiting_approval=approval_required,
            )
            async with self._state_lock:
                current = self._sessions.get(
                    (str(request.target_client_id), str(request.target_session_id))
                )
                if current is not state:
                    raise AgentSessionDisconnectedError(
                        "target session disconnected before request delivery"
                    )
                self._pending[tool_call_id] = pending

        try:
            await asyncio.wait_for(
                self._send(state, request.model_dump(mode="json")),
                timeout=max(
                    0.001,
                    pending.deadline_monotonic - asyncio.get_running_loop().time(),
                ),
            )
        except asyncio.CancelledError:
            await self.disconnect(state.handle)
            raise
        except Exception as exc:
            # Do not keep an uncertain socket alive or replay the write.  The
            # disconnect path marks every in-flight call and resolves waiters.
            await self.disconnect(state.handle)
            raise AgentSessionDisconnectedError("tool request delivery failed") from exc

        async with self._state_lock:
            current_pending = self._pending.get(tool_call_id)
            if current_pending is pending and not future.done():
                self._ensure_timeout_locked(pending)
        return future

    async def execute(self, request: ToolRequest) -> ToolResult:
        """Convenience wrapper that dispatches and awaits the correlated result."""

        future = await self.dispatch(request)
        return await future

    async def send_payload(
        self, client_id: str, session_id: str, payload: Mapping[str, Any]
    ) -> None:
        """Send a prevalidated, metadata-only server event under the session lock."""

        async with self._state_lock:
            state = self._sessions.get((client_id, session_id))
        if state is None:
            raise AgentSessionNotFoundError("target Agent session is offline")
        await self._send(state, dict(payload))

    async def active_capabilities(
        self, client_id: str, session_id: str | None = None
    ) -> ClientCapabilities | None:
        """Return a detached snapshot of one client's current live advertisement.

        Registration enforces one live Agent session per authenticated client.
        ``session_id`` can additionally pin callers to an already selected
        session; an old or disconnected selection returns ``None``.
        """

        async with self._state_lock:
            if session_id is not None:
                state = self._sessions.get((client_id, session_id))
            else:
                state = next(
                    (
                        candidate
                        for (candidate_client_id, _), candidate in self._sessions.items()
                        if candidate_client_id == client_id
                    ),
                    None,
                )
            if state is None:
                return None
            return state.advertisement.model_copy(deep=True)

    async def handle_message(
        self, handle: AgentSessionHandle, payload: object
    ) -> ClientCapabilities | ToolEvent | ToolResult:
        """Route one decoded WebSocket JSON object through strict shared models."""

        if not isinstance(payload, Mapping):
            raise AgentGatewayError("Agent messages must be JSON objects")
        event_type = payload.get("type")
        if event_type == "client.capabilities":
            return await self.refresh_capabilities(handle, payload)
        if event_type == "tool.result":
            result = ToolResult.model_validate(payload)
            await self.handle_result(handle, result)
            return result
        if isinstance(event_type, str) and event_type.startswith("tool."):
            event = ToolEvent.model_validate(payload)
            await self.handle_event(handle, event)
            return event
        raise AgentGatewayError("unsupported Agent message type")

    async def handle_event(self, handle: AgentSessionHandle, event: ToolEvent) -> bool:
        """Validate an Agent event and apply its legal orchestration transition."""

        event = ToolEvent.model_validate(event.model_dump(mode="json"))
        async with self._dispatch_lock:
            await self._require_session(handle)
            self._assert_target(handle, event.target_client_id, event.target_session_id)
            call = await self._correlate_call(
                tool_call_id=str(event.tool_call_id),
                request_id=str(event.request_id),
                client_id=handle.client_id,
                session_id=handle.session_id,
            )
            return await self._handle_event_locked(handle, event, current=str(call["status"]))

    async def _handle_event_locked(
        self,
        handle: AgentSessionHandle,
        event: ToolEvent,
        *,
        current: str,
    ) -> bool:
        """Apply an event while ``_dispatch_lock`` serializes local transitions."""

        tool_call_id = str(event.tool_call_id)
        if event.type in {
            ToolEventType.PROPOSED,
            ToolEventType.REQUEST,
            ToolEventType.APPROVAL_REQUIRED,
            ToolEventType.QUEUED,
        }:
            raise AgentCorrelationError("the client cannot originate this server-side event")

        if event.type is ToolEventType.APPROVED:
            if current in {ToolStatus.QUEUED.value, ToolStatus.RUNNING.value}:
                return False
            if current not in {
                ToolStatus.AWAITING_APPROVAL.value,
                ToolStatus.APPROVED.value,
            }:
                raise AgentCorrelationError("tool.approved is not valid for the durable state")
            pending = await self._require_pending_event(event)
            if not pending.awaiting_approval:
                return False
            approved = await self.orchestrator.approve(
                tool_call_id,
                client_id=handle.client_id,
                session_id=handle.session_id,
                safe_summary="user approved the exact local tool request",
            )
            try:
                queued = await self.orchestrator.queue(
                    tool_call_id,
                    client_id=handle.client_id,
                    session_id=handle.session_id,
                )
            except Exception as exc:
                # APPROVED has no safe failure transition other than exact-session
                # disconnect.  Never leave a waiter stranded in that state.
                await self.disconnect(handle)
                raise AgentSessionDisconnectedError(
                    "approved request could not be queued safely"
                ) from exc
            await self._begin_execution_timeout(pending)
            return approved or queued

        if event.type is ToolEventType.DENIED:
            if current == ToolStatus.DENIED.value:
                return False
            if current != ToolStatus.AWAITING_APPROVAL.value:
                raise AgentCorrelationError("tool.denied is not valid for the durable state")
            await self._require_pending_event(event)
            changed = await self.orchestrator.deny(
                tool_call_id,
                client_id=handle.client_id,
                session_id=handle.session_id,
                safe_summary="user denied the local tool request",
            )
            await self._finish_event_failure(
                handle,
                event,
                durable_status=ToolStatus.DENIED,
            )
            return changed

        if event.type is ToolEventType.STARTED:
            if current == ToolStatus.RUNNING.value:
                return False
            return await self.orchestrator.start(
                tool_call_id,
                client_id=handle.client_id,
                session_id=handle.session_id,
            )

        if event.type is ToolEventType.PROGRESS:
            if event.progress is None:  # enforced by ToolEvent, kept for typing
                raise AgentGatewayError("tool.progress payload is missing")
            async with self._state_lock:
                previous_sequence = self._progress_sequences.get(tool_call_id, 0)
                if event.progress.sequence <= previous_sequence:
                    return False
            summary = self._progress_summary(event)
            changed = await self.orchestrator.progress(
                tool_call_id,
                client_id=handle.client_id,
                session_id=handle.session_id,
                safe_summary=summary,
            )
            if changed:
                async with self._state_lock:
                    self._progress_sequences[tool_call_id] = event.progress.sequence
            return changed

        if event.type is ToolEventType.CLIENT_DISCONNECTED:
            await self.disconnect(handle)
            return True

        # A completion event cannot establish success without its typed result.
        if event.type is ToolEventType.COMPLETED:
            if current != ToolStatus.RUNNING.value:
                raise AgentCorrelationError("tool.completed arrived before execution")
            return True

        if event.type in {
            ToolEventType.FAILED,
            ToolEventType.CANCELLED,
            ToolEventType.TIMED_OUT,
            ToolEventType.VALIDATION_FAILED,
        }:
            # A local validation/policy race can occur after an approval prompt
            # has been displayed.  The durable graph cannot move from
            # AWAITING_APPROVAL to FAILED/VALIDATION_FAILED/CANCELLED, so close
            # it as a safe denial and always resolve the waiter.
            if current == ToolStatus.AWAITING_APPROVAL.value:
                await self._require_pending_event(event)
                if event.type is ToolEventType.TIMED_OUT:
                    await self.orchestrator.time_out(
                        tool_call_id,
                        client_id=handle.client_id,
                        session_id=handle.session_id,
                        approval_timeout=True,
                    )
                    durable_status = ToolStatus.TIMED_OUT
                else:
                    await self.orchestrator.deny(
                        tool_call_id,
                        client_id=handle.client_id,
                        session_id=handle.session_id,
                        safe_summary="local approval request became unavailable",
                    )
                    durable_status = ToolStatus.DENIED
                await self._finish_event_failure(
                    handle,
                    event,
                    durable_status=durable_status,
                )
                return True

            if current == ToolStatus.QUEUED.value and event.type is not ToolEventType.CANCELLED:
                await self.orchestrator.start(
                    tool_call_id,
                    client_id=handle.client_id,
                    session_id=handle.session_id,
                )
            if event.type in {ToolEventType.FAILED, ToolEventType.VALIDATION_FAILED}:
                await self.orchestrator.fail(
                    tool_call_id,
                    client_id=handle.client_id,
                    session_id=handle.session_id,
                    error_code=(event.error_code or ToolErrorCode.EXECUTION_FAILED).value,
                )
                durable_status = ToolStatus.FAILED
            elif event.type is ToolEventType.CANCELLED:
                await self.orchestrator.cancel(
                    tool_call_id,
                    client_id=handle.client_id,
                    session_id=handle.session_id,
                    safe_summary="client reported local tool cancellation",
                )
                durable_status = ToolStatus.CANCELLED
            else:
                await self.orchestrator.time_out(
                    tool_call_id,
                    client_id=handle.client_id,
                    session_id=handle.session_id,
                )
                durable_status = ToolStatus.TIMED_OUT
            await self._finish_event_failure(
                handle,
                event,
                durable_status=durable_status,
            )
            return True
        raise AgentGatewayError(f"unhandled Agent event: {event.type.value}")

    async def handle_result(self, handle: AgentSessionHandle, result: ToolResult) -> bool:
        """Accept one exact-target terminal result and resolve its waiter once."""

        result = ToolResult.model_validate(result.model_dump(mode="json"))
        await self._require_session(handle)
        self._assert_target(handle, result.target_client_id, result.target_session_id)
        TOOL_REGISTRY.validate_result(result)
        tool_call_id = str(result.tool_call_id)
        async with self._result_lock:
            call = await self._correlate_result_with_repository(result)
            async with self._state_lock:
                previous = self._terminal.get(tool_call_id)
            if previous is not None:
                incoming = self._terminal_record(result)
                if previous == incoming or (
                    previous.result_id is None
                    and previous.request_id == incoming.request_id
                    and previous.target_client_id == incoming.target_client_id
                    and previous.target_session_id == incoming.target_session_id
                    and previous.status == incoming.status
                ):
                    return False
                raise AgentConflictingTerminalError(
                    "conflicting terminal result for an existing tool_call_id"
                )

            current = str(call["status"])
            if current == ToolStatus.QUEUED.value and result.status is not ToolStatus.CANCELLED:
                await self.orchestrator.start(
                    tool_call_id,
                    client_id=handle.client_id,
                    session_id=handle.session_id,
                )
                current = ToolStatus.RUNNING.value
            await self._apply_result_transition(handle, result, current=current)
            await self._finish_with_result(result)

        if result.status is ToolStatus.CLIENT_DISCONNECTED:
            await self.disconnect(handle)
        return True

    async def disconnect(self, handle: AgentSessionHandle) -> int:
        """Expire one exact session and fail all of its unresolved waiters."""

        async with self._lifecycle_lock:
            return await self._disconnect_without_lifecycle(handle)

    async def snapshot(self) -> AgentGatewaySnapshot:
        """Return metadata-only live state; arguments and results are omitted."""

        async with self._state_lock:
            sessions = tuple(
                AgentSessionSnapshot(
                    client_id=state.handle.client_id,
                    session_id=state.handle.session_id,
                    platform=state.advertisement.platform.value,
                    app_version=state.advertisement.app_version,
                    connected_at=state.connected_at,
                    enabled_tools=tuple(
                        sorted(
                            capability.tool_name
                            for capability in state.capabilities.values()
                            if capability.enabled and capability.implementation_available
                        )
                    ),
                    capability_count=len(state.capabilities),
                )
                for state in sorted(
                    self._sessions.values(),
                    key=lambda item: (item.handle.client_id, item.handle.session_id),
                )
            )
            pending = tuple(
                PendingToolCallSnapshot(
                    tool_call_id=str(item.request.tool_call_id),
                    request_id=str(item.request.request_id),
                    target_client_id=str(item.request.target_client_id),
                    target_session_id=str(item.request.target_session_id),
                    tool_name=item.request.tool_name,
                )
                for item in sorted(
                    self._pending.values(), key=lambda item: str(item.request.tool_call_id)
                )
            )
            return AgentGatewaySnapshot(
                sessions=sessions,
                pending_calls=pending,
                terminal_dedupe_count=len(self._terminal),
            )

    def _validate_advertisement(
        self, authenticated_client_id: str, payload: object
    ) -> ClientCapabilities:
        try:
            authenticated_uuid = UUID(str(authenticated_client_id))
        except ValueError as exc:
            raise AgentAuthenticationMismatchError(
                "authenticated client_id is not a canonical UUID"
            ) from exc
        try:
            advertisement = ClientCapabilities.model_validate(payload)
            TOOL_REGISTRY.validate_capabilities(advertisement)
        except (ValidationError, ToolProtocolError) as exc:
            raise AgentGatewayError("invalid client capability advertisement") from exc
        if advertisement.client_id != authenticated_uuid:
            raise AgentAuthenticationMismatchError(
                "advertised client_id does not match the authenticated client"
            )
        if not advertisement.tools:
            raise AgentGatewayError("at least one tool capability must be advertised")
        return advertisement

    def _repository_capabilities(
        self, advertisement: ClientCapabilities, connected_at: datetime
    ) -> list[ClientCapability]:
        expires_at = connected_at + timedelta(seconds=self.capability_ttl_seconds)
        return [
            ClientCapability(
                client_id=str(advertisement.client_id),
                session_id=str(advertisement.session_id),
                platform=advertisement.platform.value,
                app_version=advertisement.app_version,
                protocol_version=advertisement.protocol_version,
                tool_name=capability.tool_name,
                tool_version=capability.tool_version,
                enabled=capability.enabled,
                implementation_available=capability.implementation_available,
                risk_level=capability.risk_level.value,
                default_approval_required=(
                    capability.default_approval_mode is not ApprovalMode.NOT_REQUIRED
                ),
                default_timeout_ms=capability.default_timeout_ms,
                maximum_timeout_ms=capability.maximum_timeout_ms,
                maximum_result_size=capability.maximum_result_size_bytes,
                expires_at=expires_at.isoformat(),
            )
            for capability in advertisement.tools
        ]

    @staticmethod
    def _capability_map(
        advertisement: ClientCapabilities,
    ) -> dict[tuple[str, str], ToolCapability]:
        return {
            (capability.tool_name, capability.tool_version): capability
            for capability in advertisement.tools
        }

    async def _routing_state(self, request: ToolRequest) -> tuple[_SessionState, ToolCapability]:
        key = (str(request.target_client_id), str(request.target_session_id))
        async with self._state_lock:
            state = self._sessions.get(key)
            if state is None:
                raise AgentSessionNotFoundError("exact target Agent session is offline")
            capability = state.capabilities.get((request.tool_name, request.tool_version))
            if (
                capability is None
                or not capability.enabled
                or not capability.implementation_available
            ):
                raise AgentSessionNotFoundError(
                    "exact target session does not advertise this enabled tool"
                )
            return state, capability

    async def _require_session(self, handle: AgentSessionHandle) -> _SessionState:
        async with self._state_lock:
            return self._require_session_locked(handle)

    def _require_session_locked(self, handle: AgentSessionHandle) -> _SessionState:
        state = self._sessions.get((handle.client_id, handle.session_id))
        if state is None or state.handle.connection_id != handle.connection_id:
            raise AgentSessionNotFoundError("Agent socket session is no longer current")
        return state

    async def _send(self, state: _SessionState, payload: JsonObject) -> None:
        async with state.send_lock:
            await state.sender(payload)

    async def _correlate_request_with_repository(self, request: ToolRequest) -> JsonObject:
        call = await self._correlate_call(
            tool_call_id=str(request.tool_call_id),
            request_id=str(request.request_id),
            client_id=str(request.target_client_id),
            session_id=str(request.target_session_id),
        )
        expected = {
            "idempotency_key": str(request.idempotency_key),
            "conversation_id": str(request.conversation_id),
            "user_message_id": str(request.user_message_id),
            "tool_name": request.tool_name,
            "tool_version": request.tool_version,
            "risk_level": request.risk_level.value,
        }
        for field, value in expected.items():
            if str(call[field]) != value:
                raise AgentCorrelationError(
                    f"ToolRequest {field} does not match its durable tool call"
                )
        return call

    async def _correlate_result_with_repository(self, result: ToolResult) -> JsonObject:
        call = await self._correlate_call(
            tool_call_id=str(result.tool_call_id),
            request_id=str(result.request_id),
            client_id=str(result.target_client_id),
            session_id=str(result.target_session_id),
        )
        if str(call["tool_name"]) != result.tool_name:
            raise AgentCorrelationError("ToolResult tool_name does not match the request")
        if str(call["tool_version"]) != result.tool_version:
            raise AgentCorrelationError("ToolResult tool_version does not match the request")
        return call

    async def _correlate_call(
        self,
        *,
        tool_call_id: str,
        request_id: str,
        client_id: str,
        session_id: str,
    ) -> JsonObject:
        call = await self.orchestrator.repository.get_tool_call(tool_call_id)
        if call is None:
            raise AgentCorrelationError("tool_call_id does not exist")
        expected = {
            "request_id": request_id,
            "target_client_id": client_id,
            "target_session_id": session_id,
        }
        for field, value in expected.items():
            if str(call[field]) != value:
                raise AgentCorrelationError(f"incoming Agent message has a mismatched {field}")
        return call

    @staticmethod
    def _assert_target(handle: AgentSessionHandle, client_id: UUID, session_id: UUID) -> None:
        if str(client_id) != handle.client_id or str(session_id) != handle.session_id:
            raise AgentCorrelationError(
                "incoming Agent message belongs to another client or session"
            )

    async def _require_pending_event(self, event: ToolEvent) -> _PendingCall:
        tool_call_id = str(event.tool_call_id)
        async with self._state_lock:
            pending = self._pending.get(tool_call_id)
        if pending is None:
            raise AgentCorrelationError("Agent event has no dispatched request waiter")
        request = pending.request
        if (
            request.request_id != event.request_id
            or request.target_client_id != event.target_client_id
            or request.target_session_id != event.target_session_id
        ):
            raise AgentCorrelationError("Agent event does not match its dispatched request")
        return pending

    async def _begin_execution_timeout(self, pending: _PendingCall) -> None:
        tool_call_id = str(pending.request.tool_call_id)
        async with self._state_lock:
            if self._pending.get(tool_call_id) is not pending or pending.future.done():
                raise AgentCorrelationError("approved request waiter is no longer active")
            self._cancel_timeout(pending)
            pending.awaiting_approval = False
            pending.deadline_monotonic = (
                asyncio.get_running_loop().time() + pending.request.timeout_ms / 1_000
            )
            pending.timeout_task = None
            self._ensure_timeout_locked(pending)

    async def _finish_event_failure(
        self,
        handle: AgentSessionHandle,
        event: ToolEvent,
        *,
        durable_status: ToolStatus,
    ) -> None:
        await self._finish_with_exception(
            str(event.tool_call_id),
            _TerminalRecord(
                result_id=None,
                request_id=str(event.request_id),
                target_client_id=handle.client_id,
                target_session_id=handle.session_id,
                status=durable_status.value,
            ),
            AgentRemoteTerminalError(event.status, event.error_code),
        )

    async def _apply_result_transition(
        self,
        handle: AgentSessionHandle,
        result: ToolResult,
        *,
        current: str,
    ) -> None:
        tool_call_id = str(result.tool_call_id)
        common = {
            "client_id": handle.client_id,
            "session_id": handle.session_id,
        }
        if result.status is ToolStatus.COMPLETED:
            await self.orchestrator.complete(
                tool_call_id,
                duration_ms=result.duration_ms or 0,
                **common,
            )
        elif result.status is ToolStatus.FAILED:
            await self.orchestrator.fail(
                tool_call_id,
                error_code=(result.error_code or ToolErrorCode.EXECUTION_FAILED).value,
                duration_ms=result.duration_ms,
                **common,
            )
        elif result.status is ToolStatus.CANCELLED:
            await self.orchestrator.cancel(
                tool_call_id,
                safe_summary=server_result_metadata_summary(
                    tool_name=result.tool_name,
                    status=result.status.value,
                    error_code=(
                        result.error_code.value if result.error_code is not None else None
                    ),
                ),
                **common,
            )
        elif result.status is ToolStatus.TIMED_OUT:
            await self.orchestrator.time_out(
                tool_call_id,
                client_id=handle.client_id,
                session_id=handle.session_id,
            )
        elif result.status in {ToolStatus.DENIED, ToolStatus.CLIENT_DISCONNECTED}:
            # Once a request is queued, local policy denial/disconnect is an
            # execution failure in the durable state machine.  The ToolResult
            # returned to the model retains the more precise client status.
            if current != ToolStatus.RUNNING.value:
                raise AgentCorrelationError("local terminal result arrived before execution")
            await self.orchestrator.fail(
                tool_call_id,
                error_code=(result.error_code or ToolErrorCode.EXECUTION_FAILED).value,
                duration_ms=result.duration_ms,
                **common,
            )
        else:  # ToolResult rejects every other state before this point.
            raise AgentGatewayError(f"unsupported ToolResult status: {result.status.value}")

    async def _finish_with_result(self, result: ToolResult) -> None:
        tool_call_id = str(result.tool_call_id)
        async with self._state_lock:
            pending = self._pending.pop(tool_call_id, None)
            self._remember_terminal_locked(tool_call_id, self._terminal_record(result))
            self._progress_sequences.pop(tool_call_id, None)
        if pending is not None:
            self._cancel_timeout(pending)
            if not pending.future.done():
                pending.future.set_result(result)

    async def _finish_with_exception(
        self,
        tool_call_id: str,
        terminal: _TerminalRecord,
        error: AgentGatewayError,
    ) -> None:
        async with self._state_lock:
            pending = self._pending.pop(tool_call_id, None)
            self._remember_terminal_locked(tool_call_id, terminal)
            self._progress_sequences.pop(tool_call_id, None)
        if pending is not None:
            self._cancel_timeout(pending)
            if not pending.future.done():
                pending.future.set_exception(error)

    async def _expire_call(self, pending: _PendingCall) -> None:
        try:
            await asyncio.sleep(
                max(0.0, pending.deadline_monotonic - asyncio.get_running_loop().time())
            )
            tool_call_id = str(pending.request.tool_call_id)
            async with self._dispatch_lock:
                async with self._result_lock:
                    async with self._state_lock:
                        if self._pending.get(tool_call_id) is not pending:
                            return
                    call = await self.orchestrator.repository.get_tool_call(tool_call_id)
                    if call is None:
                        raise AgentCorrelationError("timed-out tool call no longer exists")
                    current = str(call["status"])
                    approval_timeout = (
                        pending.awaiting_approval
                        or current == ToolStatus.AWAITING_APPROVAL.value
                    )
                    if approval_timeout:
                        await self.orchestrator.time_out(
                            tool_call_id,
                            client_id=str(pending.request.target_client_id),
                            session_id=str(pending.request.target_session_id),
                            approval_timeout=True,
                        )
                    else:
                        if current == ToolStatus.QUEUED.value:
                            await self.orchestrator.start(
                                tool_call_id,
                                client_id=str(pending.request.target_client_id),
                                session_id=str(pending.request.target_session_id),
                            )
                        await self.orchestrator.time_out(
                            tool_call_id,
                            client_id=str(pending.request.target_client_id),
                            session_id=str(pending.request.target_session_id),
                        )
                    async with self._state_lock:
                        if self._pending.pop(tool_call_id, None) is not pending:
                            return
                        self._remember_terminal_locked(
                            tool_call_id,
                            _TerminalRecord(
                                result_id=None,
                                request_id=str(pending.request.request_id),
                                target_client_id=str(pending.request.target_client_id),
                                target_session_id=str(pending.request.target_session_id),
                                status=ToolStatus.TIMED_OUT.value,
                            ),
                        )
                        self._progress_sequences.pop(tool_call_id, None)
                    if not pending.future.done():
                        message = (
                            "Agent approval timed out before execution"
                            if approval_timeout
                            else "Agent result timed out; execution is uncertain"
                        )
                        pending.future.set_exception(AgentResultTimeoutError(message))
        except asyncio.CancelledError:
            return
        except Exception:
            tool_call_id = str(pending.request.tool_call_id)
            async with self._state_lock:
                if self._pending.pop(tool_call_id, None) is not pending:
                    return
                self._remember_terminal_locked(
                    tool_call_id,
                    _TerminalRecord(
                        result_id=None,
                        request_id=str(pending.request.request_id),
                        target_client_id=str(pending.request.target_client_id),
                        target_session_id=str(pending.request.target_session_id),
                        status=ToolStatus.TIMED_OUT.value,
                    ),
                )
                self._progress_sequences.pop(tool_call_id, None)
            if not pending.future.done():
                pending.future.set_exception(
                    AgentResultTimeoutError("Agent timeout reconciliation failed")
                )

    async def _disconnect_without_lifecycle(self, handle: AgentSessionHandle) -> int:
        async with self._state_lock:
            state = self._sessions.get((handle.client_id, handle.session_id))
            if state is None or state.handle.connection_id != handle.connection_id:
                return 0
            self._sessions.pop((handle.client_id, handle.session_id), None)
            pending = [
                item
                for item in self._pending.values()
                if str(item.request.target_client_id) == handle.client_id
                and str(item.request.target_session_id) == handle.session_id
            ]
            for item in pending:
                self._pending.pop(str(item.request.tool_call_id), None)
        transitioned = await self.orchestrator.disconnect(
            client_id=handle.client_id, session_id=handle.session_id
        )
        for item in pending:
            self._cancel_timeout(item)
            tool_call_id = str(item.request.tool_call_id)
            async with self._state_lock:
                self._remember_terminal_locked(
                    tool_call_id,
                    _TerminalRecord(
                        result_id=None,
                        request_id=str(item.request.request_id),
                        target_client_id=handle.client_id,
                        target_session_id=handle.session_id,
                        status=ToolStatus.CLIENT_DISCONNECTED.value,
                    ),
                )
                self._progress_sequences.pop(tool_call_id, None)
            if not item.future.done():
                item.future.set_exception(
                    AgentSessionDisconnectedError(
                        "Agent session disconnected; the operation was not assumed successful"
                    )
                )
        return transitioned

    @staticmethod
    def _progress_summary(event: ToolEvent) -> str:
        if event.progress is None:
            return "tool progress received"
        if event.progress.total_units is None:
            return f"progress sequence {event.progress.sequence}"
        return (
            f"progress {event.progress.completed_units}/"
            f"{event.progress.total_units} {event.progress.unit}"
        )

    @staticmethod
    def _terminal_record(result: ToolResult) -> _TerminalRecord:
        return _TerminalRecord(
            result_id=str(result.result_id),
            request_id=str(result.request_id),
            target_client_id=str(result.target_client_id),
            target_session_id=str(result.target_session_id),
            status=result.status.value,
        )

    def _remember_terminal_locked(self, tool_call_id: str, record: _TerminalRecord) -> None:
        self._terminal[tool_call_id] = record
        self._terminal.move_to_end(tool_call_id)
        while len(self._terminal) > self.terminal_retention:
            self._terminal.popitem(last=False)

    def _remember_seen_session_locked(self, key: tuple[str, str]) -> None:
        self._seen_sessions[key] = None
        self._seen_sessions.move_to_end(key)
        while len(self._seen_sessions) > self.seen_session_retention:
            self._seen_sessions.popitem(last=False)

    def _ensure_timeout_locked(self, pending: _PendingCall) -> None:
        task = pending.timeout_task
        if task is not None and not task.done():
            return
        tool_call_id = str(pending.request.tool_call_id)
        pending.timeout_task = asyncio.create_task(
            self._expire_call(pending),
            name=f"nivelle-agent-timeout-{tool_call_id}",
        )

    @staticmethod
    def _cancel_timeout(pending: _PendingCall) -> None:
        task = pending.timeout_task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()

    @staticmethod
    def _consume_unretrieved_exception(future: asyncio.Future[ToolResult]) -> None:
        if future.cancelled():
            return
        try:
            future.exception()
        except (asyncio.CancelledError, Exception):
            pass


__all__ = [
    "AgentAuthenticationMismatchError",
    "AgentConflictingTerminalError",
    "AgentCorrelationError",
    "AgentDuplicateDispatchError",
    "AgentGateway",
    "AgentGatewayError",
    "AgentGatewaySnapshot",
    "AgentResultTimeoutError",
    "AgentRemoteTerminalError",
    "AgentSessionConflictError",
    "AgentSessionDisconnectedError",
    "AgentSessionHandle",
    "AgentSessionNotFoundError",
    "AgentSessionSnapshot",
    "PendingToolCallSnapshot",
]
