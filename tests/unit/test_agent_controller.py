from __future__ import annotations

import asyncio
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from nivelle_link.agent import AgentRuntime, FilesystemRoot, WindowsPathValidator
from nivelle_link.agent import ApprovalMode as LocalApprovalMode
from nivelle_link.agent_controller import AgentController
from nivelle_protocol.tools import ApprovalMode as ProtocolApprovalMode
from nivelle_protocol.tools import RiskLevel, ToolRequest
from nivelle_protocol.version import APP_VERSION, PROTOCOL_VERSION


class FakeSystemStatus:
    def snapshot(
        self,
        *,
        client_display_name: str,
        app_version: str,
        started_monotonic: float,
    ) -> dict[str, Any]:
        del started_monotonic
        return {
            "operating_system": {
                "name": "Windows",
                "release": "11",
                "architecture": "AMD64",
            },
            "client_display_name": client_display_name,
            "cpu_percent": 12.5,
            "ram": {
                "percent": 50.0,
                "total_bytes": 16_000,
                "available_bytes": 8_000,
            },
            "local_volumes": [],
            "battery": None,
            "network": {"available": True},
            "link_uptime_seconds": 1.0,
            "link_version": app_version,
        }


def make_request(
    controller: AgentController,
    tool_name: str,
    arguments: dict[str, Any],
    risk_level: RiskLevel,
) -> ToolRequest:
    return ToolRequest(
        tool_call_id=uuid4(),
        request_id=uuid4(),
        idempotency_key=uuid4(),
        conversation_id=uuid4(),
        user_message_id=uuid4(),
        target_client_id=controller.client_id,
        target_session_id=controller.session_id,
        tool_name=tool_name,
        tool_version="1.0",
        arguments=arguments,
        risk_level=risk_level,
        created_at=datetime.now(UTC),
        timeout_ms=10_000,
        user_intent_summary="사용자가 명시적으로 요청한 로컬 작업",
    )


def make_controller(
    tmp_path: Path,
) -> tuple[
    AgentController,
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[tuple[str, str, str | None]],
]:
    client_id = str(uuid4())
    session_id = str(uuid4())
    sent: list[dict[str, Any]] = []
    cards: list[dict[str, Any]] = []
    statuses: list[tuple[str, str, str | None]] = []
    runtime = AgentRuntime(
        data_directory=tmp_path,
        client_id=client_id,
        session_id=session_id,
        client_display_name="테스트 Link",
        link_version=APP_VERSION,
        system_status_provider=FakeSystemStatus(),  # type: ignore[arg-type]
    )

    async def send(value: dict[str, Any]) -> None:
        sent.append(value)

    controller = AgentController(
        data_directory=tmp_path,
        client_id=client_id,
        session_id=session_id,
        client_display_name="테스트 Link",
        link_version=APP_VERSION,
        send_event=send,
        show_approval=lambda value: cards.append(value),
        update_status=lambda call, status, message: statuses.append(
            (call, status, message)
        ),
        runtime=runtime,
    )
    return controller, sent, cards, statuses


@pytest.mark.asyncio
async def test_safe_status_runs_only_when_agent_is_locally_enabled(tmp_path: Path) -> None:
    controller, sent, cards, _statuses = make_controller(tmp_path)
    request = make_request(controller, "get_system_status", {}, RiskLevel.SAFE_STATUS)

    await controller.handle_server_event(request.model_dump(mode="json"))
    assert [item["type"] for item in sent] == ["tool.validation_failed"]
    assert sent[0]["error_code"] == "tool_disabled"
    assert cards == []

    sent.clear()
    controller.set_enabled(True)
    request = make_request(controller, "get_system_status", {}, RiskLevel.SAFE_STATUS)
    await controller.handle_server_event(request.model_dump(mode="json"))

    assert [item["type"] for item in sent] == [
        "tool.started",
        "tool.result",
    ]
    assert sent[-1]["status"] == "completed"
    assert sent[-1]["trusted"] is False
    controller.close()


@pytest.mark.asyncio
async def test_note_requires_explicit_ui_approval_and_duplicate_replays_once(
    tmp_path: Path,
) -> None:
    controller, sent, cards, statuses = make_controller(tmp_path)
    policy = controller.runtime.load_policy().model_copy(
        update={
            "agent_enabled": True,
            "enabled_tools": {"get_system_status", "create_note"},
        }
    )
    controller.runtime.policy_store.save(policy)
    request = make_request(
        controller,
        "create_note",
        {"title": "Phase 3 결과", "content": "승인된 내용", "format": "md"},
        RiskLevel.LOCAL_WRITE,
    )

    await controller.handle_server_event(request.model_dump(mode="json"))
    assert sent == []
    assert statuses[-1][1] == "awaiting_approval"
    assert cards[0]["preview"] == {
        "title": "Phase 3 결과",
        "content": "승인된 내용",
    }
    assert "allow_always_exact" not in cards[0]["approval_modes"]
    assert "allow_session" not in cards[0]["approval_modes"]

    await controller.decide(str(request.tool_call_id), "allow_once")
    assert [item["type"] for item in sent] == [
        "tool.approved",
        "tool.started",
        "tool.result",
    ]
    assert sent[-1]["status"] == "completed"
    assert len(list((tmp_path / "Nivelle Notes").glob("*.md"))) == 1

    before = len(sent)
    await controller.handle_server_event(request.model_dump(mode="json"))
    assert len(sent) == before + 1
    assert sent[-1]["type"] == "tool.result"
    assert len(list((tmp_path / "Nivelle Notes").glob("*.md"))) == 1
    controller.close()


@pytest.mark.asyncio
async def test_write_cannot_receive_persistent_approval_even_for_forged_signal(
    tmp_path: Path,
) -> None:
    controller, sent, _cards, _statuses = make_controller(tmp_path)
    policy = controller.runtime.load_policy().model_copy(
        update={"agent_enabled": True, "enabled_tools": {"create_note"}}
    )
    controller.runtime.policy_store.save(policy)
    request = make_request(
        controller,
        "create_note",
        {"title": "거부", "content": "작성되면 안 됨", "format": "txt"},
        RiskLevel.LOCAL_WRITE,
    )

    await controller.handle_server_event(request.model_dump(mode="json"))
    await controller.decide(str(request.tool_call_id), "allow_always_exact")

    assert [item["type"] for item in sent] == ["tool.denied"]
    assert sent[-1]["error_code"] == "permission_denied"
    assert not (tmp_path / "Nivelle Notes").exists()
    controller.close()


def test_snapshot_contains_no_authentication_token_fields(tmp_path: Path) -> None:
    controller, _sent, _cards, _statuses = make_controller(tmp_path)
    snapshot = controller.snapshot(connected_core="192.168.0.20:8765")

    serialized = str(snapshot).casefold()
    assert "token" not in serialized
    assert snapshot["client_id"] == controller.client_id
    assert len(snapshot["tools"]) == 8
    controller.close()


@pytest.mark.asyncio
async def test_capability_advertisement_uses_exact_session_identity(tmp_path: Path) -> None:
    controller, sent, _cards, _statuses = make_controller(tmp_path)
    await controller.advertise(
        app_version=APP_VERSION,
        protocol_version=PROTOCOL_VERSION,
    )

    assert sent[0]["type"] == "client.capabilities"
    assert sent[0]["client_id"] == controller.client_id
    assert sent[0]["session_id"] == controller.session_id
    controller.close()


@pytest.mark.asyncio
async def test_policy_can_make_safe_status_require_local_approval(tmp_path: Path) -> None:
    controller, sent, cards, statuses = make_controller(tmp_path)
    policy = controller.runtime.load_policy()
    approval_defaults = dict(policy.approval_defaults)
    approval_defaults["get_system_status"] = LocalApprovalMode.ALLOW_ONCE
    controller.runtime.policy_store.save(
        policy.model_copy(
            update={
                "agent_enabled": True,
                "approval_defaults": approval_defaults,
            }
        )
    )

    capabilities = controller.capabilities(
        app_version=APP_VERSION,
        protocol_version=PROTOCOL_VERSION,
    )
    system_status = next(
        item for item in capabilities.tools if item.tool_name == "get_system_status"
    )
    assert system_status.default_approval_mode is ProtocolApprovalMode.ALLOW_ONCE

    request = make_request(controller, "get_system_status", {}, RiskLevel.SAFE_STATUS)
    await controller.handle_server_event(request.model_dump(mode="json"))

    assert sent == []
    assert len(cards) == 1
    assert statuses[-1][1] == "awaiting_approval"

    await controller.decide(str(request.tool_call_id), "allow_once")
    assert [item["type"] for item in sent] == [
        "tool.approved",
        "tool.started",
        "tool.result",
    ]
    controller.close()


@pytest.mark.asyncio
async def test_terminal_denial_is_replayed_without_duplicate_approval_card(
    tmp_path: Path,
) -> None:
    controller, sent, cards, _statuses = make_controller(tmp_path)
    policy = controller.runtime.load_policy().model_copy(
        update={"agent_enabled": True, "enabled_tools": {"create_note"}}
    )
    controller.runtime.policy_store.save(policy)
    request = make_request(
        controller,
        "create_note",
        {"title": "거부", "content": "내용", "format": "txt"},
        RiskLevel.LOCAL_WRITE,
    )

    payload = request.model_dump(mode="json")
    await controller.handle_server_event(payload)
    await controller.decide(str(request.tool_call_id), "deny")
    await controller.handle_server_event(payload)

    assert [item["type"] for item in sent] == ["tool.denied", "tool.denied"]
    assert len(cards) == 1
    controller.close()


@pytest.mark.asyncio
async def test_malformed_or_wrong_session_request_fails_closed(tmp_path: Path) -> None:
    controller, sent, cards, _statuses = make_controller(tmp_path)
    controller.set_enabled(True)
    request = make_request(controller, "get_system_status", {}, RiskLevel.SAFE_STATUS)
    wrong_session = request.model_copy(update={"target_session_id": uuid4()})

    await controller.handle_server_event({"type": "tool.request", "tool_call_id": "bad"})
    await controller.handle_server_event(wrong_session.model_dump(mode="json"))
    await controller.handle_server_event(None)  # type: ignore[arg-type]

    assert sent == []
    assert cards == []
    controller.close()


def test_injected_runtime_must_match_controller_identity(tmp_path: Path) -> None:
    controller, _sent, _cards, _statuses = make_controller(tmp_path)
    with pytest.raises(ValueError, match="runtime identity"):
        AgentController(
            data_directory=tmp_path,
            client_id=str(uuid4()),
            session_id=controller.session_id,
            client_display_name="다른 Link",
            link_version=APP_VERSION,
            send_event=controller.send_event,
            show_approval=lambda _value: None,
            update_status=lambda _call, _status, _message: None,
            runtime=controller.runtime,
        )
    controller.close()


@pytest.mark.asyncio
async def test_tool_execution_does_not_block_the_qt_asyncio_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller, sent, _cards, _statuses = make_controller(tmp_path)
    controller.set_enabled(True)
    original = controller.runtime.handle_tool_request
    release = threading.Event()

    def delayed(*args: Any, **kwargs: Any) -> Any:
        assert release.wait(timeout=2)
        return original(*args, **kwargs)

    monkeypatch.setattr(controller.runtime, "handle_tool_request", delayed)
    request = make_request(controller, "get_system_status", {}, RiskLevel.SAFE_STATUS)

    async def release_from_event_loop() -> None:
        await asyncio.sleep(0.01)
        release.set()

    await asyncio.wait_for(
        asyncio.gather(
            controller.handle_server_event(request.model_dump(mode="json")),
            release_from_event_loop(),
        ),
        timeout=0.5,
    )

    assert [item["type"] for item in sent] == ["tool.started", "tool.result"]
    controller.close()


@pytest.mark.asyncio
async def test_close_cancels_running_work_and_suppresses_stale_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller, sent, _cards, statuses = make_controller(tmp_path)
    controller.set_enabled(True)
    original = controller.runtime.handle_tool_request
    entered = threading.Event()

    def wait_for_cancellation(*args: Any, **kwargs: Any) -> Any:
        cancellation = kwargs["cancellation"]
        entered.set()
        deadline = time.monotonic() + 2
        while not cancellation.is_set() and time.monotonic() < deadline:
            time.sleep(0.001)
        return original(*args, **kwargs)

    monkeypatch.setattr(
        controller.runtime,
        "handle_tool_request",
        wait_for_cancellation,
    )
    request = make_request(controller, "get_system_status", {}, RiskLevel.SAFE_STATUS)
    task = asyncio.create_task(
        controller.handle_server_event(request.model_dump(mode="json"))
    )
    assert await asyncio.to_thread(entered.wait, 1)

    controller.close()
    await asyncio.wait_for(task, timeout=1)

    assert [item["type"] for item in sent] == ["tool.started"]
    assert any(status == "client_disconnected" for _, status, _ in statuses)


@pytest.mark.asyncio
async def test_user_can_cancel_supported_running_search(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller, sent, cards, statuses = make_controller(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "one.txt").write_text("one", encoding="utf-8")
    policy = controller.runtime.load_policy().model_copy(
        update={
            "agent_enabled": True,
            "enabled_tools": {"search_files"},
            "filesystem_roots": {
                "workspace": FilesystemRoot(
                    display_name="Workspace",
                    path=workspace,
                    allow_search=True,
                )
            },
        }
    )
    controller.runtime.policy_store.save(policy)
    original = controller.runtime.handle_tool_request
    entered = threading.Event()

    def wait_for_cancellation(*args: Any, **kwargs: Any) -> Any:
        cancellation = kwargs["cancellation"]
        entered.set()
        deadline = time.monotonic() + 2
        while not cancellation.is_set() and time.monotonic() < deadline:
            time.sleep(0.001)
        return original(*args, **kwargs)

    monkeypatch.setattr(controller.runtime, "handle_tool_request", wait_for_cancellation)
    request = make_request(
        controller,
        "search_files",
        {"query": "one", "root_id": "workspace", "max_results": 10},
        RiskLevel.LOCAL_READ,
    )
    await controller.handle_server_event(request.model_dump(mode="json"))
    assert cards[0]["cancellation_supported"] is True
    execution = asyncio.create_task(
        controller.decide(str(request.tool_call_id), "allow_once")
    )
    assert await asyncio.to_thread(entered.wait, 1)

    await controller.decide(str(request.tool_call_id), "cancel")
    await asyncio.wait_for(execution, timeout=1)

    assert [item["type"] for item in sent] == [
        "tool.approved",
        "tool.started",
        "tool.result",
    ]
    assert sent[-1]["status"] == "cancelled"
    assert any(status == "cancelling" for _, status, _ in statuses)
    assert statuses[-1][1] == "cancelled"
    controller.close()


@pytest.mark.asyncio
async def test_path_approval_shows_readable_root_relative_target(
    tmp_path: Path,
) -> None:
    controller, sent, cards, _statuses = make_controller(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "README.md"
    target.write_text("safe text", encoding="utf-8")
    policy = controller.runtime.load_policy().model_copy(
        update={
            "agent_enabled": True,
            "enabled_tools": {"read_text_file"},
            "filesystem_roots": {
                "project": FilesystemRoot(
                    display_name="Nivelle 프로젝트",
                    path=workspace,
                    allow_read=True,
                )
            },
        }
    )
    controller.runtime.policy_store.save(policy)
    path_ref = WindowsPathValidator(policy).make_path_ref("project", "README.md")
    request = make_request(
        controller,
        "read_text_file",
        {"path_ref": path_ref, "start_line": 1, "max_lines": 20},
        RiskLevel.LOCAL_READ,
    )

    await controller.handle_server_event(request.model_dump(mode="json"))

    assert sent == []
    assert cards[0]["target_summary"] == "Nivelle 프로젝트 [project] / README.md"
    assert "path_ref" not in cards[0]["arguments"]
    controller.close()
