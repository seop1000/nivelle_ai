from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from nivelle_protocol.tools import ApprovalMode

from .tool_repository import (
    APPROVED,
    AWAITING_APPROVAL,
    CANCELLED,
    CLIENT_DISCONNECTED,
    COMPLETED,
    FAILED,
    QUEUED,
    RUNNING,
    TERMINAL_STATUSES,
    TIMED_OUT,
    VALIDATED,
    VALIDATION_FAILED,
    ClientCapability,
    InvalidToolTransitionError,
    ToolCallCreate,
    ToolCallNotFoundError,
    ToolRepository,
)


class ToolCapabilityUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ToolOrchestrationLimits:
    max_parallel_calls_per_client: int = 2
    max_calls_per_turn: int = 3
    idempotency_retention_days: int = 90

    def __post_init__(self) -> None:
        if self.max_parallel_calls_per_client < 1:
            raise ValueError("max_parallel_calls_per_client must be positive")
        if self.max_calls_per_turn < 1:
            raise ValueError("max_calls_per_turn must be positive")
        if self.idempotency_retention_days < 1:
            raise ValueError("idempotency_retention_days must be positive")


@dataclass(frozen=True, slots=True)
class ProposalOutcome:
    call: dict[str, Any]
    replayed: bool


class ToolOrchestrator:
    """Persistence and session coordinator for the Phase 3 tool state machine.

    It intentionally does not know about WebSockets, model prompts, or local
    execution. Callers must deliver requests/results through their own transport.
    """

    def __init__(
        self,
        repository: ToolRepository,
        limits: ToolOrchestrationLimits | None = None,
    ) -> None:
        self.repository = repository
        self.limits = limits or ToolOrchestrationLimits()

    async def advertise_capabilities(
        self, capabilities: Sequence[ClientCapability], *, connected_at: str | None = None
    ) -> None:
        await self.repository.replace_capabilities(capabilities, connected_at=connected_at)

    async def propose(
        self, call: ToolCallCreate, *, created_at: str | None = None
    ) -> ProposalOutcome:
        persisted, created = await self.repository.create_tool_call(
            call,
            max_calls_per_turn=self.limits.max_calls_per_turn,
            idempotency_retention_days=self.limits.idempotency_retention_days,
            created_at=created_at,
        )
        return ProposalOutcome(call=persisted, replayed=not created)

    async def validate(self, tool_call_id: str, *, at: str | None = None) -> bool:
        call = await self._require_call(tool_call_id)
        capability = await self._live_capability(call, at=at)
        if capability is None:
            await self.repository.transition(
                tool_call_id,
                VALIDATION_FAILED,
                safe_summary="exact target session has no live matching capability",
                error_code="client_offline",
                occurred_at=at,
            )
            return False
        await self.repository.transition(tool_call_id, VALIDATED, occurred_at=at)
        return True

    async def require_approval(
        self, tool_call_id: str, *, safe_summary: str = "", at: str | None = None
    ) -> bool:
        return await self.repository.transition(
            tool_call_id,
            AWAITING_APPROVAL,
            safe_summary=safe_summary,
            occurred_at=at,
        )

    async def approve(
        self,
        tool_call_id: str,
        *,
        client_id: str,
        session_id: str,
        safe_summary: str = "",
        at: str | None = None,
    ) -> bool:
        return await self.repository.record_approval(
            tool_call_id,
            approved=True,
            target_client_id=client_id,
            target_session_id=session_id,
            safe_summary=safe_summary,
            occurred_at=at,
        )

    async def deny(
        self,
        tool_call_id: str,
        *,
        client_id: str,
        session_id: str,
        safe_summary: str = "",
        at: str | None = None,
    ) -> bool:
        return await self.repository.record_approval(
            tool_call_id,
            approved=False,
            target_client_id=client_id,
            target_session_id=session_id,
            safe_summary=safe_summary,
            occurred_at=at,
        )

    async def queue(
        self,
        tool_call_id: str,
        *,
        client_id: str,
        session_id: str,
        at: str | None = None,
    ) -> bool:
        call = await self._require_call(tool_call_id)
        self._assert_requested_target(call, client_id, session_id)
        if (
            str(call["status"]) == VALIDATED
            and str(call["approval_mode"]) != ApprovalMode.NOT_REQUIRED.value
        ):
            raise InvalidToolTransitionError("approval-required call cannot skip approval")
        await self._require_live_capability(call, at=at)
        return await self.repository.queue_with_parallel_limit(
            tool_call_id,
            target_client_id=client_id,
            target_session_id=session_id,
            max_parallel_calls_per_client=self.limits.max_parallel_calls_per_client,
            occurred_at=at,
        )

    async def start(
        self,
        tool_call_id: str,
        *,
        client_id: str,
        session_id: str,
        at: str | None = None,
    ) -> bool:
        call = await self._require_call(tool_call_id)
        self._assert_requested_target(call, client_id, session_id)
        await self._require_live_capability(call, at=at)
        return await self.repository.transition(
            tool_call_id,
            RUNNING,
            target_client_id=client_id,
            target_session_id=session_id,
            occurred_at=at,
        )

    async def progress(
        self,
        tool_call_id: str,
        *,
        client_id: str,
        session_id: str,
        safe_summary: str,
        at: str | None = None,
    ) -> bool:
        return await self.repository.record_progress(
            tool_call_id,
            target_client_id=client_id,
            target_session_id=session_id,
            safe_summary=safe_summary,
            occurred_at=at,
        )

    async def complete(
        self,
        tool_call_id: str,
        *,
        client_id: str,
        session_id: str,
        duration_ms: int,
        at: str | None = None,
    ) -> bool:
        return await self.repository.record_result(
            tool_call_id,
            status=COMPLETED,
            target_client_id=client_id,
            target_session_id=session_id,
            error_code=None,
            duration_ms=duration_ms,
            occurred_at=at,
        )

    async def fail(
        self,
        tool_call_id: str,
        *,
        client_id: str,
        session_id: str,
        error_code: str = "execution_failed",
        duration_ms: int | None = None,
        at: str | None = None,
    ) -> bool:
        return await self.repository.record_result(
            tool_call_id,
            status=FAILED,
            target_client_id=client_id,
            target_session_id=session_id,
            error_code=error_code,
            duration_ms=duration_ms,
            occurred_at=at,
        )

    async def cancel(
        self,
        tool_call_id: str,
        *,
        client_id: str,
        session_id: str,
        safe_summary: str = "",
        at: str | None = None,
    ) -> bool:
        return await self.repository.transition(
            tool_call_id,
            CANCELLED,
            target_client_id=client_id,
            target_session_id=session_id,
            safe_summary=safe_summary,
            error_code="cancelled",
            occurred_at=at,
        )

    async def time_out(
        self,
        tool_call_id: str,
        *,
        client_id: str,
        session_id: str,
        approval_timeout: bool = False,
        at: str | None = None,
    ) -> bool:
        return await self.repository.transition(
            tool_call_id,
            TIMED_OUT,
            target_client_id=client_id,
            target_session_id=session_id,
            error_code="approval_expired" if approval_timeout else "timed_out",
            occurred_at=at,
        )

    async def disconnect(
        self, *, client_id: str, session_id: str, at: str | None = None
    ) -> int:
        return await self.repository.disconnect_session(
            client_id=client_id, session_id=session_id, occurred_at=at
        )

    async def _require_call(self, tool_call_id: str) -> dict[str, Any]:
        call = await self.repository.get_tool_call(tool_call_id)
        if call is None:
            raise ToolCallNotFoundError(tool_call_id)
        return call

    async def _live_capability(
        self, call: dict[str, Any], *, at: str | None
    ) -> dict[str, Any] | None:
        return await self.repository.get_live_capability(
            client_id=str(call["target_client_id"]),
            session_id=str(call["target_session_id"]),
            tool_name=str(call["tool_name"]),
            tool_version=str(call["tool_version"]),
            at=at,
        )

    async def _require_live_capability(
        self, call: dict[str, Any], *, at: str | None
    ) -> dict[str, Any]:
        capability = await self._live_capability(call, at=at)
        if capability is not None:
            return capability
        status = str(call["status"])
        if status not in TERMINAL_STATUSES:
            await self.repository.transition(
                str(call["tool_call_id"]),
                CLIENT_DISCONNECTED,
                safe_summary="exact target session capability expired or disconnected",
                error_code="client_disconnected",
                occurred_at=at,
            )
        raise ToolCapabilityUnavailableError(
            "exact target session capability is unavailable; automatic reroute is forbidden"
        )

    @staticmethod
    def _assert_requested_target(
        call: dict[str, Any], client_id: str, session_id: str
    ) -> None:
        if (
            str(call["target_client_id"]) != client_id
            or str(call["target_session_id"]) != session_id
        ):
            raise ToolCapabilityUnavailableError(
                "requested client/session does not own this tool call"
            )


__all__ = [
    "APPROVED",
    "AWAITING_APPROVAL",
    "CANCELLED",
    "CLIENT_DISCONNECTED",
    "COMPLETED",
    "FAILED",
    "QUEUED",
    "RUNNING",
    "TIMED_OUT",
    "ToolCapabilityUnavailableError",
    "ToolOrchestrationLimits",
    "ToolOrchestrator",
    "ProposalOutcome",
]
