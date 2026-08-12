from copy import deepcopy
from typing import Any

import pytest
from nivelle_link import app as client_app
from nivelle_link.windows import ServerConsoleWindow
from nivelle_protocol.settings import ConnectionProfile
from PySide6.QtWidgets import QMessageBox

SETTINGS = {
    "server": {
        "host": "0.0.0.0",
        "port": 8765,
        "log_level": "INFO",
        "mock_mode": False,
    },
    "models": {
        "mode": "external",
        "llama_server_path": "runtime/llama/llama-server.exe",
        "provider_endpoint": "http://127.0.0.1:8080",
        "fallback_enabled": True,
        "models": [
            {
                "id": "qwen35-9b",
                "name": "Qwen3.5-9B",
                "path": "models/qwen.gguf",
                "role": "primary",
                "enabled": True,
            }
        ],
    },
    "inference": {
        "context_size": 8192,
        "gpu_layers": 30,
        "threads": 8,
        "batch_size": 512,
        "micro_batch_size": 128,
        "temperature": 0.7,
        "top_p": 0.9,
        "top_k": 40,
        "repeat_penalty": 1.1,
        "max_output_tokens": 1024,
        "seed": -1,
        "request_timeout": 120.0,
        "concurrent_requests": 1,
        "streaming": True,
    },
}

STATUS = {
    "version": "0.3.1",
    "app_version": "0.3.1",
    "protocol_version": "1.0",
    "runtime": {
        "component": "nivelle-core",
        "app_version": "0.3.1",
        "protocol_version": "1.0",
        "build_commit": "abc1234",
        "build_time": None,
        "executable_path": "D:/Nivelle/Nivelle-Core.exe",
        "frozen": True,
    },
    "gateway": "running",
    "pairing_required": False,
    "model_name": "Qwen3.5-9B-Q4_K_M",
    "configured_model_name": "Qwen3.5-9B",
    "model_role": "primary",
    "model_mode": "external",
    "assistant_state": "idle",
    "llama_server": {
        "state": "ready",
        "reachable": True,
        "available": True,
        "url": "http://127.0.0.1:8080",
        "status_code": 200,
        "details": {"status": "ok"},
        "loaded_model": "Qwen3.5-9B-Q4_K_M",
        "configured_model": "Qwen3.5-9B",
        "engine": "llama.cpp",
        "quantization": "Q4_K_M",
        "error": None,
    },
    "uptime_seconds": 3700,
    "metrics": {
        "cpu_percent": 12.5,
        "system_ram_percent": 50.0,
        "disk_percent": 25.0,
        "gateway_memory_bytes": 1048576,
        "gpu": None,
        "gpu_reason": "unsupported",
    },
    "memory_database": {
        "state": "ready",
        "backend": "sqlite",
        "search_backend": "sqlite_hybrid",
        "active_count": 12,
        "inactive_count": 3,
    },
    "embedding_model": {"state": "unavailable", "provider": None},
    "last_request_metrics": {
        "prompt_tokens": 100,
        "completion_tokens": 25,
        "total_tokens": 125,
        "tokens_per_second": 10.5,
        "first_token_latency_ms": 200.0,
        "total_latency_ms": 2600.0,
        "finish_reason": "stop",
        "interrupted": False,
        "model": "Qwen3.5-9B-Q4_K_M",
        "request_id": "request-status-1",
    },
}


def test_server_console_round_trips_settings_and_rollback(
    qtbot: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = ServerConsoleWindow()
    qtbot.addWidget(window)

    window.set_status(STATUS)
    window.set_settings(SETTINGS)
    window.set_revisions(
        [
            {
                "id": 7,
                "section": "inference",
                "created_at": "2026-08-03T01:02:03+00:00",
                "client_id": "desktop",
                "apply_status": "applied",
            }
        ]
    )

    overview = window.overview.toPlainText()
    assert "실제 로드 모델: Qwen3.5-9B-Q4_K_M" in overview
    assert "설정 모델: Qwen3.5-9B" in overview
    assert "추론 엔진: llama.cpp" in overview
    assert "양자화: Q4_K_M" in overview
    assert "llama-server" in overview
    assert "ready" in overview
    assert "클라이언트 앱 버전: 0.4.0" in overview
    assert "기억 DB: ready" in overview
    assert "저장 백엔드: sqlite" in overview
    assert "검색 백엔드: sqlite_hybrid" in overview
    assert "활성 기억: 12" in overview
    assert "비활성 기억: 3" in overview
    assert "임베딩: unavailable" in overview
    assert "요청 ID: request-status-1" in overview
    assert "응답 모델: Qwen3.5-9B-Q4_K_M" in overview
    assert "입력 토큰: 100" in overview
    assert "출력 토큰: 25" in overview
    assert "전체 토큰: 125" in overview
    assert "첫 토큰 지연: 200.0 ms" in overview
    assert "전체 지연: 2600.0 ms" in overview
    assert "생성 속도: 10.50 token/s" in overview
    assert "종료 사유: stop" in overview
    assert "중단됨: 아니요" in overview
    assert window.settings_payload("server") == SETTINGS["server"]
    assert window.settings_payload("models") == SETTINGS["models"]
    assert window.settings_payload("inference") == SETTINGS["inference"]
    assert window.revisions_table.rowCount() == 1

    saved: list[tuple[str, dict[str, Any]]] = []
    window.save_requested.connect(lambda section, value: saved.append((section, value)))
    window._request_save("inference")
    assert saved == [("inference", SETTINGS["inference"])]

    rolled_back: list[int] = []
    window.rollback_requested.connect(rolled_back.append)
    window.revisions_table.selectRow(0)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )
    window._request_rollback()
    assert rolled_back == [7]


def test_server_console_round_trips_model_endpoint(qtbot: Any) -> None:
    window = ServerConsoleWindow()
    qtbot.addWidget(window)
    settings = deepcopy(SETTINGS)
    settings["models"]["models"][0]["endpoint"] = "http://primary.local:8080"

    window.set_settings(settings)

    assert window.fallback_enabled.isEnabled()
    assert window.settings_payload("models") == settings["models"]


def test_server_console_keeps_status_readable_but_disables_offline_changes(
    qtbot: Any,
) -> None:
    window = ServerConsoleWindow()
    qtbot.addWidget(window)
    window.set_status(STATUS)
    window.set_settings(SETTINGS)

    window.set_online(False)

    assert "Qwen3.5-9B" in window.overview.toPlainText()
    assert window.refresh_button.isEnabled()
    assert not window.server_host.isEnabled()
    assert not window.models_table.isEnabled()
    assert not window.rollback_button.isEnabled()
    assert all(not button.isEnabled() for button in window._save_buttons)

    window.show_loading(True)
    window.show_loading(False)
    assert all(not button.isEnabled() for button in window._save_buttons)

    window.set_online(True)
    assert window.server_host.isEnabled()
    assert window.models_table.isEnabled()
    assert window.rollback_button.isEnabled()
    assert all(button.isEnabled() for button in window._save_buttons)


def test_server_console_never_labels_configured_model_as_loaded(qtbot: Any) -> None:
    window = ServerConsoleWindow()
    qtbot.addWidget(window)
    status = {
        **STATUS,
        "model_name": None,
        "configured_model_name": "Configured-Qwen",
        "llama_server": {
            **STATUS["llama_server"],
            "loaded_model": None,
            "configured_model": "Configured-Qwen",
        },
    }

    window.set_status(status)

    overview = window.overview.toPlainText()
    assert "실제 로드 모델: unsupported" in overview
    assert "설정 모델: Configured-Qwen" in overview


@pytest.mark.asyncio
async def test_application_validates_saves_and_refreshes_admin_settings(
    qtbot: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = ConnectionProfile(id="server", host="192.168.0.20")
    monkeypatch.setattr(client_app, "load_connection_profiles", lambda: [profile])
    application = client_app.NivelleLinkApplication()
    qtbot.addWidget(application.window)
    application.connections.active = profile
    application.client.token = "token"
    application.window.console = ServerConsoleWindow()
    qtbot.addWidget(application.window.console)

    calls: list[tuple[str, str, dict[str, Any] | None]] = []

    async def post(path: str, value: dict[str, Any] | None = None) -> dict[str, Any]:
        calls.append(("POST", path, value))
        return {"valid": True}

    async def put(path: str, value: dict[str, Any]) -> dict[str, Any]:
        calls.append(("PUT", path, value))
        return value

    async def get(path: str) -> Any:
        calls.append(("GET", path, None))
        return {
            "/api/v1/status": STATUS,
            "/api/v1/settings": SETTINGS,
            "/api/v1/settings/revisions": [],
        }[path]

    monkeypatch.setattr(application.client, "post", post)
    monkeypatch.setattr(application.client, "put", put)
    monkeypatch.setattr(application.client, "get", get)

    await application._save_admin("inference", SETTINGS["inference"])

    assert calls[:2] == [
        (
            "POST",
            "/api/v1/settings/validate",
            {"section": "inference", "value": SETTINGS["inference"]},
        ),
        ("PUT", "/api/v1/settings/inference", SETTINGS["inference"]),
    ]
    assert {call[1] for call in calls[2:]} == {
        "/api/v1/status",
        "/api/v1/settings",
        "/api/v1/settings/revisions",
    }
    assert "설정을 저장했습니다" in application.window.console.message.text()
