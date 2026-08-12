import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from nivelle_core.app import create_app
from nivelle_protocol.network import (
    AddressDetectionResult,
    GatewayNetworkRuntime,
    InterfaceCandidate,
    NetworkValueSource,
)
from nivelle_protocol.server_status import ServerStatus


def test_health_and_pairing(tmp_path: Path) -> None:
    app = create_app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        code = app.state.services.pairing.code
        response = client.post(
            "/api/v1/pairing/complete", json={"code": code, "device_name": "test"}
        )
        assert response.status_code == 200
        token = response.json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        assert client.post(
            "/api/v1/memories",
            headers=headers,
            json={"content": "활성 상태 기억", "active": True},
        ).status_code == 201
        assert client.post(
            "/api/v1/memories",
            headers=headers,
            json={"content": "비활성 상태 기억", "active": False},
        ).status_code == 201

        status = client.get("/api/v1/status", headers=headers)
        assert status.status_code == 200
        typed_status = ServerStatus.model_validate(status.json())
        assert typed_status.app_version == typed_status.version
        assert status.json()["memory_database"] == {
            "state": "ready",
            "backend": "sqlite",
            "search_backend": "sqlite_hybrid",
            "active_count": 1,
            "inactive_count": 1,
        }
        assert status.json()["embedding_model"] == {
            "state": "unavailable",
            "provider": None,
            "reason": "not_configured",
        }
        assert status.json()["components"]["gateway"]["state"] == "running"
        assert status.json()["components"]["gateway"]["uptime_seconds"] >= 0
        assert status.json()["components"]["memory_database"] == {
            "state": "ready",
            "backend": "sqlite",
            "search_backend": "sqlite_hybrid",
            "active_count": 1,
            "inactive_count": 1,
        }
        assert status.json()["metrics"]["last_request"] is None


def test_authenticated_status_exposes_bind_and_advertised_addresses(tmp_path: Path) -> None:
    ethernet = InterfaceCandidate(
        interface_index=9,
        name="이더넷",
        ipv4="192.168.219.100",
        hardware_interface=True,
        interface_type=6,
        gateway="192.168.219.1",
        has_default_route=True,
        route_metric=1,
        interface_metric=1,
    )
    runtime = GatewayNetworkRuntime(
        bind_host="0.0.0.0",
        port=8765,
        advertised_host=ethernet.ipv4,
        advertised_source=NetworkValueSource.AUTO_DETECTION,
        detection=AddressDetectionResult(selected=ethernet),
    )
    app = create_app(tmp_path, network_runtime=runtime)

    with TestClient(app) as client:
        paired = client.post(
            "/api/v1/pairing/complete",
            json={"code": app.state.services.pairing.code, "device_name": "network-test"},
        )
        headers = {"Authorization": f"Bearer {paired.json()['token']}"}
        payload = client.get("/api/v1/status", headers=headers).json()

    typed = ServerStatus.model_validate(payload)
    assert typed.network is not None
    assert typed.network.bind_endpoint == "http://0.0.0.0:8765"
    assert typed.network.advertised_endpoint == "http://192.168.219.100:8765"
    assert typed.network.selected_interface is not None
    assert typed.network.selected_interface.name == "이더넷"


def test_pairing_code_is_local_only_and_never_printed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    app = create_app(tmp_path)
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        local = client.get("/api/v1/pairing/local-code")
        assert local.status_code == 200
        code = local.json()["code"]
        assert isinstance(code, str) and len(code) == 6 and code.isdigit()
        output = capsys.readouterr().out
        assert code not in output
        assert "pairing/local-code" in output

    remote_app = create_app(tmp_path / "remote")
    with TestClient(remote_app, client=("192.168.219.100", 50000)) as remote:
        denied = remote.get("/api/v1/pairing/local-code")
        assert denied.status_code == 403


def test_settings_validate_partial_update_and_rollback(tmp_path: Path) -> None:
    app = create_app(tmp_path)
    with TestClient(app) as client:
        code = app.state.services.pairing.code
        paired = client.post(
            "/api/v1/pairing/complete", json={"code": code, "device_name": "admin-test"}
        )
        headers = {"Authorization": f"Bearer {paired.json()['token']}"}
        before = client.get("/api/v1/settings/inference", headers=headers).json()

        unknown = client.post(
            "/api/v1/settings/validate",
            headers=headers,
            json={"section": "unknown", "value": {}},
        )
        assert unknown.status_code == 404

        invalid = client.post(
            "/api/v1/settings/validate",
            headers=headers,
            json={"section": "inference", "value": {**before, "temperature": 99}},
        )
        assert invalid.status_code == 422
        assert invalid.json()["error"]["code"] == "CONFIG_VALIDATION_FAILED"

        extra = client.post(
            "/api/v1/settings/validate",
            headers=headers,
            json={"section": "inference", "value": {**before, "unknown_option": True}},
        )
        assert extra.status_code == 422

        models = client.get("/api/v1/settings/models", headers=headers).json()
        managed_without_path = client.post(
            "/api/v1/settings/validate",
            headers=headers,
            json={
                "section": "models",
                "value": {**models, "mode": "managed", "llama_server_path": None},
            },
        )
        assert managed_without_path.status_code == 422
        assert managed_without_path.json()["error"]["code"] == "CONFIG_VALIDATION_FAILED"

        partial = client.put(
            "/api/v1/settings/inference",
            headers=headers,
            json={"temperature": 0.25},
        )
        assert partial.status_code == 422

        updated = client.put(
            "/api/v1/settings/inference",
            headers=headers,
            json={**before, "temperature": 0.25},
        )
        assert updated.status_code == 200
        assert updated.json()["temperature"] == 0.25
        assert updated.json()["top_p"] == before["top_p"]

        revisions = client.get("/api/v1/settings/revisions", headers=headers).json()
        assert revisions[0]["section"] == "inference"
        assert revisions[0]["apply_status"] == "applied"
        rolled_back = client.post(
            f"/api/v1/settings/rollback/{revisions[0]['id']}", headers=headers
        )
        assert rolled_back.status_code == 200
        assert rolled_back.json()["temperature"] == before["temperature"]

        restart_change = client.put(
            "/api/v1/settings/inference",
            headers=headers,
            json={**before, "context_size": before["context_size"] + 512},
        )
        assert restart_change.status_code == 200
        restart_revision = client.get(
            "/api/v1/settings/revisions", headers=headers
        ).json()[0]
        assert restart_revision["apply_status"] == "pending_restart"
        restored = client.post(
            f"/api/v1/settings/rollback/{restart_revision['id']}", headers=headers
        )
        assert restored.json() == before


def test_settings_require_admin_role(tmp_path: Path) -> None:
    app = create_app(tmp_path)
    with TestClient(app) as client:
        code = app.state.services.pairing.code
        paired = client.post(
            "/api/v1/pairing/complete", json={"code": code, "device_name": "viewer"}
        )
        client_id = paired.json()["client_id"]
        token = paired.json()["token"]
        headers = {"Authorization": f"Bearer {token}"}

        asyncio.run(
            app.state.services.db.execute(
                "UPDATE clients SET is_admin=0 WHERE id=?", (client_id,)
            )
        )

        assert client.get("/api/v1/status", headers=headers).status_code == 200
        assert client.get("/api/v1/settings", headers=headers).status_code == 403
