from typing import Any

from nivelle_link.windows import (
    ConversationHistoryWindow,
    ConversationInfoWindow,
    MainChatWindow,
    PersonaWindow,
)
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView


def test_main_window_is_chat_focused_and_menu_drives_primary_actions(qtbot: Any) -> None:
    window = MainChatWindow()
    qtbot.addWidget(window)

    assert window.windowTitle() == "Nivelle Link · 레시아 니벨"
    assert window.menu_button.text() == "≡"
    assert [action.text() for action in window.menu.actions() if not action.isSeparator()] == [
        "새 대화",
        "대화 기록",
        "대화 정보",
        "Nivelle Core 연결",
        "연결 끊기",
        "Nivelle Core 관리",
        "Nivelle Archive · 장기 기억",
        "레시아 니벨 · 성격",
        "Nivelle Agent · 도구와 권한",
    ]
    assert not hasattr(window, "splitter")
    assert not hasattr(window, "context")
    assert not hasattr(window, "conversations")
    assert window.status.isHidden()
    assert window.model.isHidden()

    reconnects: list[bool] = []
    new_conversations: list[bool] = []
    info_requests: list[bool] = []
    window.reconnect_requested.connect(lambda: reconnects.append(True))
    window.new_conversation_requested.connect(lambda: new_conversations.append(True))
    window.conversation_info_requested.connect(lambda: info_requests.append(True))
    window.connection_action.trigger()
    window.new_conversation_action.trigger()
    window.conversation_info_action.trigger()

    assert reconnects == [True]
    assert new_conversations == [True]
    assert info_requests == [True]
    assert window.conversation_info_window is not None


def test_conversation_info_is_read_only_and_accepts_incremental_updates(qtbot: Any) -> None:
    window = ConversationInfoWindow()
    qtbot.addWidget(window)

    window.set_connection_info(
        {
            "profile": "LAN",
            "host": "192.168.0.20",
            "port": 8765,
            "gateway": "online",
            "llm": "ready",
            "memory_database": "ready · sqlite · 활성 12개",
            "embedding_model": "unloaded · not_configured",
            "last_checked": "18:20:14",
            "rtt_ms": 3.25,
            "reconnect_attempts": 0,
        }
    )
    window.update_connection_info({"gateway": "reconnecting", "reconnect_attempts": 1})

    assert window.connection_labels["profile"].text() == "LAN"
    assert window.connection_labels["address"].text() == "192.168.0.20:8765"
    assert window.connection_labels["gateway"].text() == "reconnecting"
    assert window.connection_labels["llm"].text() == "ready"
    assert window.connection_labels["memory_database"].text() == "ready · sqlite · 활성 12개"
    assert window.connection_labels["embedding_model"].text() == "unloaded · not_configured"
    assert window.connection_labels["last_checked"].text() == "18:20:14"
    assert window.connection_labels["rtt_ms"].text() == "3.25 ms"
    assert window.connection_labels["reconnect_attempts"].text() == "1회"

    memories = [
        {
            "id": "memory-1",
            "category": "preference",
            "priority": 80,
            "relevance_score": 0.8754,
            "final_score": 0.812,
            "included": True,
            "reason": "selected",
            "content": "응답은 간결한 한국어로 작성한다",
        }
    ]
    window.set_retrieval_context(
        {
            "retrieval": {"backend": "sqlite_hybrid", "candidate_count": 4, "top_k": 3},
            "memories": memories,
        }
    )

    assert window.used_memories == tuple(memories)
    assert window.used_memories_table.rowCount() == 1
    assert window.used_memories_table.item(0, 0).text() == "memory-1"
    assert window.used_memories_table.item(0, 3).text() == "0.875"
    assert window.used_memories_table.item(0, 4).text() == "0.812"
    assert window.used_memories_table.item(0, 5).text() == "예"
    assert window.used_memories_table.item(0, 6).text() == "selected"
    assert window.used_memories_table.item(0, 7).text() == memories[0]["content"]
    assert "sqlite_hybrid" in window.retrieval_summary.text()
    assert window.used_memories_table.editTriggers() == QAbstractItemView.EditTrigger.NoEditTriggers

    window.set_generation_metrics(
        {
            "model": "Qwen3.5-9B Q4_K_M",
            "prompt_tokens": 100,
            "completion_tokens": 25,
            "total_tokens": 125,
            "first_token_latency_ms": 120.25,
            "total_latency_ms": 1020.5,
            "tokens_per_second": 24.51,
            "finish_reason": "stop",
            "interrupted": False,
            "request_id": "request-1",
        }
    )
    assert window.generation_labels["prompt_tokens"].text() == "100"
    assert window.generation_labels["tokens_per_second"].text() == "24.51 token/s"
    assert window.generation_labels["interrupted"].text() == "아니요"

    window.clear_response_info()
    assert window.used_memories == ()
    assert window.used_memories_table.rowCount() == 0


def test_main_window_adapts_application_connection_context(qtbot: Any) -> None:
    window = MainChatWindow()
    qtbot.addWidget(window)

    window.set_connection_context(
        {
            "state": "online",
            "profile_id": "primary",
            "profile_type": "lan",
            "host": "192.168.0.20",
            "port": 8765,
            "tls": False,
            "latency_ms": 4.5,
            "last_checked_at": "2026-08-03T18:20:14+09:00",
            "reconnect_attempts": 0,
            "server_status": {
                "gateway": "running",
                "assistant_state": "idle",
                "llama_server": {"state": "ready"},
                "memory_database": {
                    "state": "ready",
                    "backend": "sqlite",
                    "active_count": 12,
                },
                "embedding_model": {
                    "state": "unloaded",
                    "provider": None,
                    "reason": "not_configured",
                },
                "version": {
                    "app_version": "0.3.1",
                    "protocol_version": "1.0",
                    "build_commit": "abc1234",
                },
                "uptime_seconds": 3660,
            },
        }
    )

    assert window.conversation_info_window is not None
    labels = window.conversation_info_window.connection_labels
    assert labels["profile"].text() == "primary · lan"
    assert labels["address"].text() == "192.168.0.20:8765"
    assert labels["gateway"].text() == "online"
    assert labels["llm"].text() == "ready"
    assert labels["memory_database"].text() == "ready · sqlite · 활성 12개"
    assert labels["embedding_model"].text() == "unloaded · not_configured"
    assert labels["tls"].text() == "사용 안 함"
    assert labels["server_version"].text() == "0.3.1"
    assert labels["protocol_version"].text() == "1.0"
    assert labels["build_commit"].text() == "abc1234"
    assert labels["rtt_ms"].text() == "4.5 ms"
    assert "primary" in window.compact_status.text()


def test_compact_status_never_presents_configured_model_as_loaded(qtbot: Any) -> None:
    window = MainChatWindow()
    qtbot.addWidget(window)

    window.set_connection_context(
        {
            "state": "online",
            "profile_id": "primary",
            "server_status": {
                "configured_model_name": "Qwen configured only",
                "components": {
                    "llm": {
                        "state": "ready",
                        "loaded_model": None,
                        "configured_model": "Qwen configured only",
                    }
                },
            },
        }
    )

    assert "모델 상태: ready" in window.compact_status.text()
    assert "Qwen configured only" not in window.compact_status.text()


def test_user_and_streaming_assistant_messages_are_independent_plain_text_bubbles(
    qtbot: Any,
) -> None:
    window = MainChatWindow()
    qtbot.addWidget(window)

    user_text = "내 질문 <b>그대로</b>"
    window.append_user_message(user_text)
    window.begin_assistant_message()
    window.append_delta("첫 토큰 ")
    window.append_delta("<script>도 글자</script>")
    window.finish_assistant_message()

    bubbles = window.message_bubbles
    assert [bubble.role for bubble in bubbles] == ["user", "assistant"]
    assert bubbles[0].content == user_text
    assert bubbles[0].content_label.text() == user_text
    assert bubbles[0].content_label.textFormat() == Qt.TextFormat.PlainText
    assert bubbles[1].content == "첫 토큰 <script>도 글자</script>"
    assert bubbles[1].content_label.textFormat() == Qt.TextFormat.PlainText
    assert bubbles[1].role_label.text() == "니벨"

    window.begin_assistant_message()
    window.append_delta("새 응답")
    assert len(window.message_bubbles) == 3
    assert window.message_bubbles[-1].content == "새 응답"


def test_loading_saved_messages_clear_and_generating_state(qtbot: Any) -> None:
    window = MainChatWindow()
    qtbot.addWidget(window)

    window.load_messages(
        [
            {"role": "user", "content": "질문"},
            {"role": "assistant", "content": "답변"},
        ]
    )
    assert [(bubble.role, bubble.content) for bubble in window.message_bubbles] == [
        ("user", "질문"),
        ("assistant", "답변"),
    ]

    window.set_generating(True)
    assert not window.input.isEnabled()
    assert not window.send_button.isEnabled()
    assert not window.new_conversation_action.isEnabled()
    window.set_generating(False)
    assert window.input.isEnabled()
    assert window.send_button.isEnabled()
    assert window.new_conversation_action.isEnabled()

    window.clear_conversation()
    assert window.message_bubbles == ()


def test_history_reload_deduplicates_messages_by_canonical_id(qtbot: Any) -> None:
    window = MainChatWindow()
    qtbot.addWidget(window)

    window.load_messages(
        [
            {"id": "user-1", "role": "user", "content": "첫 질문"},
            {"id": "assistant-1", "role": "assistant", "content": "첫 답변"},
            {"id": "assistant-1", "role": "assistant", "content": "첫 답변"},
            {"id": "assistant-2", "role": "assistant", "content": "첫 답변"},
        ]
    )

    assert [bubble.message_id for bubble in window.message_bubbles] == [
        "user-1",
        "assistant-1",
        "assistant-2",
    ]
    assert [bubble.content for bubble in window.message_bubbles] == [
        "첫 질문",
        "첫 답변",
        "첫 답변",
    ]


def test_stream_buffer_is_request_scoped_and_replay_safe(qtbot: Any) -> None:
    window = MainChatWindow()
    qtbot.addWidget(window)

    window.begin_assistant_message(request_id="request-1", assistant_message_id="assistant-1")
    assert window.append_delta(
        "첫 답변",
        request_id="request-1",
        assistant_message_id="assistant-1",
        sequence=1,
    )
    assert not window.append_delta(
        "첫 답변",
        request_id="request-1",
        assistant_message_id="assistant-1",
        sequence=1,
    )
    assert window.complete_assistant_message(
        "첫 답변",
        request_id="request-1",
        assistant_message_id="assistant-1",
    )
    assert not window.append_delta(
        "늦은 재생",
        request_id="request-1",
        assistant_message_id="assistant-1",
        sequence=2,
    )
    assert not window.complete_assistant_message(
        "첫 답변",
        request_id="request-1",
        assistant_message_id="assistant-1",
    )

    window.begin_assistant_message(request_id="request-2", assistant_message_id="assistant-2")
    assert window.append_delta(
        "둘째 ",
        request_id="request-2",
        assistant_message_id="assistant-2",
        sequence=1,
    )
    assert window.complete_assistant_message(
        "둘째 답변",
        request_id="request-2",
        assistant_message_id="assistant-2",
    )

    assert [(bubble.message_id, bubble.content) for bubble in window.message_bubbles] == [
        ("assistant-1", "첫 답변"),
        ("assistant-2", "둘째 답변"),
    ]


def test_history_window_lists_conversations_and_renders_plain_preview(qtbot: Any) -> None:
    window = ConversationHistoryWindow()
    qtbot.addWidget(window)
    window.set_conversations(
        [
            {
                "id": "conversation-1",
                "title": "첫 대화",
                "updated_at": "2026-08-03T04:05:06+00:00",
            }
        ]
    )

    selected: list[str] = []
    window.conversation_requested.connect(selected.append)
    window.conversations.setCurrentRow(0)
    assert selected == ["conversation-1"]

    window.set_conversations(
        [
            {
                "id": "conversation-1",
                "title": "첫 대화",
                "updated_at": "2026-08-03T04:06:00+00:00",
            }
        ]
    )
    assert selected == ["conversation-1"]

    window.set_preview(
        "첫 대화",
        [
            {"role": "user", "content": "<b>질문</b>"},
            {"role": "assistant", "content": "그대로 표시"},
        ],
    )
    assert "<b>질문</b>" in window.preview.toPlainText()
    assert "나\n<b>질문</b>" in window.preview.toPlainText()
    assert "니벨\n그대로 표시" in window.preview.toPlainText()


def test_persona_window_round_trips_managed_settings(qtbot: Any) -> None:
    window = PersonaWindow()
    qtbot.addWidget(window)
    window.set_persona(
        {
            "identity": {
                "name": "Nivelle",
                "full_name": "Nivelle Lethia",
                "korean_full_name": "레시아 니벨",
                "call_name": "Nivelle",
                "role": "히냥이만을 위한 개인 AI 비서이자 전속 메이드",
                "tone": "차분함",
            },
            "behavior": {
                "verbosity": "보통",
                "humor": "절제됨",
                "avoid_excessive_flattery": True,
                "user_correction_priority": True,
            },
        }
    )
    window.tone.setText("따뜻하고 명확함")

    payload = window.persona_payload()
    assert payload["identity"]["name"] == "Nivelle"
    assert payload["identity"]["full_name"] == "Nivelle Lethia"
    assert payload["identity"]["korean_full_name"] == "레시아 니벨"
    assert payload["identity"]["call_name"] == "Nivelle"
    assert payload["identity"]["tone"] == "따뜻하고 명확함"
    assert payload["behavior"]["verbosity"] == "보통"
    assert set(payload) == {"identity", "behavior"}

    saved: list[dict[str, Any]] = []
    window.save_requested.connect(saved.append)
    window.save_button.click()
    assert saved == [payload]


def test_persona_window_keeps_values_readable_but_disables_offline_edits(qtbot: Any) -> None:
    window = PersonaWindow()
    qtbot.addWidget(window)
    window.set_persona({"identity": {"tone": "차분함"}, "behavior": {}})

    window.set_online(False)

    assert window.tone.text() == "차분함"
    assert not window.tone.isEnabled()
    assert not window.save_button.isEnabled()
    assert window.refresh_button.isEnabled()
    assert "읽기 전용" in window.message.text()

    window.show_loading(True)
    window.show_loading(False)
    assert not window.save_button.isEnabled()

    window.set_online(True)
    assert window.tone.isEnabled()
    assert window.save_button.isEnabled()
