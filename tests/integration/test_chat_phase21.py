import asyncio
import json
import socket
from collections.abc import AsyncIterator, Callable, Sequence
from pathlib import Path
from threading import Event
from typing import Any
from uuid import uuid4

import httpx
import pytest
import uvicorn
import websockets
import yaml
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from nivelle_core.app import _validated_persisted_assistant_message_id, create_app
from nivelle_core.llm import LlamaCppServerProvider, PromptMessage
from nivelle_protocol.server_status import GenerationMetrics
from nivelle_protocol.settings import InferenceSettings


class CapturingProvider:
    def __init__(self) -> None:
        self.calls: list[list[PromptMessage]] = []

    async def stream(self, messages: Sequence[PromptMessage]) -> AsyncIterator[str]:
        self.calls.append(list(messages))
        yield "확인 완료"


class EchoLatestUserProvider:
    def __init__(self) -> None:
        self.calls: list[list[PromptMessage]] = []

    async def stream(self, messages: Sequence[PromptMessage]) -> AsyncIterator[str]:
        captured = list(messages)
        self.calls.append(captured)
        assert captured[-1].role == "user"
        yield f"echo:{captured[-1].content}"


class CapturingLlamaProvider(LlamaCppServerProvider):
    def __init__(self) -> None:
        super().__init__("http://127.0.0.1:1", InferenceSettings())
        self.seen_max_tokens: int | None = None

    async def stream_with_metrics(
        self,
        messages: Sequence[PromptMessage],
        *,
        on_metrics: Callable[[GenerationMetrics], None] | None = None,
    ) -> AsyncIterator[str]:
        del messages
        self.seen_max_tokens = self.inference.max_output_tokens
        yield "간결한 답변"
        if on_metrics is not None:
            on_metrics(
                GenerationMetrics(
                    prompt_tokens=20,
                    completion_tokens=4,
                    total_tokens=24,
                    tokens_per_second=12.5,
                    first_token_latency_ms=25,
                    total_latency_ms=100,
                )
            )


class BlockingProvider:
    def __init__(self) -> None:
        self.started = Event()

    async def stream(self, messages: Sequence[PromptMessage]) -> AsyncIterator[str]:
        del messages
        self.started.set()
        await asyncio.Event().wait()
        yield "unreachable"


def pair(client: TestClient, app: FastAPI) -> dict[str, str]:
    response = client.post(
        "/api/v1/pairing/complete",
        json={"code": app.state.services.pairing.code, "device_name": "phase21-chat-test"},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['token']}"}


def receive_until_terminal(websocket: Any) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    while True:
        event = websocket.receive_json()
        events.append(event)
        if event["type"] in {"assistant.completed", "chat.cancelled", "error"}:
            return events


def chat_request(
    content: str,
    *,
    request_id: str | None = None,
    client_message_id: str | None = None,
    conversation_id: str | None = None,
    retry_of_client_message_id: str | None = None,
) -> dict[str, object]:
    request: dict[str, object] = {
        "type": "chat.request",
        "protocol_version": "1.0",
        "request_id": request_id or str(uuid4()),
        "client_message_id": client_message_id or str(uuid4()),
        "content": content,
    }
    if conversation_id is not None:
        request["conversation_id"] = conversation_id
    if retry_of_client_message_id is not None:
        request["retry_of_client_message_id"] = retry_of_client_message_id
    return request


def test_completed_message_identity_must_match_accepted_message() -> None:
    accepted_message_id = str(uuid4())

    assert (
        _validated_persisted_assistant_message_id(
            {"id": accepted_message_id}, accepted_message_id
        )
        == accepted_message_id
    )
    with pytest.raises(RuntimeError, match="does not match accepted"):
        _validated_persisted_assistant_message_id(
            {"id": str(uuid4())}, accepted_message_id
        )


def test_assistant_context_precedes_delta_and_runtime_reaches_prompt(tmp_path: Path) -> None:
    app = create_app(tmp_path)
    provider = CapturingProvider()
    app.state.services.provider = lambda: provider

    with TestClient(app) as client:
        headers = pair(client, app)
        relevant = client.post(
            "/api/v1/memories",
            headers=headers,
            json={
                "content": "Nivelle Core PC의 시스템 RAM은 16GB이고 GPU 예약 메모리는 8GB이다.",
                "category": "project",
                "priority": 80,
            },
        ).json()
        client.post(
            "/api/v1/memories",
            headers=headers,
            json={
                "content": "사용자의 기본 호칭은 히냥이이다.",
                "category": "preference",
                "priority": 100,
            },
        )
        request = chat_request("서버 PC RAM과 GPU 메모리 배분은?")
        request["runtime_context"] = {
            "profile_id": "primary",
            "connection_type": "local",
            "host": "192.0.2.10",
            "port": 8765,
            "tls": False,
            "client_version": "0.3.1",
            "latency_ms": 62.49,
        }
        with client.websocket_connect("/ws/v1/chat", headers=headers) as websocket:
            websocket.send_json(request)
            events = receive_until_terminal(websocket)

        assert [event["type"] for event in events] == [
            "chat.accepted",
            "assistant.context",
            "assistant.delta",
            "assistant.completed",
        ]
        context = events[1]["payload"]
        assert context["query"] == request["content"]
        assert context["retrieval"]["backend"] == "sqlite_hybrid"
        assert context["retrieval"]["candidate_count"] == 2
        selected = [item for item in context["memories"] if item["included"]]
        rejected = [item for item in context["memories"] if not item["included"]]
        assert [item["memory_id"] for item in selected] == [relevant["id"]]
        assert rejected[0]["reason"] == "low_relevance"
        assert {
            "memory_id",
            "summary",
            "category",
            "priority",
            "relevance_score",
            "priority_score",
            "recency_score",
            "final_score",
            "included",
            "reason",
        } <= set(context["memories"][0])
        assert context["user_message_id"] == events[0]["payload"]["user_message_id"]
        assert context["assistant_message_id"] == events[0]["payload"]["assistant_message_id"]
        assert events[-1]["payload"]["message_id"] == context["assistant_message_id"]
        assert events[-1]["payload"]["assistant_message_id"] == context["assistant_message_id"]
        assert events[-1]["payload"]["message"]["id"] == context["assistant_message_id"]
        completed_metrics = events[-1]["payload"]["metrics"]
        assert completed_metrics["request_id"] == request["request_id"]
        assert completed_metrics["finish_reason"] == "stop"

        system = provider.calls[0][0].content
        assert "192.0.2.10:8765" in system
        assert "connection_profile_id: primary" in system
        assert "tls_enabled: false" in system
        assert "2PC" in system and "two-phase commit이 아니다" in system
        assert "사설 VPN을 우선" in system
        assert "포트 포워딩을 기본 해결책으로 권하지 않는다" in system
        assert "16GB" in system and "8GB" in system
        assert "사용자의 기본 호칭은 히냥이이다." not in system
        assert "[현재 사용자 호칭 적용]" not in system
        assert provider.calls[0][-1] == PromptMessage("user", str(request["content"]))


def test_sequential_turns_use_fresh_ids_and_latest_user_prompt(tmp_path: Path) -> None:
    app = create_app(tmp_path)
    provider = EchoLatestUserProvider()
    app.state.services.provider = lambda: provider

    with TestClient(app) as client:
        headers = pair(client, app)
        first_request = chat_request("첫 입력")
        with client.websocket_connect("/ws/v1/chat", headers=headers) as websocket:
            websocket.send_json(first_request)
            first_events = receive_until_terminal(websocket)
            conversation_id = first_events[0]["payload"]["conversation_id"]
            second_request = chat_request("둘째 입력", conversation_id=conversation_id)
            websocket.send_json(second_request)
            second_events = receive_until_terminal(websocket)

        first_accepted = first_events[0]["payload"]
        second_accepted = second_events[0]["payload"]
        first_completed = first_events[-1]["payload"]
        second_completed = second_events[-1]["payload"]

        assert first_request["request_id"] != second_request["request_id"]
        assert first_request["client_message_id"] != second_request["client_message_id"]
        assert first_accepted["user_message_id"] != second_accepted["user_message_id"]
        assert first_accepted["assistant_message_id"] != second_accepted["assistant_message_id"]
        assert first_completed["message"]["content"] == "echo:첫 입력"
        assert second_completed["message"]["content"] == "echo:둘째 입력"
        assert provider.calls[0][-1] == PromptMessage("user", "첫 입력")
        assert provider.calls[1][-1] == PromptMessage("user", "둘째 입력")

        messages = client.get(
            f"/api/v1/conversations/{conversation_id}/messages", headers=headers
        ).json()
        assert len(messages) == 4
        assert len({message["id"] for message in messages}) == 4


def test_completed_request_id_cannot_be_reused_on_same_socket(tmp_path: Path) -> None:
    app = create_app(tmp_path)
    provider = CapturingProvider()
    app.state.services.provider = lambda: provider

    with TestClient(app) as client:
        headers = pair(client, app)
        request_id = str(uuid4())
        with client.websocket_connect("/ws/v1/chat", headers=headers) as websocket:
            websocket.send_json(chat_request("첫 요청", request_id=request_id))
            assert receive_until_terminal(websocket)[-1]["type"] == "assistant.completed"
            websocket.send_json(
                chat_request(
                    "재사용 요청",
                    request_id=request_id,
                    client_message_id=str(uuid4()),
                )
            )
            duplicate = receive_until_terminal(websocket)[-1]

        assert duplicate["type"] == "error"
        assert duplicate["payload"]["code"] == "DUPLICATE_REQUEST"
        assert duplicate["payload"]["details"]["state"] == "completed"
        assert len(provider.calls) == 1


def test_request_id_reuse_is_rejected_after_server_restart(tmp_path: Path) -> None:
    request_id = str(uuid4())
    first_app = create_app(tmp_path)
    first_provider = CapturingProvider()
    first_app.state.services.provider = lambda: first_provider

    with TestClient(first_app) as client:
        headers = pair(client, first_app)
        with client.websocket_connect("/ws/v1/chat", headers=headers) as websocket:
            websocket.send_json(chat_request("원래 요청", request_id=request_id))
            first_events = receive_until_terminal(websocket)
        assert first_events[-1]["type"] == "assistant.completed"
        conversation_count = len(client.get("/api/v1/conversations", headers=headers).json())

    restarted_app = create_app(tmp_path)
    restarted_provider = CapturingProvider()
    restarted_app.state.services.provider = lambda: restarted_provider
    with TestClient(restarted_app) as client:
        with client.websocket_connect("/ws/v1/chat", headers=headers) as websocket:
            websocket.send_json(
                chat_request(
                    "재사용 요청",
                    request_id=request_id,
                    client_message_id=str(uuid4()),
                )
            )
            duplicate = receive_until_terminal(websocket)[-1]

        assert duplicate["type"] == "error"
        assert duplicate["payload"]["code"] == "DUPLICATE_REQUEST"
        assert duplicate["payload"]["details"]["state"] == "completed"
        assert len(restarted_provider.calls) == 0
        assert len(client.get("/api/v1/conversations", headers=headers).json()) == conversation_count


def test_duplicate_client_message_id_survives_server_restart(tmp_path: Path) -> None:
    message_id = str(uuid4())
    first_app = create_app(tmp_path)
    first_provider = CapturingProvider()
    first_app.state.services.provider = lambda: first_provider

    with TestClient(first_app) as client:
        headers = pair(client, first_app)
        with client.websocket_connect("/ws/v1/chat", headers=headers) as websocket:
            websocket.send_json(
                chat_request("한 번만 저장", client_message_id=message_id)
            )
            first_events = receive_until_terminal(websocket)
        conversation_id = first_events[0]["payload"]["conversation_id"]

    restarted_app = create_app(tmp_path)
    restarted_provider = CapturingProvider()
    restarted_app.state.services.provider = lambda: restarted_provider
    with TestClient(restarted_app) as client:
        with client.websocket_connect("/ws/v1/chat", headers=headers) as websocket:
            websocket.send_json(
                chat_request(
                    "한 번만 저장",
                    client_message_id=message_id,
                    conversation_id=conversation_id,
                )
            )
            duplicate = websocket.receive_json()
        messages = client.get(
            f"/api/v1/conversations/{conversation_id}/messages", headers=headers
        ).json()

    assert duplicate["type"] == "error"
    assert duplicate["payload"]["code"] == "DUPLICATE_MESSAGE"
    assert duplicate["payload"]["retryable"] is False
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[0]["client_message_id"] == message_id
    assert len(first_provider.calls) == 1
    assert restarted_provider.calls == []


def test_controlled_retry_accepts_interrupted_and_rejects_completed_target(
    tmp_path: Path,
) -> None:
    app = create_app(tmp_path)
    blocking = BlockingProvider()
    app.state.services.provider = lambda: blocking
    interrupted_client_id = str(uuid4())

    with TestClient(app) as client:
        headers = pair(client, app)
        with client.websocket_connect("/ws/v1/chat", headers=headers) as websocket:
            original = chat_request(
                "중단될 원래 요청", client_message_id=interrupted_client_id
            )
            websocket.send_json(original)
            accepted = websocket.receive_json()
            assert websocket.receive_json()["type"] == "assistant.context"
            assert blocking.started.wait(timeout=5)
            websocket.send_json(
                {
                    "type": "chat.cancel",
                    "protocol_version": "1.0",
                    "request_id": original["request_id"],
                }
            )
            assert websocket.receive_json()["type"] == "chat.cancelled"

        app.state.services.inflight_retry_targets.add(interrupted_client_id)
        with client.websocket_connect("/ws/v1/chat", headers=headers) as websocket:
            websocket.send_json(
                chat_request(
                    "동시에 겹친 재시도",
                    retry_of_client_message_id=interrupted_client_id,
                )
            )
            inflight_retry = websocket.receive_json()
        app.state.services.inflight_retry_targets.discard(interrupted_client_id)
        assert inflight_retry["payload"]["code"] == "RETRY_ALREADY_CREATED"
        assert inflight_retry["payload"]["details"]["state"] == "inflight"

        capturing = CapturingProvider()
        app.state.services.provider = lambda: capturing
        retried_client_id = str(uuid4())
        with client.websocket_connect("/ws/v1/chat", headers=headers) as websocket:
            websocket.send_json(
                chat_request(
                    "통제된 새 재시도",
                    client_message_id=retried_client_id,
                    retry_of_client_message_id=interrupted_client_id,
                )
            )
            retried_events = receive_until_terminal(websocket)
        assert retried_events[-1]["type"] == "assistant.completed"
        assert (
            retried_events[0]["payload"]["conversation_id"]
            == accepted["payload"]["conversation_id"]
        )

        with client.websocket_connect("/ws/v1/chat", headers=headers) as websocket:
            websocket.send_json(
                chat_request(
                    "같은 원본을 두 번째로 재시도",
                    retry_of_client_message_id=interrupted_client_id,
                )
            )
            duplicate_retry = websocket.receive_json()
        with client.websocket_connect("/ws/v1/chat", headers=headers) as websocket:
            websocket.send_json(
                chat_request(
                    "완료된 응답을 재시도",
                    retry_of_client_message_id=retried_client_id,
                )
            )
            completed_retry = websocket.receive_json()
        messages = client.get(
            f"/api/v1/conversations/{accepted['payload']['conversation_id']}/messages",
            headers=headers,
        ).json()

    assert duplicate_retry["type"] == "error"
    assert duplicate_retry["payload"]["code"] == "RETRY_ALREADY_CREATED"
    assert completed_retry["type"] == "error"
    assert completed_retry["payload"]["code"] == "RETRY_TARGET_NOT_INTERRUPTED"
    assert completed_retry["payload"]["details"]["state"] == "completed"
    assert [message["state"] for message in messages] == [
        "completed",
        "interrupted",
        "completed",
        "completed",
    ]
    assert len(capturing.calls) == 1


def test_startup_recovers_generating_and_orphan_user_turns(tmp_path: Path) -> None:
    first_app = create_app(tmp_path)
    with TestClient(first_app) as client:
        headers = pair(client, first_app)

    async def seed_unclean_shutdown_rows() -> tuple[str, str]:
        repository = first_app.state.services.conversations
        generating_conversation = await repository.create("생성 중이던 요청")
        generating_client_id = str(uuid4())
        await repository.add_message(
            generating_conversation["id"],
            "user",
            "생성 중이던 요청",
            client_message_id=generating_client_id,
        )
        await repository.add_message(
            generating_conversation["id"],
            "assistant",
            "부분 응답",
            state="generating",
            metadata={"in_reply_to_client_message_id": generating_client_id},
        )
        orphan_conversation = await repository.create("응답 배정 전 중단")
        await repository.add_message(
            orphan_conversation["id"],
            "user",
            "응답 배정 전 중단",
            client_message_id=str(uuid4()),
        )
        return generating_conversation["id"], orphan_conversation["id"]

    generating_id, orphan_id = asyncio.run(seed_unclean_shutdown_rows())
    restarted_app = create_app(tmp_path)
    with TestClient(restarted_app) as client:
        generating_messages = client.get(
            f"/api/v1/conversations/{generating_id}/messages", headers=headers
        ).json()
        orphan_messages = client.get(
            f"/api/v1/conversations/{orphan_id}/messages", headers=headers
        ).json()

    assert generating_messages[-1]["state"] == "interrupted"
    assert "server_restart" in generating_messages[-1]["metadata_json"]
    assert orphan_messages[-1]["state"] == "interrupted"
    assert "orphaned_user_turn" in orphan_messages[-1]["metadata_json"]


def test_cancelled_generation_is_persisted_as_interrupted(tmp_path: Path) -> None:
    app = create_app(tmp_path)
    provider = BlockingProvider()
    app.state.services.provider = lambda: provider
    request_id = str(uuid4())

    with TestClient(app) as client:
        headers = pair(client, app)
        with client.websocket_connect("/ws/v1/chat", headers=headers) as websocket:
            websocket.send_json(chat_request("중단 테스트", request_id=request_id))
            accepted = websocket.receive_json()
            context = websocket.receive_json()
            assert accepted["type"] == "chat.accepted"
            assert context["type"] == "assistant.context"
            assert provider.started.wait(timeout=5)
            websocket.send_json(
                {
                    "type": "chat.cancel",
                    "protocol_version": "1.0",
                    "request_id": request_id,
                }
            )
            terminal = websocket.receive_json()
        messages = client.get(
            f"/api/v1/conversations/{accepted['payload']['conversation_id']}/messages",
            headers=headers,
        ).json()

    assert terminal["type"] == "chat.cancelled"
    assert terminal["payload"]["state"] == "interrupted"
    assert [message["state"] for message in messages] == ["completed", "interrupted"]
    assert messages[1]["id"] == accepted["payload"]["assistant_message_id"]


@pytest.mark.asyncio
async def test_raw_websocket_disconnect_interrupts_streamed_generation(
    tmp_path: Path,
) -> None:
    """Exercise the production ASGI disconnect path over a real loopback socket."""

    app = create_app(tmp_path)
    provider = BlockingProvider()
    app.state.services.provider = lambda: provider

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
            await asyncio.sleep(0.01)

    try:
        await asyncio.wait_for(wait_until_started(), timeout=5)
        base_url = f"http://127.0.0.1:{port}"
        async with httpx.AsyncClient(base_url=base_url, timeout=3) as http:
            pairing = await http.post(
                "/api/v1/pairing/complete",
                json={
                    "code": app.state.services.pairing.code,
                    "device_name": "raw-disconnect-test",
                },
            )
            pairing.raise_for_status()
            headers = {"Authorization": f"Bearer {pairing.json()['token']}"}
            request = chat_request("실제 소켓 중단 테스트")

            websocket = await websockets.connect(
                f"ws://127.0.0.1:{port}/ws/v1/chat",
                additional_headers=headers,
            )
            try:
                await websocket.send(json.dumps(request, ensure_ascii=False))
                accepted = json.loads(
                    await asyncio.wait_for(websocket.recv(), timeout=3)
                )
                context = json.loads(
                    await asyncio.wait_for(websocket.recv(), timeout=3)
                )
                assert [accepted["type"], context["type"]] == [
                    "chat.accepted",
                    "assistant.context",
                ]
                assert await asyncio.to_thread(provider.started.wait, 3)
            finally:
                await websocket.close()

            conversation_id = accepted["payload"]["conversation_id"]

            async def wait_for_interrupted_messages() -> list[dict[str, Any]]:
                while True:
                    response = await http.get(
                        f"/api/v1/conversations/{conversation_id}/messages",
                        headers=headers,
                    )
                    response.raise_for_status()
                    messages = response.json()
                    if [message["state"] for message in messages] == [
                        "completed",
                        "interrupted",
                    ]:
                        return messages
                    await asyncio.sleep(0.02)

            messages = await asyncio.wait_for(
                wait_for_interrupted_messages(), timeout=3
            )

        assert [message["role"] for message in messages] == ["user", "assistant"]
        assert messages[1]["id"] == accepted["payload"]["assistant_message_id"]
    finally:
        server.should_exit = True
        await asyncio.wait_for(server_task, timeout=5)
        listener.close()


def test_completion_commit_wins_cancellation_race_on_wire_and_in_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(tmp_path)
    provider = CapturingProvider()
    app.state.services.provider = lambda: provider
    repository = app.state.services.conversations
    original_update = repository.update_message
    injected = False

    async def commit_then_cancel(
        message_id: str,
        *,
        content: str,
        state: str,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        expected_state: str | None = None,
    ) -> dict[str, object] | None:
        nonlocal injected
        result = await original_update(
            message_id,
            content=content,
            state=state,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            expected_state=expected_state,
        )
        if state == "completed" and not injected:
            injected = True
            raise asyncio.CancelledError
        return result

    monkeypatch.setattr(repository, "update_message", commit_then_cancel)

    with TestClient(app) as client:
        headers = pair(client, app)
        with client.websocket_connect("/ws/v1/chat", headers=headers) as websocket:
            websocket.send_json(chat_request("완료 커밋 경합"))
            events = receive_until_terminal(websocket)
        conversation_id = events[0]["payload"]["conversation_id"]
        messages = client.get(
            f"/api/v1/conversations/{conversation_id}/messages", headers=headers
        ).json()
        status = client.get("/api/v1/status", headers=headers).json()

    assert events[-1]["type"] == "assistant.completed"
    assert events[-1]["payload"]["metrics"]["interrupted"] is False
    assert messages[-1]["state"] == "completed"
    assert status["last_request_metrics"]["interrupted"] is False


def test_concise_persona_changes_actual_llama_max_tokens(tmp_path: Path) -> None:
    persona_dir = tmp_path / "config" / "persona"
    persona_dir.mkdir(parents=True)
    (persona_dir / "behavior_rules.yaml").write_text(
        yaml.safe_dump({"verbosity": "간결"}, allow_unicode=True), encoding="utf-8"
    )
    app = create_app(tmp_path)
    provider = CapturingLlamaProvider()
    app.state.services.provider = lambda: provider

    with TestClient(app) as client:
        headers = pair(client, app)
        with client.websocket_connect("/ws/v1/chat", headers=headers) as websocket:
            websocket.send_json(chat_request("짧게 답해줘"))
            events = receive_until_terminal(websocket)
        status = client.get("/api/v1/status", headers=headers).json()

    assert provider.seen_max_tokens == 512
    metrics = events[-1]["payload"]["metrics"]
    assert metrics["prompt_tokens"] == 20
    assert metrics["completion_tokens"] == 4
    assert metrics["tokens_per_second"] == 12.5
    assert status["last_request_metrics"] == metrics


def test_protocol_minor_difference_is_allowed_but_major_difference_is_rejected(
    tmp_path: Path,
) -> None:
    app = create_app(tmp_path)
    provider = CapturingProvider()
    app.state.services.provider = lambda: provider

    with TestClient(app) as client:
        headers = pair(client, app)
        with client.websocket_connect("/ws/v1/chat", headers=headers) as websocket:
            compatible = chat_request("minor version")
            compatible["protocol_version"] = "1.7"
            websocket.send_json(compatible)
            assert receive_until_terminal(websocket)[-1]["type"] == "assistant.completed"

            incompatible = chat_request("major version")
            incompatible["protocol_version"] = "2.0"
            websocket.send_json(incompatible)
            error = websocket.receive_json()

    assert error["type"] == "error"
    assert error["payload"]["code"] == "PROTOCOL_VERSION_MISMATCH"
    assert error["payload"]["details"]["status"] == "major_mismatch"
    assert len(provider.calls) == 1


def test_completed_database_message_does_not_regress_when_delivery_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(tmp_path)
    provider = CapturingProvider()
    app.state.services.provider = lambda: provider
    completed_delivery_attempted = Event()
    original_send_json = WebSocket.send_json

    async def fail_completed_event(
        socket: WebSocket, data: Any, mode: str = "text"
    ) -> None:
        if isinstance(data, dict) and data.get("type") == "assistant.completed":
            completed_delivery_attempted.set()
            raise RuntimeError("simulated completed delivery failure")
        await original_send_json(socket, data, mode=mode)

    monkeypatch.setattr(WebSocket, "send_json", fail_completed_event)

    with TestClient(app) as client:
        headers = pair(client, app)
        with client.websocket_connect("/ws/v1/chat", headers=headers) as websocket:
            websocket.send_json(chat_request("완료 저장 우선"))
            accepted = websocket.receive_json()
            assert websocket.receive_json()["type"] == "assistant.context"
            assert websocket.receive_json()["type"] == "assistant.delta"
            assert completed_delivery_attempted.wait(timeout=5)
        messages = client.get(
            f"/api/v1/conversations/{accepted['payload']['conversation_id']}/messages",
            headers=headers,
        ).json()

    assert [message["state"] for message in messages] == ["completed", "completed"]
    assert messages[-1]["content"] == "확인 완료"


def test_completed_database_message_does_not_regress_when_delivery_is_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(tmp_path)
    provider = CapturingProvider()
    app.state.services.provider = lambda: provider
    completed_delivery_started = Event()
    original_send_json = WebSocket.send_json

    async def block_completed_event(
        socket: WebSocket, data: Any, mode: str = "text"
    ) -> None:
        if isinstance(data, dict) and data.get("type") == "assistant.completed":
            completed_delivery_started.set()
            await asyncio.Event().wait()
        await original_send_json(socket, data, mode=mode)

    monkeypatch.setattr(WebSocket, "send_json", block_completed_event)

    with TestClient(app) as client:
        headers = pair(client, app)
        with client.websocket_connect("/ws/v1/chat", headers=headers) as websocket:
            websocket.send_json(chat_request("완료 전송 취소"))
            accepted = websocket.receive_json()
            assert websocket.receive_json()["type"] == "assistant.context"
            assert websocket.receive_json()["type"] == "assistant.delta"
            assert completed_delivery_started.wait(timeout=5)
        messages = client.get(
            f"/api/v1/conversations/{accepted['payload']['conversation_id']}/messages",
            headers=headers,
        ).json()

    assert [message["state"] for message in messages] == ["completed", "completed"]
    assert app.state.services.last_request_metrics is not None
    assert app.state.services.last_request_metrics.interrupted is False
