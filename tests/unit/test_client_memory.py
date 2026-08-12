from typing import Any

import httpx
import pytest
from nivelle_link import app as client_app
from nivelle_link.windows import MemoryArchiveWindow
from nivelle_protocol.settings import ConnectionProfile
from PySide6.QtWidgets import QMessageBox

MEMORY = {
    "id": "memory-1",
    "content": "응답은 간결한 한국어로 작성한다",
    "category": "instruction",
    "active": True,
    "priority": 80,
    "created_at": "2026-08-03T01:02:03+00:00",
    "updated_at": "2026-08-03T01:02:03+00:00",
}


def test_memory_window_lists_searches_and_emits_crud_requests(
    qtbot: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = MemoryArchiveWindow()
    qtbot.addWidget(window)
    window.set_memories([MEMORY])

    assert window.memories_table.rowCount() == 1
    assert window.memory_payload() == {
        "content": MEMORY["content"],
        "category": "instruction",
        "active": True,
        "priority": 80,
    }

    searches: list[str] = []
    window.search_requested.connect(searches.append)
    window.search_input.setText("한국어 응답")
    window.include_inactive_search.setChecked(True)
    window._request_search()
    assert searches == ["한국어 응답"]
    assert window.include_inactive() is True

    created: list[dict[str, Any]] = []
    window.create_requested.connect(created.append)
    window.clear_editor()
    window.memory_content.setText("예시는 짧게 보여준다")
    window.memory_category.setCurrentIndex(window.memory_category.findData("preference"))
    window.memory_priority.setValue(60)
    window._request_create()
    assert created == [
        {
            "content": "예시는 짧게 보여준다",
            "category": "preference",
            "active": True,
            "priority": 60,
        }
    ]

    updated: list[tuple[str, dict[str, Any]]] = []
    window.update_requested.connect(lambda memory_id, value: updated.append((memory_id, value)))
    window.set_memories([MEMORY])
    window.memory_active.setChecked(False)
    window.memory_priority.setValue(90)
    window._request_update()
    assert updated[0][0] == "memory-1"
    assert updated[0][1]["active"] is False
    assert updated[0][1]["priority"] == 90

    deleted: list[str] = []
    window.delete_requested.connect(deleted.append)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )
    window._request_delete()
    assert deleted == ["memory-1"]


def test_memory_window_keeps_loaded_rows_readable_but_disables_offline_changes(
    qtbot: Any,
) -> None:
    window = MemoryArchiveWindow()
    qtbot.addWidget(window)
    window.set_memories([MEMORY])

    window.set_online(False)

    assert window.memories_table.isEnabled()
    assert window.memories_table.item(0, 0).text() == MEMORY["content"]
    assert window.refresh_button.isEnabled()
    assert not window.memory_content.isEnabled()
    assert not window.new_button.isEnabled()
    assert not window.create_button.isEnabled()
    assert not window.update_button.isEnabled()
    assert not window.delete_button.isEnabled()

    window.show_loading(True)
    window.show_loading(False)
    assert not window.create_button.isEnabled()
    assert not window.update_button.isEnabled()

    window.set_online(True)
    assert window.memory_content.isEnabled()
    assert window.create_button.isEnabled()
    assert window.update_button.isEnabled()
    assert window.delete_button.isEnabled()


@pytest.mark.asyncio
async def test_application_calls_memory_search_create_update_and_delete(
    qtbot: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = ConnectionProfile(id="server", host="192.168.0.20")
    monkeypatch.setattr(client_app, "load_connection_profiles", lambda: [profile])
    application = client_app.NivelleLinkApplication()
    qtbot.addWidget(application.window)
    application.connections.active = profile
    application.client.token = "token"
    application.window.memory_window = MemoryArchiveWindow()
    qtbot.addWidget(application.window.memory_window)
    application.window.memory_window.include_inactive_search.setChecked(True)

    calls: list[tuple[str, str, object | None]] = []

    async def get(
        path: str, params: dict[str, str | int | bool] | None = None
    ) -> list[dict[str, Any]]:
        calls.append(("GET", path, params))
        return [MEMORY]

    async def post(path: str, value: dict[str, Any] | None = None) -> dict[str, Any]:
        calls.append(("POST", path, value))
        return MEMORY

    async def patch(path: str, value: dict[str, Any]) -> dict[str, Any]:
        calls.append(("PATCH", path, value))
        return {**MEMORY, **value}

    async def delete(path: str) -> None:
        calls.append(("DELETE", path, None))

    monkeypatch.setattr(application.client, "get", get)
    monkeypatch.setattr(application.client, "post", post)
    monkeypatch.setattr(application.client, "patch", patch)
    monkeypatch.setattr(application.client, "delete", delete)

    await application._refresh_memories("한국어")
    await application._create_memory(
        {"content": "예시는 짧게 보여준다", "category": "preference", "active": True, "priority": 60}
    )
    await application._update_memory("memory-1", {"active": False, "priority": 70})
    await application._delete_memory("memory-1")

    assert calls[0] == (
        "GET",
        "/api/v1/memories/search",
        {"q": "한국어", "limit": 50, "include_inactive": True},
    )
    assert ("POST", "/api/v1/memories", {
        "content": "예시는 짧게 보여준다",
        "category": "preference",
        "active": True,
        "priority": 60,
    }) in calls
    assert ("PATCH", "/api/v1/memories/memory-1", {"active": False, "priority": 70}) in calls
    assert ("DELETE", "/api/v1/memories/memory-1", None) in calls
    assert application.window.memory_window.memories_table.rowCount() == 1


def test_memory_validation_error_is_readable() -> None:
    request = httpx.Request("POST", "http://server/api/v1/memories")
    response = httpx.Response(
        422,
        request=request,
        json={
            "detail": [
                {
                    "loc": ["body", "content"],
                    "msg": "memory contains a prohibited phone number",
                    "type": "value_error",
                }
            ]
        },
    )
    error = httpx.HTTPStatusError("invalid", request=request, response=response)

    message = client_app.NivelleLinkApplication._memory_http_error(error)

    assert "입력값을 확인하세요" in message
    assert "content" in message
    assert "phone number" in message


def test_memory_duplicate_error_names_existing_memory() -> None:
    request = httpx.Request("POST", "http://server/api/v1/memories")
    response = httpx.Response(
        409,
        request=request,
        json={
            "detail": {
                "code": "MEMORY_DUPLICATE",
                "message": "동일한 활성 기억이 이미 있습니다.",
                "existing_memory_id": "memory-existing",
            }
        },
    )
    error = httpx.HTTPStatusError("duplicate", request=request, response=response)

    message = client_app.NivelleLinkApplication._memory_http_error(error)

    assert "동일한 활성 장기 기억" in message
    assert "memory-existing" in message
