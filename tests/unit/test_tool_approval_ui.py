from typing import Any

from nivelle_link.windows import AgentManagementWindow, MainChatWindow, ToolApprovalCard
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest


def _payload(**updates: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "tool_call_id": "018f8d30-7c3d-7f50-8b61-6fe9c552a101",
        "tool_name": "read_text_file",
        "display_name": "텍스트 파일 읽기",
        "action_summary": "승인한 파일의 일부를 읽습니다.",
        "target_client_id": "client-1",
        "target_client_name": "내 데스크톱",
        "target_summary": "project/README.md",
        "risk_level": "LOCAL_READ",
        "reason": "README 요약 요청",
        "arguments": {"path_ref": "root:project:README.md", "max_lines": 100},
        "approval_modes": ["deny", "allow_once", "allow_session"],
    }
    value.update(updates)
    return value


def test_approval_card_enter_never_approves_and_escape_denies(qtbot: Any) -> None:
    card = ToolApprovalCard(_payload())
    qtbot.addWidget(card)
    decisions: list[tuple[str, str]] = []
    card.decision_requested.connect(lambda call_id, mode: decisions.append((call_id, mode)))
    card.show()
    card.setFocus()

    QTest.keyClick(card, Qt.Key.Key_Return)
    assert decisions == []
    assert card.once_button.isEnabled()

    QTest.keyClick(card, Qt.Key.Key_Escape)
    assert decisions == [(_payload()["tool_call_id"], "deny")]
    assert not card.once_button.isEnabled()


def test_local_write_never_offers_persistent_approval(qtbot: Any) -> None:
    card = ToolApprovalCard(
        _payload(
            tool_name="create_note",
            risk_level="LOCAL_WRITE",
            approval_modes=["deny", "allow_once", "allow_always_exact"],
        )
    )
    qtbot.addWidget(card)
    card.show()

    assert card.once_button.isVisible()
    assert not card.always_button.isVisible()


def test_main_window_deduplicates_tool_cards_and_keeps_status(qtbot: Any) -> None:
    window = MainChatWindow()
    qtbot.addWidget(window)

    first = window.show_tool_approval(_payload())
    repeated = window.show_tool_approval(_payload(action_summary="replayed"))

    assert first is not None
    assert repeated is first
    assert len(window._tool_cards_by_id) == 1
    assert window.update_tool_status(_payload()["tool_call_id"], "completed", "완료")
    assert first.status_label.text() == "완료"


def test_history_tool_card_is_restored_once_and_cannot_be_approved(qtbot: Any) -> None:
    window = MainChatWindow()
    qtbot.addWidget(window)
    history = {
        "tool_call_id": _payload()["tool_call_id"],
        "request_id": "018f8d30-7c3d-7f50-8b61-6fe9c552a102",
        "tool_name": "read_text_file",
        "target_client_id": "client-1",
        "arguments_summary": "validated argument fields: max_lines, path_ref",
        "risk_level": "LOCAL_READ",
        "status": "completed",
        "result_summary": "텍스트 일부를 안전하게 읽었습니다.",
    }

    window.load_tool_calls([history, dict(history)])
    card = window._tool_cards_by_id[_payload()["tool_call_id"]]
    window.show()

    assert len(window._tool_cards_by_id) == 1
    assert card.status_label.text() == "텍스트 일부를 안전하게 읽었습니다."
    assert not card.deny_button.isVisible()
    assert not card.once_button.isVisible()
    assert not card.session_button.isVisible()
    assert not card.always_button.isVisible()


def test_cancellable_card_offers_cancel_only_while_executing(qtbot: Any) -> None:
    card = ToolApprovalCard(_payload(cancellation_supported=True))
    qtbot.addWidget(card)
    decisions: list[tuple[str, str]] = []
    card.decision_requested.connect(lambda call_id, mode: decisions.append((call_id, mode)))
    card.show()

    assert not card.cancel_button.isVisible()
    card._decide("allow_once")
    card.set_status("running", "실행 중")
    assert card.cancel_button.isVisible()
    card.cancel_button.click()
    assert decisions[-1] == (_payload()["tool_call_id"], "cancel")
    card.set_status("cancelled", "취소됨")
    assert not card.cancel_button.isVisible()


def test_agent_window_is_singleton_and_active_branding_is_nivelle(qtbot: Any) -> None:
    window = MainChatWindow()
    qtbot.addWidget(window)

    window.open_agent()
    agent = window.agent_window
    window.open_agent()

    assert agent is not None
    assert window.agent_window is agent
    visible_text = "\n".join(
        [
            window.windowTitle(),
            agent.windowTitle(),
            window.connection_action.text(),
            window.admin_action.text(),
            window.memory_action.text(),
            window.persona_action.text(),
            window.agent_action.text(),
        ]
    )
    assert "Nivelle" in visible_text
    assert "레시아 니벨" in visible_text
    assert "Nozomi" not in visible_text


def test_agent_snapshot_contains_status_but_no_secret_fields(qtbot: Any) -> None:
    window = AgentManagementWindow()
    qtbot.addWidget(window)
    window.set_snapshot(
        {
            "enabled": True,
            "connected_core": "Nivelle Core",
            "client_id": "client-1",
            "session_id": "session-1",
            "enabled_tool_count": 1,
            "tools": [
                {
                    "name": "get_system_status",
                    "enabled": True,
                    "risk_level": "SAFE_STATUS",
                    "approval_mode": "none",
                    "available": True,
                    "timeout_ms": 5000,
                    "token": "must-not-render",
                }
            ],
        }
    )

    rendered = window.overview.toPlainText()
    for row in range(window.tools_table.rowCount()):
        for column in range(window.tools_table.columnCount()):
            item = window.tools_table.item(row, column)
            if item is not None:
                rendered += "\n" + item.text()
    assert "Nivelle Core" in rendered
    assert "get_system_status" in rendered
    assert "must-not-render" not in rendered
