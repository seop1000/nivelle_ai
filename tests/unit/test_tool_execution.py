from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from nivelle_core.agent_gateway import AgentGateway, AgentSessionHandle
from nivelle_core.database import Database
from nivelle_core.llm import LLMToolProposal
from nivelle_core.repositories import ConversationRepository
from nivelle_core.tool_execution import execute_tool_proposals
from nivelle_core.tool_orchestrator import ToolOrchestrationLimits, ToolOrchestrator
from nivelle_core.tool_repository import ToolRepository
from nivelle_protocol.settings import AgentSettings
from nivelle_protocol.tools import (
    TOOL_REGISTRY,
    ApprovalMode,
    ClientCapabilities,
    GetSystemStatusResult,
    ToolApprovalDecision,
    ToolCapability,
    ToolEvent,
    ToolEventType,
    ToolPlatform,
    ToolRequest,
    ToolResult,
    ToolStatus,
)
from nivelle_protocol.version import APP_VERSION, PROTOCOL_VERSION


def _settings() -> AgentSettings:
    return AgentSettings(
        enabled=True,
        max_parallel_calls_per_client=2,
        max_calls_per_turn=3,
        approval_timeout_seconds=120,
        result_timeout_seconds=30,
        audit_retention_days=90,
        expose_debug_metadata=False,
    )


async def _turn(
    tmp_path: Path,
) -> tuple[
    ToolRepository,
    ToolOrchestrator,
    UUID,
    UUID,
    UUID,
    UUID,
]:
    database = Database(tmp_path / "nivelle.db")
    await database.initialize()
    conversations = ConversationRepository(database)
    conversation = await conversations.create("현재 PC 상태를 알려주세요.")
    request_id = uuid4()
    user, assistant = await conversations.allocate_turn(
        str(conversation["id"]),
        "현재 PC 상태를 알려주세요.",
        user_metadata={"request_id": str(request_id)},
        assistant_metadata={"request_id": str(request_id)},
        client_message_id=str(uuid4()),
        request_id=str(request_id),
    )
    repository = ToolRepository(database)
    return (
        repository,
        ToolOrchestrator(repository),
        UUID(str(conversation["id"])),
        UUID(str(user["id"])),
        UUID(str(assistant["id"])),
        request_id,
    )


def _capabilities(
    client_id: UUID,
    session_id: UUID,
    *,
    approval_mode: ApprovalMode,
) -> ClientCapabilities:
    definition = TOOL_REGISTRY.require("get_system_status")
    capability = ToolCapability.from_definition(definition, enabled=True).model_copy(
        update={"default_approval_mode": approval_mode}
    )
    return ClientCapabilities(
        client_id=client_id,
        session_id=session_id,
        platform=ToolPlatform.WINDOWS,
        app_version=APP_VERSION,
        protocol_version=PROTOCOL_VERSION,
        tools=[capability],
        advertised_at=datetime.now(UTC),
    )


def _started(request: ToolRequest) -> ToolEvent:
    return ToolEvent(
        type=ToolEventType.STARTED,
        tool_call_id=request.tool_call_id,
        request_id=request.request_id,
        target_client_id=request.target_client_id,
        target_session_id=request.target_session_id,
        status=ToolStatus.RUNNING,
        occurred_at=datetime.now(UTC),
    )


def _approved(request: ToolRequest) -> ToolEvent:
    return ToolEvent(
        type=ToolEventType.APPROVED,
        tool_call_id=request.tool_call_id,
        request_id=request.request_id,
        target_client_id=request.target_client_id,
        target_session_id=request.target_session_id,
        status=ToolStatus.APPROVED,
        occurred_at=datetime.now(UTC),
        approval=ToolApprovalDecision(
            approval_id=uuid4(),
            mode=ApprovalMode.ALLOW_ONCE,
            decided_at=datetime.now(UTC),
            policy_version="1",
            reason="Approved in the local Nivelle Link UI.",
        ),
    )


def _result(request: ToolRequest) -> ToolResult:
    started_at = datetime.now(UTC)
    payload = GetSystemStatusResult(
        operating_system="Windows 11",
        client_display_name="Nivelle Link test",
        cpu_usage_percent=12.5,
        ram_usage_percent=40.0,
        ram_total_bytes=16_000,
        ram_available_bytes=9_600,
        link_uptime_seconds=60,
        link_version=APP_VERSION,
    )
    return ToolResult(
        result_id=uuid4(),
        source_tool=request.tool_name,
        tool_call_id=request.tool_call_id,
        request_id=request.request_id,
        target_client_id=request.target_client_id,
        target_session_id=request.target_session_id,
        tool_name=request.tool_name,
        tool_version=request.tool_version,
        status=ToolStatus.COMPLETED,
        started_at=started_at,
        completed_at=started_at + timedelta(milliseconds=10),
        duration_ms=10,
        result=payload.model_dump(mode="json"),
        safe_summary="Safe system status returned.",
    )


async def _run_completed_flow(
    tmp_path: Path,
    *,
    approval_mode: ApprovalMode,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], ToolRepository]:
    (
        repository,
        orchestrator,
        conversation_id,
        user_message_id,
        assistant_message_id,
        request_id,
    ) = await _turn(tmp_path)
    gateway = AgentGateway(orchestrator)
    client_id, session_id = uuid4(), uuid4()
    capabilities = _capabilities(
        client_id,
        session_id,
        approval_mode=approval_mode,
    )
    sent: list[dict[str, Any]] = []
    handle: AgentSessionHandle | None = None

    async def respond(request: ToolRequest) -> None:
        assert handle is not None
        if approval_mode is not ApprovalMode.NOT_REQUIRED:
            await gateway.handle_event(handle, _approved(request))
        await gateway.handle_event(handle, _started(request))
        await gateway.handle_result(handle, _result(request))

    async def sender(payload: dict[str, Any]) -> None:
        sent.append(payload)
        request = ToolRequest.model_validate(payload)
        asyncio.create_task(respond(request))

    handle = await gateway.register(str(client_id), capabilities, sender)
    output = await execute_tool_proposals(
        [
            LLMToolProposal(
                tool_call_id="model-controlled-id-must-be-ignored",
                name="get_system_status",
                arguments={},
            )
        ],
        request_id=request_id,
        conversation_id=conversation_id,
        user_message_id=user_message_id,
        assistant_message_id=assistant_message_id,
        capabilities=capabilities,
        settings=_settings(),
        orchestrator=orchestrator,
        gateway=gateway,
    )
    return output, sent, repository


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "approval_mode",
    [ApprovalMode.NOT_REQUIRED, ApprovalMode.ALLOW_ONCE],
)
async def test_model_tool_proposal_executes_once_with_server_owned_identity(
    tmp_path: Path,
    approval_mode: ApprovalMode,
) -> None:
    output, sent, repository = await _run_completed_flow(
        tmp_path,
        approval_mode=approval_mode,
    )

    assert len(sent) == 1
    assert sent[0]["type"] == "tool.request"
    assert sent[0]["tool_call_id"] != "model-controlled-id-must-be-ignored"
    assert output[0]["status"] == "completed"
    assert output[0]["trusted"] is False
    call = await repository.get_tool_call(str(sent[0]["tool_call_id"]))
    assert call is not None
    assert call["status"] == "completed"
    events = await repository.list_events(str(sent[0]["tool_call_id"]))
    event_types = [event["event_type"] for event in events]
    assert event_types.count("tool.request") == 1
    assert event_types.count("tool.completed") == 1
    assert event_types.count("tool.approved") == (
        0 if approval_mode is ApprovalMode.NOT_REQUIRED else 1
    )


@pytest.mark.asyncio
async def test_malformed_and_unadvertised_proposals_never_reach_agent(
    tmp_path: Path,
) -> None:
    (
        repository,
        orchestrator,
        conversation_id,
        user_message_id,
        assistant_message_id,
        request_id,
    ) = await _turn(tmp_path)
    gateway = AgentGateway(orchestrator)
    client_id, session_id = uuid4(), uuid4()
    capabilities = _capabilities(
        client_id,
        session_id,
        approval_mode=ApprovalMode.NOT_REQUIRED,
    )
    sent: list[dict[str, Any]] = []

    async def sender(payload: dict[str, Any]) -> None:
        sent.append(payload)

    await gateway.register(str(client_id), capabilities, sender)
    output = await execute_tool_proposals(
        [
            LLMToolProposal("one", "get_system_status", {"unexpected": True}),
            LLMToolProposal("two", "run_powershell", {"command": "whoami"}),
        ],
        request_id=request_id,
        conversation_id=conversation_id,
        user_message_id=user_message_id,
        assistant_message_id=assistant_message_id,
        capabilities=capabilities,
        settings=_settings(),
        orchestrator=orchestrator,
        gateway=gateway,
    )

    assert sent == []
    assert [item["status"] for item in output] == [
        "validation_failed",
        "validation_failed",
    ]
    assert all(item["trusted"] is False for item in output)
    assert await repository.db.fetchone("SELECT 1 FROM tool_calls LIMIT 1") is None


@pytest.mark.asyncio
async def test_orchestration_limit_is_a_safe_result_not_a_broken_chat_turn(
    tmp_path: Path,
) -> None:
    (
        repository,
        _orchestrator,
        conversation_id,
        user_message_id,
        assistant_message_id,
        request_id,
    ) = await _turn(tmp_path)
    orchestrator = ToolOrchestrator(
        repository,
        ToolOrchestrationLimits(
            max_parallel_calls_per_client=2,
            max_calls_per_turn=1,
            idempotency_retention_days=90,
        ),
    )
    gateway = AgentGateway(orchestrator)
    client_id, session_id = uuid4(), uuid4()
    capabilities = _capabilities(
        client_id,
        session_id,
        approval_mode=ApprovalMode.NOT_REQUIRED,
    )
    handle: AgentSessionHandle | None = None

    async def sender(payload: dict[str, Any]) -> None:
        request = ToolRequest.model_validate(payload)

        async def respond() -> None:
            assert handle is not None
            await gateway.handle_event(handle, _started(request))
            await gateway.handle_result(handle, _result(request))

        asyncio.create_task(respond())

    handle = await gateway.register(str(client_id), capabilities, sender)
    proposals = [
        LLMToolProposal("ignored-one", "get_system_status", {}),
        LLMToolProposal("ignored-two", "get_system_status", {}),
    ]
    output = await execute_tool_proposals(
        proposals,
        request_id=request_id,
        conversation_id=conversation_id,
        user_message_id=user_message_id,
        assistant_message_id=assistant_message_id,
        capabilities=capabilities,
        settings=_settings(),
        orchestrator=orchestrator,
        gateway=gateway,
    )

    assert output[0]["status"] == "completed"
    assert output[1]["status"] == "failed"
    assert output[1]["trusted"] is False
