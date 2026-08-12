import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from nivelle_link import app as client_app
from nivelle_link.windows import ConversationHistoryWindow, PersonaWindow
from nivelle_protocol.settings import ConnectionProfile
from PySide6.QtCore import Qt


def _application(monkeypatch: pytest.MonkeyPatch, qtbot: Any) -> client_app.NivelleLinkApplication:
    profile = ConnectionProfile(id="server", host="192.168.0.20")
    monkeypatch.setattr(client_app, "load_connection_profiles", lambda: [profile])
    application = client_app.NivelleLinkApplication()
    qtbot.addWidget(application.window)
    application.connections.active = profile
    application.client.token = "token"
    return application


@pytest.mark.asyncio
async def test_history_loads_server_conversations_and_opens_selected_chat(
    monkeypatch: pytest.MonkeyPatch, qtbot: Any
) -> None:
    application = _application(monkeypatch, qtbot)
    history = ConversationHistoryWindow()
    qtbot.addWidget(history)
    application.window.history_window = history

    conversations = [
        {
            "id": "conversation-1",
            "title": "저장된 첫 대화",
            "updated_at": "2026-08-03T01:02:03+00:00",
        }
    ]
    messages = [
        {"role": "user", "content": "이전 질문"},
        {"role": "assistant", "content": "이전 답변"},
    ]

    async def get(path: str, _params: object = None) -> object:
        if path == "/api/v1/conversations":
            return conversations
        if path == "/api/v1/conversations/conversation-1/messages":
            return messages
        raise AssertionError(path)

    monkeypatch.setattr(application.client, "get", get)

    await application._refresh_conversations()
    await application._load_conversation("conversation-1")

    assert history.conversations.count() == 1
    assert application._active_conversation_id == "conversation-1"
    assert [(bubble.role, bubble.content) for bubble in application.window.message_bubbles] == [
        ("user", "이전 질문"),
        ("assistant", "이전 답변"),
    ]
    assert "저장된 첫 대화" in history.preview.toPlainText()


@pytest.mark.asyncio
async def test_history_load_restores_metadata_only_tool_cards(
    monkeypatch: pytest.MonkeyPatch, qtbot: Any
) -> None:
    application = _application(monkeypatch, qtbot)
    history = ConversationHistoryWindow()
    qtbot.addWidget(history)
    application.window.history_window = history
    application._last_server_status = {"agent": {"enabled": True}}
    application._conversation_titles["conversation-tools"] = "도구가 포함된 대화"
    calls: list[str] = []

    async def get(path: str, _params: object = None) -> object:
        calls.append(path)
        if path.endswith("/messages"):
            return [
                {"id": "user-1", "role": "user", "content": "상태를 알려줘"},
                {
                    "id": "assistant-1",
                    "role": "assistant",
                    "content": "상태를 확인했습니다.",
                },
            ]
        if path.endswith("/tool-calls"):
            return [
                {
                    "tool_call_id": "tool-call-1",
                    "request_id": "request-1",
                    "target_client_id": "client-1",
                    "tool_name": "get_system_status",
                    "risk_level": "SAFE_STATUS",
                    "arguments_summary": "validated argument fields:",
                    "status": "completed",
                    "result_summary": "안전한 시스템 상태를 확인했습니다.",
                }
            ]
        raise AssertionError(path)

    monkeypatch.setattr(application.client, "get", get)
    await application._load_conversation("conversation-tools")

    assert calls == [
        "/api/v1/conversations/conversation-tools/messages",
        "/api/v1/conversations/conversation-tools/tool-calls",
    ]
    assert list(application.window._tool_cards_by_id) == ["tool-call-1"]
    card = application.window._tool_cards_by_id["tool-call-1"]
    assert card.status_label.text() == "안전한 시스템 상태를 확인했습니다."
    assert not card.once_button.isVisible()


@pytest.mark.asyncio
async def test_send_reuses_conversation_and_keeps_assistant_stream_separate(
    monkeypatch: pytest.MonkeyPatch, qtbot: Any
) -> None:
    application = _application(monkeypatch, qtbot)
    application._active_conversation_id = "c70f2510-fe0d-449a-9067-6cbc973cc2a7"
    requests: list[dict[str, Any]] = []

    async def chat(request: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        requests.append(request)
        yield {
            "type": "chat.accepted",
            "request_id": request["request_id"],
            "payload": {
                "conversation_id": request["conversation_id"],
                "user_message_id": "user-message-1",
                "assistant_message_id": "assistant-message-1",
                "client_message_id": request["client_message_id"],
            },
        }
        yield {
            "type": "assistant.context",
            "request_id": request["request_id"],
            "payload": {
                "conversation_id": request["conversation_id"],
                "user_message_id": "user-message-1",
                "assistant_message_id": "assistant-message-1",
                "client_message_id": request["client_message_id"],
                "retrieval": {"backend": "sqlite_hybrid", "candidate_count": 1, "top_k": 3},
                "memories": [
                    {
                        "memory_id": "memory-1",
                        "summary": "관련 기억",
                        "category": "project",
                        "priority": 90,
                        "relevance_score": 1.0,
                        "final_score": 0.95,
                        "included": True,
                        "reason": "selected",
                    }
                ],
            },
        }
        yield {
            "type": "assistant.delta",
            "request_id": request["request_id"],
            "payload": {
                "assistant_message_id": "assistant-message-1",
                "sequence": 1,
                "delta": "분리된 ",
            },
        }
        yield {
            "type": "assistant.delta",
            "request_id": request["request_id"],
            "payload": {
                "assistant_message_id": "assistant-message-1",
                "sequence": 2,
                "delta": "답변",
            },
        }
        yield {
            "type": "assistant.completed",
            "request_id": request["request_id"],
            "payload": {
                "conversation_id": request["conversation_id"],
                "client_message_id": request["client_message_id"],
                "message_id": "assistant-message-1",
                "assistant_message_id": "assistant-message-1",
                "message": {"id": "assistant-message-1", "content": "분리된 답변"},
                "metrics": {"completion_tokens": 7, "tokens_per_second": 3.5},
            },
        }

    monkeypatch.setattr(application.client, "chat", chat)

    await application.send("새 질문")

    assert requests[0]["conversation_id"] == application._active_conversation_id
    assert requests[0]["client_message_id"]
    assert requests[0]["runtime_context"]["host"] == "192.168.0.20"
    assert [(bubble.role, bubble.content) for bubble in application.window.message_bubbles] == [
        ("user", "새 질문"),
        ("assistant", "분리된 답변"),
    ]
    assert application.window.conversation_info_window is not None
    assert application.window.conversation_info_window.used_memories[0]["memory_id"] == "memory-1"
    assert (
        application.window.conversation_info_window.generation_labels["completion_tokens"].text()
        == "7"
    )


@pytest.mark.asyncio
async def test_duplicate_completed_event_renders_canonical_message_once(
    monkeypatch: pytest.MonkeyPatch, qtbot: Any
) -> None:
    application = _application(monkeypatch, qtbot)
    finalized: list[bool] = []

    async def chat(request: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        try:
            accepted = {
                "type": "chat.accepted",
                "request_id": request["request_id"],
                "payload": {
                    "conversation_id": "conversation-1",
                    "user_message_id": "user-1",
                    "assistant_message_id": "assistant-1",
                    "client_message_id": request["client_message_id"],
                },
            }
            completed = {
                "type": "assistant.completed",
                "request_id": request["request_id"],
                "payload": {
                    "conversation_id": "conversation-1",
                    "client_message_id": request["client_message_id"],
                    "assistant_message_id": "assistant-1",
                    "message": {
                        "id": "assistant-1",
                        "role": "assistant",
                        "content": "한 번만 표시",
                        "state": "completed",
                    },
                },
            }
            yield accepted
            yield completed
            yield completed
        finally:
            finalized.append(True)

    monkeypatch.setattr(application.client, "chat", chat)

    await application.send("중복 완료 검사")

    assert [(bubble.role, bubble.content) for bubble in application.window.message_bubbles] == [
        ("user", "중복 완료 검사"),
        ("assistant", "한 번만 표시"),
    ]
    assert application.window.message_bubbles[-1].message_id == "assistant-1"
    assert finalized == [True]


@pytest.mark.asyncio
async def test_each_submission_has_new_ids_and_ignores_previous_request_completion(
    monkeypatch: pytest.MonkeyPatch, qtbot: Any
) -> None:
    application = _application(monkeypatch, qtbot)
    requests: list[dict[str, Any]] = []

    async def chat(request: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        requests.append(dict(request))
        turn = len(requests)
        if turn == 2:
            first = requests[0]
            yield {
                "type": "assistant.completed",
                "request_id": first["request_id"],
                "payload": {
                    "client_message_id": first["client_message_id"],
                    "assistant_message_id": "assistant-1",
                    "message": {"id": "assistant-1", "content": "이전 답변"},
                },
            }
        yield {
            "type": "chat.accepted",
            "request_id": request["request_id"],
            "payload": {
                "conversation_id": "conversation-1",
                "user_message_id": f"user-{turn}",
                "assistant_message_id": f"assistant-{turn}",
                "client_message_id": request["client_message_id"],
            },
        }
        yield {
            "type": "assistant.completed",
            "request_id": request["request_id"],
            "payload": {
                "conversation_id": "conversation-1",
                "client_message_id": request["client_message_id"],
                "assistant_message_id": f"assistant-{turn}",
                "message": {
                    "id": f"assistant-{turn}",
                    "role": "assistant",
                    "content": f"새 답변 {turn}",
                    "state": "completed",
                },
            },
        }

    monkeypatch.setattr(application.client, "chat", chat)

    await application.send("첫 질문")
    await application.send("둘째 질문")

    assert requests[0]["request_id"] != requests[1]["request_id"]
    assert requests[0]["client_message_id"] != requests[1]["client_message_id"]
    assert [(bubble.role, bubble.content) for bubble in application.window.message_bubbles] == [
        ("user", "첫 질문"),
        ("assistant", "새 답변 1"),
        ("user", "둘째 질문"),
        ("assistant", "새 답변 2"),
    ]
    assert [bubble.message_id for bubble in application.window.message_bubbles] == [
        "user-1",
        "assistant-1",
        "user-2",
        "assistant-2",
    ]


@pytest.mark.asyncio
async def test_persona_window_loads_and_saves_server_settings(
    monkeypatch: pytest.MonkeyPatch, qtbot: Any
) -> None:
    application = _application(monkeypatch, qtbot)
    persona = PersonaWindow()
    qtbot.addWidget(persona)
    application.window.persona_window = persona
    saved_values: list[dict[str, Any]] = []
    value = {
        "identity": {
            "name": "Nivelle",
            "full_name": "Nivelle Lethia",
            "korean_full_name": "레시아 니벨",
            "call_name": "Nivelle",
            "profile_version": "1.0",
            "role": "히냥이만을 위한 개인 AI 비서이자 전속 메이드",
            "user_name": "히냥이",
            "user_address": "히냥이",
            "default_language": "ko",
            "tone": "차분하고 명확함",
            "relationship_description": "신뢰할 수 있는 협업자",
        },
        "behavior": {
            "everyday_conversation": "자연스럽게 답한다.",
            "technical_work": "근거를 제시한다.",
            "correction_style": "예의 있게 바로잡는다.",
            "praise_style": "구체적으로 칭찬한다.",
            "verbosity": "보통",
            "humor": "절제됨",
            "avoid_excessive_flattery": True,
            "user_correction_priority": True,
        },
    }

    async def get(path: str, _params: object = None) -> object:
        assert path == "/api/v1/persona"
        return value

    async def put(path: str, body: dict[str, Any]) -> object:
        assert path == "/api/v1/persona"
        saved_values.append(body)
        return body

    monkeypatch.setattr(application.client, "get", get)
    monkeypatch.setattr(application.client, "put", put)

    await application._refresh_persona()
    persona.tone.setText("따뜻하고 명확함")
    await application._save_persona(persona.persona_payload())

    assert saved_values[0]["identity"]["name"] == "Nivelle"
    assert saved_values[0]["identity"]["full_name"] == "Nivelle Lethia"
    assert saved_values[0]["identity"]["korean_full_name"] == "레시아 니벨"
    assert saved_values[0]["identity"]["tone"] == "따뜻하고 명확함"
    assert persona.message.text().startswith("성격 설정을 저장했습니다")


@pytest.mark.asyncio
async def test_failed_preflight_does_not_add_an_unsaved_user_bubble(
    monkeypatch: pytest.MonkeyPatch, qtbot: Any
) -> None:
    application = _application(monkeypatch, qtbot)
    application.connections.active = None
    errors: list[str] = []
    connection_requests: list[bool] = []
    monkeypatch.setattr(application.window, "show_error", errors.append)
    monkeypatch.setattr(
        application,
        "_schedule_connection_settings",
        lambda: connection_requests.append(True),
    )

    application.window.input.setPlainText("저장되면 안 되는 질문")
    application.window.send_button.click()

    assert application.window.message_bubbles == ()
    assert application.window.input.toPlainText() == "저장되면 안 되는 질문"
    assert application._send_task is None
    assert connection_requests == [True]
    assert errors == ["먼저 서버에 연결하세요."]


@pytest.mark.asyncio
async def test_conversation_load_blocks_send_and_latest_selection_wins(
    monkeypatch: pytest.MonkeyPatch, qtbot: Any
) -> None:
    application = _application(monkeypatch, qtbot)
    history = ConversationHistoryWindow()
    qtbot.addWidget(history)
    application.window.history_window = history
    application._conversation_titles = {"old": "이전", "latest": "최신"}
    old_started = asyncio.Event()

    async def get(path: str, _params: object = None) -> object:
        if path.endswith("/old/messages"):
            old_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return [{"role": "user", "content": "오래된 응답"}]
        if path.endswith("/latest/messages"):
            return [
                {"role": "user", "content": "최신 질문"},
                {"role": "assistant", "content": "최신 답변"},
            ]
        raise AssertionError(path)

    monkeypatch.setattr(application.client, "get", get)
    errors: list[str] = []
    monkeypatch.setattr(application.window, "show_error", errors.append)

    application._schedule_conversation_load("old")
    await old_started.wait()
    old_task = application._conversation_load_task
    assert old_task is not None

    application.window.input.setPlainText("로드 중 전송")
    application.window.send_button.click()
    assert application._send_task is None
    assert application.window.message_bubbles == ()
    assert errors == ["대화를 불러오는 중입니다. 완료된 뒤 메시지를 보내세요."]

    application._schedule_conversation_load("latest")
    latest_task = application._conversation_load_task
    assert latest_task is not None
    await asyncio.gather(old_task, latest_task, return_exceptions=True)

    assert application._active_conversation_id == "latest"
    assert [(bubble.role, bubble.content) for bubble in application.window.message_bubbles] == [
        ("user", "최신 질문"),
        ("assistant", "최신 답변"),
    ]


@pytest.mark.asyncio
async def test_latest_history_refresh_discards_cancelled_stale_response(
    monkeypatch: pytest.MonkeyPatch, qtbot: Any
) -> None:
    application = _application(monkeypatch, qtbot)
    history = ConversationHistoryWindow()
    qtbot.addWidget(history)
    application.window.history_window = history
    first_started = asyncio.Event()
    call_count = 0

    async def get(path: str, _params: object = None) -> object:
        nonlocal call_count
        assert path == "/api/v1/conversations"
        call_count += 1
        if call_count == 1:
            first_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return [{"id": "stale", "title": "오래된 목록"}]
        return [{"id": "latest", "title": "최신 목록"}]

    monkeypatch.setattr(application.client, "get", get)
    application._schedule_history_refresh()
    await first_started.wait()
    stale_task = application._history_refresh_task
    assert stale_task is not None

    application._schedule_history_refresh()
    latest_task = application._history_refresh_task
    assert latest_task is not None
    await asyncio.gather(stale_task, latest_task, return_exceptions=True)

    assert history.conversations.count() == 1
    assert history.conversations.item(0).data(Qt.ItemDataRole.UserRole) == "latest"
    assert application._conversation_titles == {"latest": "최신 목록"}


@pytest.mark.asyncio
async def test_generation_blocks_reconnect_and_missing_terminal_event_is_visible(
    monkeypatch: pytest.MonkeyPatch, qtbot: Any
) -> None:
    application = _application(monkeypatch, qtbot)
    stream_started = asyncio.Event()
    release_stream = asyncio.Event()
    errors: list[str] = []
    monkeypatch.setattr(application.window, "show_error", errors.append)

    async def chat(request: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        stream_started.set()
        yield {
            "type": "chat.accepted",
            "payload": {"conversation_id": "2a2c9414-a26f-4a44-8a76-4f138172b6a4"},
        }
        await release_stream.wait()

    monkeypatch.setattr(application.client, "chat", chat)
    application._schedule_send("완료 이벤트 없는 질문")
    await stream_started.wait()
    send_task = application._send_task
    assert send_task is not None
    assert not application.window.connection_action.isEnabled()

    application._schedule_connection_settings()
    assert application._connection_task is None
    assert errors == ["답변 생성 중에는 서버 연결을 변경할 수 없습니다."]

    automatic_reconnects: list[bool] = []
    monkeypatch.setattr(
        application,
        "_schedule_auto_reconnect",
        lambda: automatic_reconnects.append(True),
    )
    release_stream.set()
    await send_task
    await asyncio.sleep(0)

    assert application.connections.active is None
    assert [bubble.role for bubble in application.window.message_bubbles] == [
        "user",
        "assistant",
    ]
    assert "완료 신호 없이 연결을 종료" in application.window.message_bubbles[-1].content
    assert automatic_reconnects == [True]
    assert application.window.conversation_info_window is not None
    metrics = application.window.conversation_info_window.generation_labels
    assert metrics["finish_reason"].text() == "unknown"
    assert metrics["interrupted"].text() == "예"


@pytest.mark.asyncio
async def test_error_event_preserves_server_interruption_metrics(
    monkeypatch: pytest.MonkeyPatch, qtbot: Any
) -> None:
    application = _application(monkeypatch, qtbot)

    async def chat(request: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        yield {
            "type": "error",
            "payload": {
                "code": "LLM_STREAM_INTERRUPTED",
                "message": "중단됨",
                "details": {
                    "metrics": {
                        "request_id": request["request_id"],
                        "completion_tokens": 3,
                        "finish_reason": "error",
                        "interrupted": True,
                    }
                },
            },
        }

    monkeypatch.setattr(application.client, "chat", chat)

    await application.send("오류 메트릭")

    assert application.window.conversation_info_window is not None
    labels = application.window.conversation_info_window.generation_labels
    assert labels["completion_tokens"].text() == "3"
    assert labels["finish_reason"].text() == "error"
    assert labels["interrupted"].text() == "예"


@pytest.mark.asyncio
async def test_pre_generation_error_is_rejected_not_interrupted(
    monkeypatch: pytest.MonkeyPatch, qtbot: Any
) -> None:
    application = _application(monkeypatch, qtbot)

    async def chat(_request: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        yield {
            "type": "error",
            "payload": {
                "code": "PROMPT_TOO_LARGE",
                "message": "요청이 너무 큽니다.",
                "details": {"input_character_budget": 100},
                "retryable": False,
            },
        }

    monkeypatch.setattr(application.client, "chat", chat)

    await application.send("거절되는 요청")

    assert application.window.conversation_info_window is not None
    labels = application.window.conversation_info_window.generation_labels
    assert labels["finish_reason"].text() == "rejected"
    assert labels["interrupted"].text() == "아니요"
