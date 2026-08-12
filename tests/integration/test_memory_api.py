from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from nivelle_core.app import create_app
from nivelle_core.llm import PromptMessage


def _pair(client: TestClient) -> str:
    code = client.app.state.services.pairing.code
    response = client.post(
        "/api/v1/pairing/complete", json={"code": code, "device_name": "memory-test"}
    )
    assert response.status_code == 200
    return str(response.json()["token"])


def test_authenticated_memory_crud_search_and_safety(tmp_path: Path) -> None:
    app = create_app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/api/v1/memories").status_code == 401
        token = _pair(client)
        headers = {"Authorization": f"Bearer {token}"}

        response = client.post(
            "/api/v1/memories",
            headers=headers,
            json={
                "content": "답변은 간결하게 작성한다",
                "category": "instruction",
                "priority": 90,
            },
        )
        assert response.status_code == 201
        memory_id = response.json()["id"]

        assert client.get(f"/api/v1/memories/{memory_id}", headers=headers).status_code == 200
        search = client.get(
            "/api/v1/memories/search", params={"q": "간결하게"}, headers=headers
        )
        assert [item["id"] for item in search.json()] == [memory_id]

        patched = client.patch(
            f"/api/v1/memories/{memory_id}", headers=headers, json={"active": False}
        )
        assert patched.status_code == 200
        assert patched.json()["active"] is False
        assert client.get(
            "/api/v1/memories", params={"active": True}, headers=headers
        ).json() == []
        assert client.get(
            "/api/v1/memories/search",
            params={"q": "간결하게"},
            headers=headers,
        ).json() == []
        include_inactive = client.get(
            "/api/v1/memories/search",
            params={"q": "간결하게", "include_inactive": True},
            headers=headers,
        )
        assert [item["id"] for item in include_inactive.json()] == [memory_id]

        rejected = client.post(
            "/api/v1/memories",
            headers=headers,
            json={"content": "연락처는 010-1234-5678"},
        )
        assert rejected.status_code == 422

        deleted = client.delete(f"/api/v1/memories/{memory_id}", headers=headers)
        assert deleted.status_code == 204
        assert client.get(f"/api/v1/memories/{memory_id}", headers=headers).status_code == 404


class CapturingProvider:
    def __init__(self) -> None:
        self.messages: list[PromptMessage] = []

    async def stream(self, messages: Sequence[PromptMessage]) -> AsyncIterator[str]:
        self.messages = list(messages)
        yield "ok"


def test_chat_selects_relevant_active_memories_and_reports_exact_prompt_context(
    tmp_path: Path,
) -> None:
    app = create_app(tmp_path)
    capture = CapturingProvider()
    app.state.services.provider = lambda: capture

    with TestClient(app) as client:
        token = _pair(client)
        headers = {"Authorization": f"Bearer {token}"}
        memories = (
            ("답변 문체는 간결함을 우선한다", 99, True),
            ("설명은 한국어로 작성한다", 30, True),
            ("코드 예시는 파이썬을 우선한다", 40, True),
            ("요약을 먼저 제시한다", 50, True),
            ("비활성 기억은 절대로 사용하지 않는다", 100, False),
        )
        for content, priority, active in memories:
            response = client.post(
                "/api/v1/memories",
                headers=headers,
                json={"content": content, "priority": priority, "active": active},
            )
            assert response.status_code == 201

        memory_settings = client.get("/api/v1/settings/memory", headers=headers).json()
        assert memory_settings["automatic_extraction"] is False
        before = client.get("/api/v1/memories", headers=headers).json()

        with client.websocket_connect("/ws/v1/chat", headers=headers) as websocket:
            request_id = str(uuid4())
            websocket.send_json(
                {
                    "type": "chat.request",
                    "protocol_version": "1.0",
                    "request_id": request_id,
                    "content": "한국어 코드 예시와 요약을 보여줘",
                }
            )
            events: list[dict[str, object]] = []
            while True:
                event = websocket.receive_json()
                events.append(event)
                if event["type"] == "assistant.completed":
                    break

        system_prompt = capture.messages[0].content
        context = next(event for event in events if event["type"] == "assistant.context")
        payload = context["payload"]
        assert isinstance(payload, dict)
        memories_context = payload["memories"]
        assert isinstance(memories_context, list)
        selected = [item for item in memories_context if item["included"]]
        rejected = [item for item in memories_context if not item["included"]]
        selected_contents = [item["summary"] for item in selected]
        assert set(selected_contents) == {
            "요약을 먼저 제시한다",
            "코드 예시는 파이썬을 우선한다",
            "설명은 한국어로 작성한다",
        }
        assert all(item["included"] is True for item in selected)
        assert all(0 < item["relevance_score"] <= 1 for item in selected)
        assert all(
            {
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
            }
            <= set(item)
            for item in selected
        )
        assert {item["reason"] for item in rejected} >= {"low_relevance", "inactive"}
        assert payload["retrieval"] == {
            "backend": "sqlite_hybrid",
            "top_k": 5,
            "candidate_count": 5,
        }
        assert "요약을 먼저 제시한다" in system_prompt
        assert "코드 예시는 파이썬을 우선한다" in system_prompt
        assert "설명은 한국어로 작성한다" in system_prompt
        assert "답변 문체는 간결함을 우선한다" not in system_prompt
        assert "비활성 기억은 절대로 사용하지 않는다" not in system_prompt
        assert client.get("/api/v1/memories", headers=headers).json() == before


def test_memory_api_rejects_normalized_duplicate_and_reports_existing_id(
    tmp_path: Path,
) -> None:
    app = create_app(tmp_path)
    with TestClient(app) as client:
        token = _pair(client)
        headers = {"Authorization": f"Bearer {token}"}
        created = client.post(
            "/api/v1/memories",
            headers=headers,
            json={"content": "Nivelle: 강조색은 회색이다!", "active": True},
        )
        assert created.status_code == 201
        memory_id = created.json()["id"]

        duplicate = client.post(
            "/api/v1/memories",
            headers=headers,
            json={"content": "  nivelle， 강조색은 회색이다.  ", "active": True},
        )
        assert duplicate.status_code == 409
        assert duplicate.json()["detail"] == {
            "code": "MEMORY_DUPLICATE",
            "message": "동일한 활성 기억이 이미 있습니다.",
            "existing_memory_id": memory_id,
        }

        inactive = client.post(
            "/api/v1/memories",
            headers=headers,
            json={"content": "NIVELLE 강조색은 회색이다", "active": False},
        )
        assert inactive.status_code == 201
        inactive_id = inactive.json()["id"]
        collision = client.patch(
            f"/api/v1/memories/{inactive_id}",
            headers=headers,
            json={"active": True},
        )
        assert collision.status_code == 409
        assert collision.json()["detail"]["existing_memory_id"] == memory_id

        same_record_update = client.patch(
            f"/api/v1/memories/{memory_id}",
            headers=headers,
            json={"content": "NIVELLE, 강조색은 회색이다."},
        )
        assert same_record_update.status_code == 200
        assert same_record_update.json()["id"] == memory_id

        assert client.delete(f"/api/v1/memories/{memory_id}", headers=headers).status_code == 204
        reactivated = client.patch(
            f"/api/v1/memories/{inactive_id}",
            headers=headers,
            json={"active": True},
        )
        assert reactivated.status_code == 200
        assert reactivated.json()["active"] is True
