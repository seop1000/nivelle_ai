import asyncio
from pathlib import Path

from fastapi.testclient import TestClient
from nivelle_core.app import create_app


def test_admin_can_load_and_update_persona_without_changing_boundaries(
    tmp_path: Path,
) -> None:
    persona_dir = tmp_path / "config" / "persona"
    persona_dir.mkdir(parents=True)
    boundaries = persona_dir / "boundaries.yaml"
    boundaries.write_text(
        "actions_never_allowed: [인증 정보 노출]\ncustom_boundary: 유지\n",
        encoding="utf-8",
    )
    boundaries_before = boundaries.read_bytes()
    app = create_app(tmp_path)

    with TestClient(app) as client:
        code = app.state.services.pairing.code
        paired = client.post(
            "/api/v1/pairing/complete",
            json={"code": code, "device_name": "persona-admin"},
        )
        headers = {"Authorization": f"Bearer {paired.json()['token']}"}

        loaded = client.get("/api/v1/persona", headers=headers)
        assert loaded.status_code == 200
        assert set(loaded.json()) == {"identity", "behavior"}

        updated = client.put(
            "/api/v1/persona",
            headers=headers,
            json={"identity": {"tone": "따뜻하고 명확함"}},
        )
        assert updated.status_code == 200
        assert updated.json()["identity"]["tone"] == "따뜻하고 명확함"
        assert boundaries.read_bytes() == boundaries_before

        invalid = client.put(
            "/api/v1/persona",
            headers=headers,
            json={"identity": {"name": ""}},
        )
        assert invalid.status_code == 422


def test_persona_requires_administrator(tmp_path: Path) -> None:
    app = create_app(tmp_path)
    with TestClient(app) as client:
        code = app.state.services.pairing.code
        paired = client.post(
            "/api/v1/pairing/complete",
            json={"code": code, "device_name": "persona-viewer"},
        )
        client_id = paired.json()["client_id"]
        token = paired.json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        asyncio.run(
            app.state.services.db.execute(
                "UPDATE clients SET is_admin=0 WHERE id=?", (client_id,)
            )
        )

        assert client.get("/api/v1/persona", headers=headers).status_code == 403
        assert client.put("/api/v1/persona", headers=headers, json={}).status_code == 403


def test_persona_api_preserves_malformed_yaml_and_returns_conflict(tmp_path: Path) -> None:
    persona_dir = tmp_path / "config" / "persona"
    persona_dir.mkdir(parents=True)
    identity = persona_dir / "identity.yaml"
    original = b"name: [broken\nlegacy: keep\n"
    identity.write_bytes(original)
    app = create_app(tmp_path)

    with TestClient(app) as client:
        code = app.state.services.pairing.code
        paired = client.post(
            "/api/v1/pairing/complete",
            json={"code": code, "device_name": "persona-corruption-test"},
        )
        headers = {"Authorization": f"Bearer {paired.json()['token']}"}

        response = client.put(
            "/api/v1/persona",
            headers=headers,
            json={"identity": {"name": "덮어쓰면 안 됨"}},
        )

    assert response.status_code == 409
    assert "저장하지 않았습니다" in response.json()["detail"]
    assert identity.read_bytes() == original
