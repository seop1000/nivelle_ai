from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from nivelle_core.database import Database
from nivelle_core.repositories import ConversationRepository
from nivelle_core.tool_orchestrator import (
    ToolCapabilityUnavailableError,
    ToolOrchestrationLimits,
    ToolOrchestrator,
)
from nivelle_core.tool_repository import (
    CLIENT_DISCONNECTED,
    COMPLETED,
    FAILED,
    QUEUED,
    VALIDATED,
    VALIDATION_FAILED,
    ClientCapability,
    ConflictingToolResultError,
    ToolCallCreate,
    ToolLimitExceededError,
    ToolRepository,
)

T0 = "2026-08-04T00:00:00+00:00"
T1 = "2026-08-04T00:01:00+00:00"
T2 = "2026-08-04T00:02:00+00:00"
T3 = "2026-08-04T00:03:00+00:00"
T4 = "2026-08-04T00:04:00+00:00"


async def _orchestrator(
    tmp_path: Path, *, parallel: int = 2, per_turn: int = 3
) -> tuple[ToolOrchestrator, ToolRepository, str, str]:
    database = Database(tmp_path / "nivelle.db")
    await database.initialize()
    conversations = ConversationRepository(database)
    conversation = await conversations.create("도구 상태 전이 테스트")
    user = await conversations.add_message(
        str(conversation["id"]),
        "user",
        "현재 PC 상태를 알려주세요.",
        message_id="user-message-1",
        client_message_id="client-message-1",
        request_id="request-1",
    )
    repository = ToolRepository(database)
    orchestrator = ToolOrchestrator(
        repository,
        ToolOrchestrationLimits(
            max_parallel_calls_per_client=parallel,
            max_calls_per_turn=per_turn,
            idempotency_retention_days=90,
        ),
    )
    return orchestrator, repository, str(conversation["id"]), str(user["id"])


def _capability(*, session_id: str = "session-1", expires_at: str = T4) -> ClientCapability:
    return ClientCapability(
        client_id="client-1",
        session_id=session_id,
        platform="windows",
        app_version="0.4.0",
        protocol_version="1.0",
        tool_name="get_system_status",
        tool_version="1.0",
        enabled=True,
        implementation_available=True,
        risk_level="SAFE_STATUS",
        default_approval_required=False,
        default_timeout_ms=1_000,
        maximum_timeout_ms=5_000,
        maximum_result_size=10_000,
        expires_at=expires_at,
    )


def _call(
    conversation_id: str,
    user_message_id: str,
    suffix: str,
    *,
    request_id: str = "request-1",
    session_id: str = "session-1",
    approval_mode: str = "not_required",
) -> ToolCallCreate:
    return ToolCallCreate(
        tool_call_id=f"call-{suffix}",
        request_id=request_id,
        idempotency_key=f"key-{suffix}",
        conversation_id=conversation_id,
        user_message_id=user_message_id,
        assistant_message_id=None,
        target_client_id="client-1",
        target_session_id=session_id,
        tool_name="get_system_status",
        tool_version="1.0",
        risk_level="SAFE_STATUS",
        arguments_summary="CPU와 메모리 사용률",
        approval_mode=approval_mode,
    )


@pytest.mark.asyncio
async def test_no_approval_path_exact_target_and_duplicate_replay(tmp_path: Path) -> None:
    orchestrator, repository, conversation_id, user_message_id = await _orchestrator(
        tmp_path
    )
    await orchestrator.advertise_capabilities([_capability()], connected_at=T0)
    call = _call(conversation_id, user_message_id, "1")

    proposal = await orchestrator.propose(call, created_at=T0)
    replay = await orchestrator.propose(call, created_at=T1)
    assert proposal.replayed is False
    assert replay.replayed is True
    assert replay.call["tool_call_id"] == proposal.call["tool_call_id"]
    assert await orchestrator.validate(call.tool_call_id, at=T1)
    assert await orchestrator.queue(
        call.tool_call_id, client_id="client-1", session_id="session-1", at=T1
    )
    assert await orchestrator.start(
        call.tool_call_id, client_id="client-1", session_id="session-1", at=T2
    )
    assert await orchestrator.progress(
        call.tool_call_id,
        client_id="client-1",
        session_id="session-1",
        safe_summary="1/2 단계 token=hidden",
        at=T2,
    )
    assert await orchestrator.complete(
        call.tool_call_id,
        client_id="client-1",
        session_id="session-1",
        duration_ms=50,
        at=T3,
    )
    assert not await orchestrator.complete(
        call.tool_call_id,
        client_id="client-1",
        session_id="session-1",
        duration_ms=50,
        at=T3,
    )
    persisted = await repository.get_tool_call(call.tool_call_id)
    assert persisted is not None
    assert persisted["status"] == COMPLETED
    events = await repository.list_events(call.tool_call_id)
    event_types = [str(event["event_type"]) for event in events]
    assert event_types.count("tool.request") == 1
    assert event_types.count("tool.progress") == 1
    progress = next(event for event in events if event["event_type"] == "tool.progress")
    assert progress["safe_summary"] == "1/2 단계 token=[REDACTED]"


@pytest.mark.asyncio
async def test_approval_denial_wrong_target_and_failed_result_cannot_be_success(
    tmp_path: Path,
) -> None:
    orchestrator, repository, conversation_id, user_message_id = await _orchestrator(
        tmp_path
    )
    await orchestrator.advertise_capabilities([_capability()], connected_at=T0)
    denied = _call(
        conversation_id, user_message_id, "denied", approval_mode="allow_once"
    )
    await orchestrator.propose(denied, created_at=T0)
    assert await orchestrator.validate(denied.tool_call_id, at=T1)
    await orchestrator.require_approval(denied.tool_call_id, at=T1)
    with pytest.raises(ToolCapabilityUnavailableError):
        await orchestrator.queue(
            denied.tool_call_id,
            client_id="wrong-client",
            session_id="session-1",
            at=T1,
        )
    assert await orchestrator.deny(
        denied.tool_call_id,
        client_id="client-1",
        session_id="session-1",
        at=T1,
    )

    failed = _call(conversation_id, user_message_id, "failed", request_id="request-2")
    await orchestrator.propose(failed, created_at=T0)
    await orchestrator.validate(failed.tool_call_id, at=T1)
    await orchestrator.queue(
        failed.tool_call_id, client_id="client-1", session_id="session-1", at=T1
    )
    await orchestrator.start(
        failed.tool_call_id, client_id="client-1", session_id="session-1", at=T2
    )
    await orchestrator.fail(
        failed.tool_call_id,
        client_id="client-1",
        session_id="session-1",
        duration_ms=12,
        at=T3,
    )
    with pytest.raises(ConflictingToolResultError):
        await orchestrator.complete(
            failed.tool_call_id,
            client_id="client-1",
            session_id="session-1",
            duration_ms=13,
        )
    persisted = await repository.get_tool_call(failed.tool_call_id)
    assert persisted is not None
    assert persisted["status"] == FAILED


@pytest.mark.asyncio
async def test_capability_expiry_and_reconnect_never_reroute_to_new_session(
    tmp_path: Path,
) -> None:
    orchestrator, repository, conversation_id, user_message_id = await _orchestrator(
        tmp_path
    )
    await orchestrator.advertise_capabilities(
        [_capability(expires_at=T2)], connected_at=T0
    )
    expired = _call(conversation_id, user_message_id, "expired")
    await orchestrator.propose(expired, created_at=T0)
    assert not await orchestrator.validate(expired.tool_call_id, at=T3)
    persisted = await repository.get_tool_call(expired.tool_call_id)
    assert persisted is not None
    assert persisted["status"] == VALIDATION_FAILED

    old_session = _call(
        conversation_id,
        user_message_id,
        "old-session",
        request_id="request-2",
        session_id="session-1",
    )
    await orchestrator.propose(old_session, created_at=T0)
    await orchestrator.advertise_capabilities(
        [_capability(session_id="session-2")], connected_at=T1
    )
    assert not await orchestrator.validate(old_session.tool_call_id, at=T2)
    old = await repository.get_tool_call(old_session.tool_call_id)
    assert old is not None
    assert old["target_session_id"] == "session-1"
    assert old["status"] == VALIDATION_FAILED


@pytest.mark.asyncio
async def test_calls_per_turn_and_parallel_limits_are_enforced(tmp_path: Path) -> None:
    orchestrator, repository, conversation_id, user_message_id = await _orchestrator(
        tmp_path, parallel=1, per_turn=2
    )
    await orchestrator.advertise_capabilities([_capability()], connected_at=T0)
    first = _call(conversation_id, user_message_id, "1")
    second = _call(conversation_id, user_message_id, "2")
    third = _call(conversation_id, user_message_id, "3")
    await orchestrator.propose(first, created_at=T0)
    await orchestrator.propose(second, created_at=T0)
    with pytest.raises(ToolLimitExceededError):
        await orchestrator.propose(third, created_at=T0)

    await orchestrator.validate(first.tool_call_id, at=T1)
    await orchestrator.validate(second.tool_call_id, at=T1)
    await orchestrator.queue(
        first.tool_call_id, client_id="client-1", session_id="session-1", at=T1
    )
    with pytest.raises(ToolLimitExceededError):
        await orchestrator.queue(
            second.tool_call_id,
            client_id="client-1",
            session_id="session-1",
            at=T1,
        )
    first_row = await repository.get_tool_call(first.tool_call_id)
    second_row = await repository.get_tool_call(second.tool_call_id)
    assert first_row is not None and first_row["status"] == QUEUED
    assert second_row is not None and second_row["status"] == VALIDATED


@pytest.mark.asyncio
async def test_disconnect_marks_every_nonterminal_call_for_only_that_session(
    tmp_path: Path,
) -> None:
    orchestrator, repository, conversation_id, user_message_id = await _orchestrator(
        tmp_path
    )
    await orchestrator.advertise_capabilities([_capability()], connected_at=T0)
    first = _call(conversation_id, user_message_id, "first")
    other = _call(
        conversation_id,
        user_message_id,
        "other",
        request_id="request-2",
        session_id="session-2",
    )
    await orchestrator.propose(first, created_at=T0)
    await orchestrator.propose(other, created_at=T0)
    await orchestrator.validate(first.tool_call_id, at=T1)
    assert await orchestrator.disconnect(
        client_id="client-1", session_id="session-1", at=T2
    ) == 1

    first_row = await repository.get_tool_call(first.tool_call_id)
    other_row = await repository.get_tool_call(other.tool_call_id)
    assert first_row is not None and first_row["status"] == CLIENT_DISCONNECTED
    assert other_row is not None and other_row["status"] == "proposed"


def test_orchestration_limits_reject_zero() -> None:
    with pytest.raises(ValueError):
        ToolOrchestrationLimits(max_parallel_calls_per_client=0)
    with pytest.raises(ValueError):
        ToolOrchestrationLimits(max_calls_per_turn=0)
    with pytest.raises(ValueError):
        ToolOrchestrationLimits(idempotency_retention_days=0)


def test_call_fixture_can_change_without_mutating_identity() -> None:
    """Keep dataclass replacement usable for controlled explicit retry tests."""

    call = _call("conversation", "user", "base")
    retry = replace(
        call,
        tool_call_id="call-retry",
        idempotency_key="key-retry",
        request_id="request-retry",
    )
    assert retry.tool_call_id != call.tool_call_id
    assert call.tool_call_id == "call-base"
