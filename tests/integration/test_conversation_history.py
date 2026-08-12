import asyncio
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from threading import Event
from typing import Any
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from nivelle_core.app import create_app
from nivelle_core.llm import PromptMessage


class CapturingProvider:
    def __init__(self) -> None:
        self.calls: list[list[PromptMessage]] = []

    async def stream(self, messages: Sequence[PromptMessage]) -> AsyncIterator[str]:
        turn = len(self.calls) + 1
        self.calls.append(list(messages))
        yield f"{turn}번째 답변"


class BlockingProvider:
    def __init__(self) -> None:
        self.calls: list[list[PromptMessage]] = []
        self.started = Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._release: asyncio.Event | None = None

    async def stream(self, messages: Sequence[PromptMessage]) -> AsyncIterator[str]:
        self.calls.append(list(messages))
        self._loop = asyncio.get_running_loop()
        self._release = asyncio.Event()
        self.started.set()
        await self._release.wait()
        yield "동시성 제어 후 답변"

    def allow_completion(self) -> None:
        if not self.started.wait(timeout=5) or self._loop is None or self._release is None:
            raise RuntimeError("provider did not start")
        self._loop.call_soon_threadsafe(self._release.set)


def pair(client: TestClient, app: FastAPI) -> dict[str, str]:
    code = app.state.services.pairing.code
    response = client.post(
        "/api/v1/pairing/complete",
        json={"code": code, "device_name": "conversation-history-test"},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['token']}"}


def send_chat(
    websocket: Any, content: str, conversation_id: str | None = None
) -> list[dict[str, Any]]:
    request: dict[str, Any] = {
        "type": "chat.request",
        "protocol_version": "1.0",
        "request_id": str(uuid4()),
        "content": content,
    }
    if conversation_id is not None:
        request["conversation_id"] = conversation_id
    websocket.send_json(request)

    events: list[dict[str, Any]] = []
    while True:
        event = websocket.receive_json()
        events.append(event)
        if event["type"] in {"assistant.completed", "error", "chat.cancelled"}:
            return events


def test_same_conversation_persists_two_turns_and_passes_completed_history(
    tmp_path: Path,
) -> None:
    app = create_app(tmp_path)
    provider = CapturingProvider()
    app.state.services.provider = lambda: provider

    with TestClient(app) as client:
        headers = pair(client, app)
        with client.websocket_connect("/ws/v1/chat", headers=headers) as websocket:
            first_events = send_chat(websocket, "첫 질문")
            accepted = next(event for event in first_events if event["type"] == "chat.accepted")
            conversation_id = str(accepted["payload"]["conversation_id"])

            second_events = send_chat(websocket, "둘째 질문", conversation_id)

        assert [event["type"] for event in second_events] == [
            "chat.accepted",
            "assistant.context",
            "assistant.delta",
            "assistant.completed",
        ]
        conversations = client.get("/api/v1/conversations", headers=headers).json()
        assert [conversation["id"] for conversation in conversations] == [conversation_id]
        messages = client.get(
            f"/api/v1/conversations/{conversation_id}/messages", headers=headers
        ).json()

    assert [message["role"] for message in messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert [message["content"] for message in messages] == [
        "첫 질문",
        "1번째 답변",
        "둘째 질문",
        "2번째 답변",
    ]
    assert len(provider.calls) == 2
    assert [(message.role, message.content) for message in provider.calls[1][1:]] == [
        ("user", "첫 질문"),
        ("assistant", "1번째 답변"),
        ("user", "둘째 질문"),
    ]
    assert sum(message.content == "둘째 질문" for message in provider.calls[1]) == 1


def test_unknown_and_archived_conversations_are_rejected_without_new_messages(
    tmp_path: Path,
) -> None:
    app = create_app(tmp_path)
    provider = CapturingProvider()
    app.state.services.provider = lambda: provider

    with TestClient(app) as client:
        headers = pair(client, app)
        unknown_id = str(uuid4())
        with client.websocket_connect("/ws/v1/chat", headers=headers) as websocket:
            unknown_events = send_chat(websocket, "저장되면 안 됨", unknown_id)

        assert [event["type"] for event in unknown_events] == ["error"]
        assert unknown_events[0]["payload"]["code"] == "CONVERSATION_NOT_FOUND"
        assert client.get("/api/v1/conversations", headers=headers).json() == []
        assert provider.calls == []

        with client.websocket_connect("/ws/v1/chat", headers=headers) as websocket:
            valid_events = send_chat(websocket, "보관 전 질문")
        accepted = next(event for event in valid_events if event["type"] == "chat.accepted")
        archived_id = str(accepted["payload"]["conversation_id"])
        asyncio.run(
            app.state.services.db.execute(
                "UPDATE conversations SET archived_at=datetime('now') WHERE id=?",
                (archived_id,),
            )
        )

        with client.websocket_connect("/ws/v1/chat", headers=headers) as websocket:
            archived_events = send_chat(websocket, "보관 후 저장되면 안 됨", archived_id)

        assert [event["type"] for event in archived_events] == ["error"]
        assert archived_events[0]["payload"]["code"] == "CONVERSATION_NOT_FOUND"
        messages = client.get(
            f"/api/v1/conversations/{archived_id}/messages", headers=headers
        ).json()

    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert len(provider.calls) == 1


def test_malformed_chat_request_returns_error_and_keeps_websocket_open(
    tmp_path: Path,
) -> None:
    app = create_app(tmp_path)
    provider = CapturingProvider()
    app.state.services.provider = lambda: provider

    with TestClient(app) as client:
        headers = pair(client, app)
        with client.websocket_connect("/ws/v1/chat", headers=headers) as websocket:
            invalid_request_id = str(uuid4())
            websocket.send_json(
                {
                    "type": "chat.request",
                    "protocol_version": "1.0",
                    "request_id": invalid_request_id,
                    "conversation_id": "not-a-uuid",
                    "content": "잘못된 요청",
                }
            )
            invalid_event = websocket.receive_json()
            valid_events = send_chat(websocket, "연결 유지 확인")

        assert invalid_event["type"] == "error"
        assert invalid_event["request_id"] == invalid_request_id
        assert invalid_event["payload"]["code"] == "INVALID_REQUEST"
        assert valid_events[-1]["type"] == "assistant.completed"
        assert len(provider.calls) == 1


def test_concurrent_generation_for_same_conversation_is_rejected(
    tmp_path: Path,
) -> None:
    app = create_app(tmp_path)
    initial_provider = CapturingProvider()
    app.state.services.provider = lambda: initial_provider

    with TestClient(app) as client:
        headers = pair(client, app)
        with client.websocket_connect("/ws/v1/chat", headers=headers) as websocket:
            initial_events = send_chat(websocket, "대화 시작")
            accepted = next(
                event for event in initial_events if event["type"] == "chat.accepted"
            )
            conversation_id = str(accepted["payload"]["conversation_id"])

            blocking_provider = BlockingProvider()
            app.state.services.provider = lambda: blocking_provider
            first_request_id = str(uuid4())
            websocket.send_json(
                {
                    "type": "chat.request",
                    "protocol_version": "1.0",
                    "request_id": first_request_id,
                    "conversation_id": conversation_id,
                    "content": "먼저 처리할 요청",
                }
            )
            first_accepted = websocket.receive_json()
            assert first_accepted["type"] == "chat.accepted"
            assert first_accepted["request_id"] == first_request_id
            first_context = websocket.receive_json()
            assert first_context["type"] == "assistant.context"
            assert first_context["request_id"] == first_request_id

            busy_request_id = str(uuid4())
            websocket.send_json(
                {
                    "type": "chat.request",
                    "protocol_version": "1.0",
                    "request_id": busy_request_id,
                    "conversation_id": conversation_id,
                    "content": "겹치면 안 되는 요청",
                }
            )
            busy_event = websocket.receive_json()
            assert busy_event["type"] == "error"
            assert busy_event["request_id"] == busy_request_id
            assert busy_event["payload"]["code"] == "CONVERSATION_BUSY"

            blocking_provider.allow_completion()
            while True:
                event = websocket.receive_json()
                if event["request_id"] == first_request_id and event["type"] in {
                    "assistant.completed",
                    "error",
                    "chat.cancelled",
                }:
                    assert event["type"] == "assistant.completed"
                    break

        messages = client.get(
            f"/api/v1/conversations/{conversation_id}/messages", headers=headers
        ).json()

    assert [message["content"] for message in messages] == [
        "대화 시작",
        "1번째 답변",
        "먼저 처리할 요청",
        "동시성 제어 후 답변",
    ]
    assert len(blocking_provider.calls) == 1


def test_oversized_prompt_is_rejected_before_persistence_or_provider_call(
    tmp_path: Path,
) -> None:
    app = create_app(tmp_path)
    provider = CapturingProvider()
    app.state.services.provider = lambda: provider

    with TestClient(app) as client:
        headers = pair(client, app)
        with client.websocket_connect("/ws/v1/chat", headers=headers) as websocket:
            events = send_chat(websocket, "가" * 10_000)

        assert [event["type"] for event in events] == ["error"]
        assert events[0]["payload"]["code"] == "PROMPT_TOO_LARGE"
        assert client.get("/api/v1/conversations", headers=headers).json() == []

    assert provider.calls == []
