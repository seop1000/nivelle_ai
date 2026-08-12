from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from nivelle_core.agent_gateway import (
    AgentAuthenticationMismatchError,
    AgentConflictingTerminalError,
    AgentCorrelationError,
    AgentGateway,
    AgentGatewayError,
    AgentRemoteTerminalError,
    AgentResultTimeoutError,
    AgentSessionConflictError,
    AgentSessionDisconnectedError,
    AgentSessionHandle,
)
from nivelle_core.database import Database
from nivelle_core.repositories import ConversationRepository
from nivelle_core.tool_orchestrator import ToolOrchestrator
from nivelle_core.tool_repository import (
    AWAITING_APPROVAL,
    CLIENT_DISCONNECTED,
    COMPLETED,
    DENIED,
    QUEUED,
    TIMED_OUT,
    ToolCallCreate,
    ToolRepository,
)
from nivelle_protocol.tools import (
    TOOL_REGISTRY,
    ApprovalMode,
    ClientCapabilities,
    GetSystemStatusResult,
    ReadTextFileResult,
    ToolApprovalDecision,
    ToolCapability,
    ToolErrorCode,
    ToolEvent,
    ToolEventType,
    ToolPlatform,
    ToolProgress,
    ToolRequest,
    ToolResult,
    ToolStatus,
)
from nivelle_protocol.version import APP_VERSION


@dataclass(slots=True)
class GatewayCase:
    gateway: AgentGateway
    orchestrator: ToolOrchestrator
    repository: ToolRepository
    handle: AgentSessionHandle
    request: ToolRequest
    sent: list[dict[str, Any]]


def _capabilities(
    client_id: str,
    session_id: str,
    tool_name: str,
    *,
    approval_mode: ApprovalMode | None = None,
) -> ClientCapabilities:
    definition = TOOL_REGISTRY.require(tool_name)
    capability = ToolCapability.from_definition(definition, enabled=True)
    if approval_mode is not None:
        capability = capability.model_copy(
            update={"default_approval_mode": approval_mode},
        )
    return ClientCapabilities(
        client_id=client_id,
        session_id=session_id,
        platform=ToolPlatform.WINDOWS,
        app_version=APP_VERSION,
        tools=[capability],
        advertised_at=datetime.now(UTC),
    )


def _arguments(tool_name: str) -> dict[str, Any]:
    if tool_name == "get_system_status":
        return {}
    if tool_name == "read_text_file":
        return {"path_ref": "project:readme", "start_line": 1, "max_lines": 20}
    raise AssertionError(f"test helper does not define {tool_name}")


async def _case(
    tmp_path: Path,
    *,
    tool_name: str = "get_system_status",
    approval_mode: str | None = None,
    timeout_ms: int | None = None,
    approval_timeout_seconds: float = 120,
    sender: Any | None = None,
) -> GatewayCase:
    database = Database(tmp_path / f"{uuid4()}.db")
    await database.initialize()
    conversations = ConversationRepository(database)
    conversation = await conversations.create("Agent gateway test")
    request_id = str(uuid4())
    user = await conversations.add_message(
        str(conversation["id"]),
        "user",
        "Run one safe local tool",
        message_id=str(uuid4()),
        client_message_id=str(uuid4()),
        request_id=request_id,
    )
    repository = ToolRepository(database)
    orchestrator = ToolOrchestrator(repository)
    gateway = AgentGateway(
        orchestrator,
        capability_ttl_seconds=60,
        approval_timeout_seconds=approval_timeout_seconds,
    )
    client_id = str(uuid4())
    session_id = str(uuid4())
    sent: list[dict[str, Any]] = []

    async def collect(payload: dict[str, Any]) -> None:
        sent.append(payload)

    definition = TOOL_REGISTRY.require(tool_name)
    effective_approval_mode = approval_mode or definition.default_approval_mode.value
    handle = await gateway.register(
        client_id,
        _capabilities(
            client_id,
            session_id,
            tool_name,
            approval_mode=ApprovalMode(effective_approval_mode),
        ),
        sender or collect,
    )
    request = ToolRequest(
        tool_call_id=uuid4(),
        request_id=request_id,
        idempotency_key=uuid4(),
        conversation_id=str(conversation["id"]),
        user_message_id=str(user["id"]),
        target_client_id=client_id,
        target_session_id=session_id,
        tool_name=tool_name,
        tool_version=definition.version,
        arguments=_arguments(tool_name),
        risk_level=definition.risk_level,
        created_at=datetime.now(UTC),
        timeout_ms=timeout_ms or definition.default_timeout_ms,
        user_intent_summary="The user requested one bounded local action.",
    )
    proposal = await orchestrator.propose(
        ToolCallCreate(
            tool_call_id=str(request.tool_call_id),
            request_id=str(request.request_id),
            idempotency_key=str(request.idempotency_key),
            conversation_id=str(request.conversation_id),
            user_message_id=str(request.user_message_id),
            assistant_message_id=None,
            target_client_id=client_id,
            target_session_id=session_id,
            tool_name=tool_name,
            tool_version=definition.version,
            risk_level=definition.risk_level.value,
            arguments_summary="bounded test arguments",
            approval_mode=effective_approval_mode,
        )
    )
    assert proposal.replayed is False
    assert await orchestrator.validate(str(request.tool_call_id))
    if effective_approval_mode != "not_required":
        assert await orchestrator.require_approval(str(request.tool_call_id))
    return GatewayCase(gateway, orchestrator, repository, handle, request, sent)


def _status_result(request: ToolRequest, *, result_id: str | None = None) -> ToolResult:
    result = GetSystemStatusResult(
        operating_system="Windows 11",
        client_display_name="Desktop",
        cpu_usage_percent=12.5,
        ram_usage_percent=40,
        ram_total_bytes=16_000,
        ram_available_bytes=9_600,
        link_uptime_seconds=100,
        link_version=APP_VERSION,
    )
    started = datetime.now(UTC)
    return ToolResult(
        result_id=result_id or str(uuid4()),
        source_tool=request.tool_name,
        tool_call_id=request.tool_call_id,
        request_id=request.request_id,
        target_client_id=request.target_client_id,
        target_session_id=request.target_session_id,
        tool_name=request.tool_name,
        tool_version=request.tool_version,
        status=ToolStatus.COMPLETED,
        started_at=started,
        completed_at=started + timedelta(milliseconds=20),
        duration_ms=20,
        result=result.model_dump(mode="json"),
        safe_summary="Safe system status returned.",
    )


def _approval_event(request: ToolRequest, *, approved: bool) -> ToolEvent:
    now = datetime.now(UTC)
    decision = ToolApprovalDecision(
        approval_id=uuid4(),
        mode=ApprovalMode.ALLOW_ONCE if approved else ApprovalMode.DENY,
        decided_at=now,
        policy_version="1",
    )
    return ToolEvent(
        type=ToolEventType.APPROVED if approved else ToolEventType.DENIED,
        tool_call_id=request.tool_call_id,
        request_id=request.request_id,
        target_client_id=request.target_client_id,
        target_session_id=request.target_session_id,
        status=ToolStatus.APPROVED if approved else ToolStatus.DENIED,
        occurred_at=now,
        approval=decision,
        error_code=None if approved else ToolErrorCode.APPROVAL_DENIED,
        error_message=None if approved else "The user denied this local action.",
    )


@pytest.mark.asyncio
async def test_register_strict_capabilities_auth_binding_refresh_and_snapshot(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "register.db")
    await database.initialize()
    gateway = AgentGateway(ToolOrchestrator(ToolRepository(database)))
    client_id = str(uuid4())
    session_id = str(uuid4())
    sent: list[dict[str, Any]] = []

    async def sender(payload: dict[str, Any]) -> None:
        sent.append(payload)

    advertisement = _capabilities(client_id, session_id, "get_system_status")
    wrong = advertisement.model_copy(update={"client_id": uuid4()})
    with pytest.raises(AgentAuthenticationMismatchError):
        await gateway.register(client_id, wrong, sender)
    with pytest.raises(AgentGatewayError):
        await gateway.register(
            client_id,
            advertisement.model_dump(mode="json") | {"authorization": "secret"},
            sender,
        )

    handle = await gateway.register(client_id, advertisement, sender)
    refreshed = advertisement.model_copy(update={"advertised_at": datetime.now(UTC)})
    assert await gateway.refresh_capabilities(handle, refreshed) == refreshed
    snapshot = await gateway.snapshot()
    assert len(snapshot.sessions) == 1
    assert snapshot.sessions[0].enabled_tools == ("get_system_status",)
    assert snapshot.pending_calls == ()
    assert "secret" not in snapshot.model_dump_json()

    active = await gateway.active_capabilities(client_id)
    assert active == refreshed
    assert active is not refreshed
    assert active is not None and active.tools is not refreshed.tools
    assert await gateway.active_capabilities(client_id, session_id) == refreshed
    assert await gateway.active_capabilities(client_id, str(uuid4())) is None
    assert await gateway.active_capabilities(str(uuid4())) is None

    await gateway.disconnect(handle)
    assert await gateway.active_capabilities(client_id) is None
    with pytest.raises(AgentSessionConflictError):
        await gateway.register(client_id, advertisement, sender)


@pytest.mark.asyncio
async def test_dispatch_result_is_exactly_correlated_and_terminal_deduplicated(
    tmp_path: Path,
) -> None:
    case = await _case(tmp_path)
    future = await case.gateway.dispatch(case.request)
    assert len(case.sent) == 1
    assert case.sent[0]["type"] == "tool.request"
    assert case.sent[0]["tool_call_id"] == str(case.request.tool_call_id)

    result = _status_result(case.request)
    assert await case.gateway.handle_result(case.handle, result)
    assert await future == result
    persisted = await case.repository.get_tool_call(str(case.request.tool_call_id))
    assert persisted is not None
    assert persisted["status"] == COMPLETED

    assert not await case.gateway.handle_result(case.handle, result)
    with pytest.raises(AgentConflictingTerminalError):
        await case.gateway.handle_result(
            case.handle,
            result.model_copy(update={"result_id": uuid4()}),
        )
    events = await case.repository.list_events(str(case.request.tool_call_id))
    assert [event["event_type"] for event in events].count("tool.completed") == 1


@pytest.mark.asyncio
async def test_other_socket_and_mismatched_request_cannot_submit_result(
    tmp_path: Path,
) -> None:
    case = await _case(tmp_path)
    future = await case.gateway.dispatch(case.request)
    other_client = str(uuid4())
    other_session = str(uuid4())

    async def sender(_: dict[str, Any]) -> None:
        return None

    other_handle = await case.gateway.register(
        other_client,
        _capabilities(other_client, other_session, "get_system_status"),
        sender,
    )
    result = _status_result(case.request)
    with pytest.raises(AgentCorrelationError):
        await case.gateway.handle_result(other_handle, result)
    with pytest.raises(AgentCorrelationError):
        await case.gateway.handle_result(
            case.handle,
            result.model_copy(update={"request_id": uuid4()}),
        )
    assert not future.done()

    await case.gateway.disconnect(case.handle)
    with pytest.raises(AgentSessionDisconnectedError):
        await future
    await case.gateway.disconnect(other_handle)


@pytest.mark.asyncio
async def test_started_progress_and_approval_events_drive_orchestrator(
    tmp_path: Path,
) -> None:
    case = await _case(tmp_path, approval_mode="allow_once")
    before = await case.repository.get_tool_call(str(case.request.tool_call_id))
    assert before is not None and before["status"] == AWAITING_APPROVAL

    future = await case.gateway.dispatch(case.request)
    assert len(case.sent) == 1
    waiting = await case.repository.get_tool_call(str(case.request.tool_call_id))
    assert waiting is not None and waiting["status"] == AWAITING_APPROVAL

    now = datetime.now(UTC)
    approved = _approval_event(case.request, approved=True)
    assert await case.gateway.handle_event(case.handle, approved)
    assert not await case.gateway.handle_event(case.handle, approved)
    queued = await case.repository.get_tool_call(str(case.request.tool_call_id))
    assert queued is not None and queued["status"] == QUEUED

    started = ToolEvent(
        type=ToolEventType.STARTED,
        tool_call_id=case.request.tool_call_id,
        request_id=case.request.request_id,
        target_client_id=case.request.target_client_id,
        target_session_id=case.request.target_session_id,
        status=ToolStatus.RUNNING,
        occurred_at=now,
    )
    assert await case.gateway.handle_event(case.handle, started)
    assert not await case.gateway.handle_event(case.handle, started)

    progress = ToolEvent(
        type=ToolEventType.PROGRESS,
        tool_call_id=case.request.tool_call_id,
        request_id=case.request.request_id,
        target_client_id=case.request.target_client_id,
        target_session_id=case.request.target_session_id,
        status=ToolStatus.RUNNING,
        occurred_at=now,
        progress=ToolProgress(sequence=1, completed_units=2, total_units=5),
    )
    assert await case.gateway.handle_event(case.handle, progress)
    assert not await case.gateway.handle_event(case.handle, progress)
    await case.gateway.handle_result(case.handle, _status_result(case.request))
    assert (await future).status is ToolStatus.COMPLETED

    events = await case.repository.list_events(str(case.request.tool_call_id))
    event_types = [str(event["event_type"]) for event in events]
    assert event_types.count("tool.approved") == 1
    assert event_types.count("tool.started") == 1
    assert event_types.count("tool.progress") == 1


@pytest.mark.asyncio
async def test_denial_finishes_approval_waiter_exactly_once(tmp_path: Path) -> None:
    case = await _case(tmp_path, approval_mode="allow_once")
    future = await case.gateway.dispatch(case.request)
    denied = _approval_event(case.request, approved=False)

    assert await case.gateway.handle_event(case.handle, denied)
    with pytest.raises(AgentRemoteTerminalError) as failure:
        await future
    assert failure.value.status is ToolStatus.DENIED
    assert failure.value.error_code is ToolErrorCode.APPROVAL_DENIED
    persisted = await case.repository.get_tool_call(str(case.request.tool_call_id))
    assert persisted is not None and persisted["status"] == DENIED
    assert (await case.gateway.snapshot()).pending_calls == ()
    assert not await case.gateway.handle_event(case.handle, denied)


@pytest.mark.asyncio
async def test_validation_failure_during_approval_reconciles_as_denied(
    tmp_path: Path,
) -> None:
    case = await _case(tmp_path, approval_mode="allow_once")
    future = await case.gateway.dispatch(case.request)

    for event_type, status in (
        (ToolEventType.APPROVAL_REQUIRED, ToolStatus.AWAITING_APPROVAL),
        (ToolEventType.QUEUED, ToolStatus.QUEUED),
    ):
        server_only = ToolEvent(
            type=event_type,
            tool_call_id=case.request.tool_call_id,
            request_id=case.request.request_id,
            target_client_id=case.request.target_client_id,
            target_session_id=case.request.target_session_id,
            status=status,
            occurred_at=datetime.now(UTC),
        )
        with pytest.raises(AgentCorrelationError, match="cannot originate"):
            await case.gateway.handle_event(case.handle, server_only)
    assert not future.done()

    validation_failed = ToolEvent(
        type=ToolEventType.VALIDATION_FAILED,
        tool_call_id=case.request.tool_call_id,
        request_id=case.request.request_id,
        target_client_id=case.request.target_client_id,
        target_session_id=case.request.target_session_id,
        status=ToolStatus.VALIDATION_FAILED,
        occurred_at=datetime.now(UTC),
        error_code=ToolErrorCode.TOOL_DISABLED,
        error_message="The tool was disabled while approval was open.",
    )
    assert await case.gateway.handle_event(case.handle, validation_failed)
    with pytest.raises(AgentRemoteTerminalError) as failure:
        await future
    assert failure.value.status is ToolStatus.VALIDATION_FAILED
    assert failure.value.error_code is ToolErrorCode.TOOL_DISABLED
    persisted = await case.repository.get_tool_call(str(case.request.tool_call_id))
    assert persisted is not None and persisted["status"] == DENIED
    assert (await case.gateway.snapshot()).pending_calls == ()


@pytest.mark.asyncio
async def test_approval_timeout_is_separate_from_execution_timeout(tmp_path: Path) -> None:
    case = await _case(
        tmp_path,
        approval_mode="allow_once",
        timeout_ms=5_000,
        approval_timeout_seconds=0.05,
    )
    future = await case.gateway.dispatch(case.request)

    with pytest.raises(AgentResultTimeoutError, match="approval"):
        await asyncio.wait_for(future, timeout=1)
    persisted = await case.repository.get_tool_call(str(case.request.tool_call_id))
    assert persisted is not None and persisted["status"] == TIMED_OUT
    assert persisted["error_code"] == ToolErrorCode.APPROVAL_EXPIRED.value
    assert (await case.gateway.snapshot()).pending_calls == ()


@pytest.mark.asyncio
async def test_result_timeout_marks_durable_state_without_replay(tmp_path: Path) -> None:
    case = await _case(tmp_path, timeout_ms=100)
    future = await case.gateway.dispatch(case.request)

    with pytest.raises(AgentResultTimeoutError, match="uncertain"):
        await asyncio.wait_for(future, timeout=1)
    persisted = await case.repository.get_tool_call(str(case.request.tool_call_id))
    assert persisted is not None
    assert persisted["status"] == TIMED_OUT
    snapshot = await case.gateway.snapshot()
    assert snapshot.pending_calls == ()
    assert snapshot.terminal_dedupe_count == 1


@pytest.mark.asyncio
async def test_disconnect_expires_capability_calls_and_waiters(tmp_path: Path) -> None:
    case = await _case(tmp_path)
    future = await case.gateway.dispatch(case.request)

    assert await case.gateway.disconnect(case.handle) == 1
    with pytest.raises(AgentSessionDisconnectedError, match="not assumed successful"):
        await future
    persisted = await case.repository.get_tool_call(str(case.request.tool_call_id))
    assert persisted is not None
    assert persisted["status"] == CLIENT_DISCONNECTED
    assert (
        await case.repository.get_live_capability(
            client_id=case.handle.client_id,
            session_id=case.handle.session_id,
            tool_name=case.request.tool_name,
            tool_version=case.request.tool_version,
        )
        is None
    )
    assert await case.gateway.disconnect(case.handle) == 0
    assert (await case.gateway.snapshot()).sessions == ()


@pytest.mark.asyncio
async def test_session_send_lock_serializes_concurrent_requests(tmp_path: Path) -> None:
    active = 0
    maximum_active = 0
    sent: list[dict[str, Any]] = []

    async def sender(payload: dict[str, Any]) -> None:
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.02)
        sent.append(payload)
        active -= 1

    case = await _case(tmp_path, sender=sender)

    # Add a second durable call for the same conversation/turn and target.
    second = case.request.model_copy(update={"tool_call_id": uuid4(), "idempotency_key": uuid4()})
    definition = TOOL_REGISTRY.require(second.tool_name)
    await case.orchestrator.propose(
        ToolCallCreate(
            tool_call_id=str(second.tool_call_id),
            request_id=str(second.request_id),
            idempotency_key=str(second.idempotency_key),
            conversation_id=str(second.conversation_id),
            user_message_id=str(second.user_message_id),
            assistant_message_id=None,
            target_client_id=str(second.target_client_id),
            target_session_id=str(second.target_session_id),
            tool_name=second.tool_name,
            tool_version=second.tool_version,
            risk_level=definition.risk_level.value,
            arguments_summary="second bounded request",
            approval_mode="not_required",
        )
    )
    assert await case.orchestrator.validate(str(second.tool_call_id))

    first_future, second_future = await asyncio.gather(
        case.gateway.dispatch(case.request), case.gateway.dispatch(second)
    )
    assert maximum_active == 1
    assert len(sent) == 2
    await case.gateway.handle_result(case.handle, _status_result(case.request))
    await case.gateway.handle_result(case.handle, _status_result(second))
    assert (await first_future).status is ToolStatus.COMPLETED
    assert (await second_future).status is ToolStatus.COMPLETED


@pytest.mark.asyncio
async def test_raw_file_content_is_returned_but_never_persisted_or_snapshotted(
    tmp_path: Path,
) -> None:
    case = await _case(tmp_path, tool_name="read_text_file")
    future = await case.gateway.dispatch(case.request)
    assert await case.gateway.handle_event(
        case.handle,
        _approval_event(case.request, approved=True),
    )
    raw_content = "password=never-store-this\nnormal text"
    typed = ReadTextFileResult(
        result_id=uuid4(),
        path_ref="project:readme",
        content=raw_content,
        encoding="utf-8",
        encoding_uncertain=False,
        start_line=1,
        returned_lines=2,
        has_more=False,
        truncated=False,
        original_size=len(raw_content.encode("utf-8")),
        returned_size=len(raw_content.encode("utf-8")),
        omitted_count=0,
    )
    now = datetime.now(UTC)
    result = ToolResult(
        result_id=uuid4(),
        source_tool="read_text_file",
        tool_call_id=case.request.tool_call_id,
        request_id=case.request.request_id,
        target_client_id=case.request.target_client_id,
        target_session_id=case.request.target_session_id,
        tool_name="read_text_file",
        tool_version=case.request.tool_version,
        status=ToolStatus.COMPLETED,
        started_at=now,
        completed_at=now + timedelta(milliseconds=10),
        duration_ms=10,
        result=typed.model_dump(mode="json"),
        safe_summary="token=never-store-this",
        original_size=typed.original_size,
        returned_size=typed.returned_size,
    )

    assert await case.gateway.handle_result(case.handle, result)
    delivered = await future
    assert delivered.result is not None
    assert delivered.result["content"] == raw_content
    persisted = await case.repository.get_tool_call(str(case.request.tool_call_id))
    events = await case.repository.list_events(str(case.request.tool_call_id))
    durable_text = (
        repr(persisted) + repr(events) + (await case.gateway.snapshot()).model_dump_json()
    )
    assert raw_content not in durable_text
    assert "never-store-this" not in durable_text
    assert persisted is not None
    assert persisted["result_summary"] == (
        "tool=read_text_file; status=completed; error_code=none"
    )
    assert all(raw_content not in str(event["safe_summary"]) for event in events)


@pytest.mark.asyncio
async def test_failed_result_never_becomes_success(tmp_path: Path) -> None:
    case = await _case(tmp_path)
    future = await case.gateway.dispatch(case.request)
    now = datetime.now(UTC)
    failed = ToolResult(
        result_id=uuid4(),
        source_tool=case.request.tool_name,
        tool_call_id=case.request.tool_call_id,
        request_id=case.request.request_id,
        target_client_id=case.request.target_client_id,
        target_session_id=case.request.target_session_id,
        tool_name=case.request.tool_name,
        tool_version=case.request.tool_version,
        status=ToolStatus.FAILED,
        started_at=now,
        completed_at=now + timedelta(milliseconds=10),
        duration_ms=10,
        result=None,
        safe_summary="The tool failed safely.",
        error_code=ToolErrorCode.EXECUTION_FAILED,
        error_message="The tool failed safely.",
        retryable=False,
    )

    await case.gateway.handle_result(case.handle, failed)
    assert (await future).status is ToolStatus.FAILED
    persisted = await case.repository.get_tool_call(str(case.request.tool_call_id))
    assert persisted is not None
    assert persisted["status"] != COMPLETED


@pytest.mark.asyncio
async def test_failure_event_stops_waiting_without_persisting_remote_text(
    tmp_path: Path,
) -> None:
    case = await _case(tmp_path)
    future = await case.gateway.dispatch(case.request)
    failed = ToolEvent(
        type=ToolEventType.FAILED,
        tool_call_id=case.request.tool_call_id,
        request_id=case.request.request_id,
        target_client_id=case.request.target_client_id,
        target_session_id=case.request.target_session_id,
        status=ToolStatus.FAILED,
        occurred_at=datetime.now(UTC),
        error_code=ToolErrorCode.EXECUTION_FAILED,
        error_message="password=must-not-enter-audit",
    )

    assert await case.gateway.handle_event(case.handle, failed)
    with pytest.raises(AgentRemoteTerminalError):
        await future
    persisted = await case.repository.get_tool_call(str(case.request.tool_call_id))
    assert persisted is not None
    assert persisted["status"] != COMPLETED
    assert "must-not-enter-audit" not in repr(persisted)

    # A subsequent same-status ToolResult is a duplicate terminal signal, not
    # a second transition or assistant completion.
    now = datetime.now(UTC)
    late = ToolResult(
        result_id=uuid4(),
        source_tool=case.request.tool_name,
        tool_call_id=case.request.tool_call_id,
        request_id=case.request.request_id,
        target_client_id=case.request.target_client_id,
        target_session_id=case.request.target_session_id,
        tool_name=case.request.tool_name,
        tool_version=case.request.tool_version,
        status=ToolStatus.FAILED,
        started_at=now,
        completed_at=now,
        duration_ms=0,
        result=None,
        safe_summary="late duplicate",
        error_code=ToolErrorCode.EXECUTION_FAILED,
        error_message="late duplicate",
    )
    assert not await case.gateway.handle_result(case.handle, late)
