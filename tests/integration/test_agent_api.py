from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import uvicorn
import websockets
from fastapi import FastAPI
from fastapi.testclient import TestClient
from nivelle_core.app import create_app
from nivelle_core.llm import LLMToolProposal, PromptMessage
from nivelle_protocol.tools import (
    TOOL_REGISTRY,
    ApprovalMode,
    ClientCapabilities,
    CreateNoteResult,
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
from nivelle_protocol.version import APP_VERSION
from starlette.websockets import WebSocketDisconnect


def _pair(client: TestClient, app: FastAPI) -> tuple[dict[str, str], str]:
    response = client.post(
        "/api/v1/pairing/complete",
        json={
            "code": app.state.services.pairing.code,
            "device_name": "phase3-agent-api-test",
        },
    )
    assert response.status_code == 200
    body = response.json()
    return {"Authorization": f"Bearer {body['token']}"}, str(body["client_id"])


def _capabilities(
    client_id: str,
    session_id: str,
    *,
    tool_name: str = "get_system_status",
) -> dict[str, Any]:
    definition = TOOL_REGISTRY.require(tool_name)
    capabilities = ClientCapabilities(
        client_id=UUID(client_id),
        session_id=UUID(session_id),
        platform=ToolPlatform.WINDOWS,
        app_version=APP_VERSION,
        tools=[ToolCapability.from_definition(definition, enabled=True)],
        advertised_at=datetime.now(UTC),
    )
    return capabilities.model_dump(mode="json")


def _agent_clients(
    client: TestClient, headers: dict[str, str]
) -> list[dict[str, Any]]:
    response = client.get("/api/v1/status", headers=headers)
    assert response.status_code == 200
    return list(response.json()["agent"]["clients"])


def _poll_agent_clients(
    client: TestClient,
    headers: dict[str, str],
    predicate: Any,
    *,
    attempts: int = 100,
) -> list[dict[str, Any]]:
    """Yield the TestClient portal without timing-based sleeps."""

    latest: list[dict[str, Any]] = []
    for _ in range(attempts):
        latest = _agent_clients(client, headers)
        if predicate(latest):
            return latest
    pytest.fail(f"Agent status did not converge; latest clients={latest!r}")


def _chat_request(content: str = "현재 이 PC의 상태를 확인해 주세요.") -> dict[str, Any]:
    return {
        "type": "chat.request",
        "protocol_version": "1.0",
        "request_id": str(uuid4()),
        "client_message_id": str(uuid4()),
        "content": content,
    }


class _ToolUsingProvider:
    def __init__(self, tool_name: str, arguments: dict[str, Any]) -> None:
        self.tool_name = tool_name
        self.arguments = arguments
        self.planning_messages: list[PromptMessage] = []
        self.tool_definitions: list[Mapping[str, object]] = []
        self.final_messages: list[PromptMessage] = []

    async def plan_tools(
        self,
        messages: Sequence[PromptMessage],
        tools: Sequence[Mapping[str, object]],
        *,
        max_calls: int,
    ) -> list[LLMToolProposal]:
        self.planning_messages = list(messages)
        self.tool_definitions = list(tools)
        assert max_calls >= 1
        return [
            LLMToolProposal(
                tool_call_id="model-supplied-id-is-not-authoritative",
                name=self.tool_name,
                arguments=self.arguments,
            )
        ]

    async def stream(
        self, messages: Sequence[PromptMessage]
    ) -> AsyncIterator[str]:
        self.final_messages = list(messages)
        yield "현재 PC 상태를 확인했습니다."


def _started_event(request: ToolRequest) -> dict[str, Any]:
    return ToolEvent(
        type=ToolEventType.STARTED,
        tool_call_id=request.tool_call_id,
        request_id=request.request_id,
        target_client_id=request.target_client_id,
        target_session_id=request.target_session_id,
        status=ToolStatus.RUNNING,
        occurred_at=datetime.now(UTC),
    ).model_dump(mode="json")


def _completed_result(request: ToolRequest) -> dict[str, Any]:
    started_at = datetime.now(UTC)
    if request.tool_name == "get_system_status":
        result: GetSystemStatusResult | CreateNoteResult = GetSystemStatusResult(
            operating_system="Windows 11",
            client_display_name="Nivelle Link integration test",
            cpu_usage_percent=12.5,
            ram_usage_percent=40.0,
            ram_total_bytes=16_000,
            ram_available_bytes=9_600,
            link_uptime_seconds=60,
            link_version=APP_VERSION,
        )
    elif request.tool_name == "create_note":
        # This is a simulated Agent response.  The test never writes a note.
        result = CreateNoteResult(
            note_id=uuid4(),
            title="통합 테스트",
            format="txt",
            path_ref="notes:simulated-note",
            safe_path_summary="Nivelle managed notes folder",
            size_bytes=12,
        )
    else:  # pragma: no cover - guarded by the scenario table below
        raise AssertionError(f"unsupported test tool: {request.tool_name}")
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
        result=result.model_dump(mode="json"),
        safe_summary=f"Nivelle Link returned bounded {request.tool_name} data.",
    ).model_dump(mode="json")


def _approved_event(request: ToolRequest) -> dict[str, Any]:
    now = datetime.now(UTC)
    return ToolEvent(
        type=ToolEventType.APPROVED,
        tool_call_id=request.tool_call_id,
        request_id=request.request_id,
        target_client_id=request.target_client_id,
        target_session_id=request.target_session_id,
        status=ToolStatus.APPROVED,
        occurred_at=now,
        approval=ToolApprovalDecision(
            approval_id=uuid4(),
            mode=ApprovalMode.ALLOW_ONCE,
            decided_at=now,
            policy_version="integration-test-v1",
        ),
    ).model_dump(mode="json")


def test_agent_registration_is_visible_only_while_socket_is_live(
    tmp_path: Path,
) -> None:
    app = create_app(tmp_path)
    session_id = str(uuid4())

    with TestClient(app) as client:
        headers, client_id = _pair(client, app)
        with client.websocket_connect("/ws/v1/agent", headers=headers) as agent:
            agent.send_json(_capabilities(client_id, session_id))
            clients = _poll_agent_clients(
                client,
                headers,
                lambda items: any(item["session_id"] == session_id for item in items),
            )

            assert clients == [
                {
                    "client_id": client_id,
                    "session_id": session_id,
                    "tool_count": 1,
                    "enabled_tools": ["get_system_status"],
                    "protocol_status": "compatible",
                    "app_version": APP_VERSION,
                    "platform": "windows",
                }
            ]
            status = client.get("/api/v1/status", headers=headers).json()
            assert status["agent"]["selected_target_client"] == client_id

        assert _poll_agent_clients(client, headers, lambda items: not items) == []


def test_agent_channel_rejects_invalid_authentication_and_capabilities(
    tmp_path: Path,
) -> None:
    app = create_app(tmp_path)

    with TestClient(app) as client:
        headers, client_id = _pair(client, app)

        with pytest.raises(WebSocketDisconnect) as unauthorized:
            with client.websocket_connect(
                "/ws/v1/agent",
                headers={"Authorization": "Bearer invalid-token"},
            ):
                pass
        assert unauthorized.value.code == 4401

        with client.websocket_connect("/ws/v1/agent", headers=headers) as agent:
            agent.send_json(_capabilities(str(uuid4()), str(uuid4())))
            with pytest.raises(WebSocketDisconnect) as invalid_capability:
                agent.receive_json()
        assert invalid_capability.value.code == 4400
        assert client_id
        assert _poll_agent_clients(client, headers, lambda items: not items) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "arguments", "chat_content", "approval_required"),
    [
        (
            "get_system_status",
            {},
            "현재 이 PC의 상태를 확인해 주세요.",
            False,
        ),
        (
            "create_note",
            {"title": "통합 테스트", "content": "실제 파일은 쓰지 않습니다.", "format": "txt"},
            "통합 테스트 메모를 만들어 주세요.",
            True,
        ),
    ],
)
async def test_chat_executes_one_exact_agent_tool_and_persists_one_call(
    tmp_path: Path,
    tool_name: str,
    arguments: dict[str, Any],
    chat_content: str,
    approval_required: bool,
) -> None:
    app = create_app(tmp_path)
    provider = _ToolUsingProvider(tool_name, arguments)
    app.state.services.provider = lambda: provider
    session_id = str(uuid4())

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    listener.setblocking(False)
    port = int(listener.getsockname()[1])
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="critical",
            lifespan="on",
            timeout_graceful_shutdown=2,
        )
    )
    server_task = asyncio.create_task(server.serve(sockets=[listener]))

    async def wait_until_started() -> None:
        while not server.started:
            if server_task.done():
                await server_task
                raise AssertionError("Uvicorn exited before accepting connections")
            await asyncio.sleep(0)

    agent: Any = None
    chat: Any = None
    try:
        await asyncio.wait_for(wait_until_started(), timeout=5)
        base_url = f"http://127.0.0.1:{port}"
        async with httpx.AsyncClient(base_url=base_url, timeout=3) as http:
            pairing = await http.post(
                "/api/v1/pairing/complete",
                json={
                    "code": app.state.services.pairing.code,
                    "device_name": "phase3-agent-e2e-test",
                },
            )
            pairing.raise_for_status()
            client_id = str(pairing.json()["client_id"])
            headers = {"Authorization": f"Bearer {pairing.json()['token']}"}
            websocket_headers = [("Authorization", headers["Authorization"])]

            agent = await websockets.connect(
                f"ws://127.0.0.1:{port}/ws/v1/agent",
                additional_headers=websocket_headers,
            )
            await agent.send(
                json.dumps(
                    _capabilities(client_id, session_id, tool_name=tool_name),
                    ensure_ascii=False,
                )
            )

            async def registered() -> dict[str, Any]:
                latest: dict[str, Any] = {}
                for _ in range(100):
                    response = await http.get("/api/v1/status", headers=headers)
                    response.raise_for_status()
                    latest = response.json()
                    if any(
                        item["session_id"] == session_id
                        for item in latest["agent"]["clients"]
                    ):
                        return latest
                raise AssertionError(f"Agent did not register: {latest!r}")

            status = await asyncio.wait_for(registered(), timeout=3)
            assert status["agent"]["selected_target_client"] == client_id

            chat = await websockets.connect(
                f"ws://127.0.0.1:{port}/ws/v1/chat",
                additional_headers=websocket_headers,
            )
            await chat.send(
                json.dumps(_chat_request(chat_content), ensure_ascii=False)
            )

            request = ToolRequest.model_validate_json(
                await asyncio.wait_for(agent.recv(), timeout=3)
            )
            assert str(request.target_client_id) == client_id
            assert str(request.target_session_id) == session_id
            assert request.tool_name == tool_name
            assert request.arguments == arguments
            if approval_required:
                await agent.send(json.dumps(_approved_event(request), ensure_ascii=False))
            await agent.send(json.dumps(_started_event(request), ensure_ascii=False))
            await agent.send(json.dumps(_completed_result(request), ensure_ascii=False))

            chat_events: list[dict[str, Any]] = []
            while True:
                event = json.loads(await asyncio.wait_for(chat.recv(), timeout=3))
                chat_events.append(event)
                if event["type"] in {
                    "assistant.completed",
                    "chat.cancelled",
                    "error",
                }:
                    break
            # A ping gives any queued duplicate completion a chance to surface;
            # the next event must be the correlated pong.
            await chat.send(json.dumps({"type": "ping", "protocol_version": "1.0"}))
            chat_events.append(
                json.loads(await asyncio.wait_for(chat.recv(), timeout=3))
            )

            event_types = [event["type"] for event in chat_events]
            assert event_types == [
                "chat.accepted",
                "assistant.context",
                "assistant.delta",
                "assistant.completed",
                "pong",
            ]
            assert event_types.count("assistant.completed") == 1
            completed = chat_events[-2]
            conversation_id = completed["payload"]["conversation_id"]

            tool_history = await http.get(
                f"/api/v1/conversations/{conversation_id}/tool-calls",
                headers=headers,
            )
            tool_history.raise_for_status()
            rows = tool_history.json()
            assert len(rows) == 1
            assert rows[0]["tool_call_id"] == str(request.tool_call_id)
            assert rows[0]["request_id"] == str(request.request_id)
            assert rows[0]["target_client_id"] == client_id
            assert rows[0]["target_session_id"] == session_id
            assert rows[0]["status"] == "completed"
    finally:
        if chat is not None:
            await chat.close()
        if agent is not None:
            await agent.close()
        server.should_exit = True
        await asyncio.wait_for(server_task, timeout=5)
        listener.close()

    assert len(provider.tool_definitions) == 1
    function = provider.tool_definitions[0].get("function")
    assert isinstance(function, Mapping)
    assert function.get("name") == tool_name
    assert [message.role for message in provider.planning_messages] == ["system", "user"]
    assert [message.role for message in provider.final_messages] == ["system", "user"]
    assert provider.final_messages[-1].content.endswith(
        f"[현재 사용자 요청]\n{chat_content}"
    )
    untrusted = [
        message.content
        for message in provider.final_messages
        if message.role == "user" and "[도구 결과: 신뢰되지 않은 데이터]" in message.content
    ]
    assert len(untrusted) == 1
    assert f"source_tool: {tool_name}" in untrusted[0]
    assert "trusted: false" in untrusted[0]
    assert f"Nivelle Link returned bounded {tool_name} data." in untrusted[0]
