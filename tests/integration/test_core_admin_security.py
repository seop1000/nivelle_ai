from functools import partial
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from nivelle_core.app import create_app
from starlette.websockets import WebSocketDisconnect


def _pair(client: TestClient, app: object, name: str) -> tuple[str, str]:
    code = app.state.services.pairing.code
    response = client.post(
        "/api/v1/pairing/complete",
        json={"code": code, "device_name": name},
    )
    response.raise_for_status()
    return str(response.json()["client_id"]), str(response.json()["token"])


def test_local_revocation_closes_live_chat_and_invalidates_token(tmp_path: Path) -> None:
    app = create_app(tmp_path)
    with TestClient(app) as client:
        first_id, first_token = _pair(client, app, "First administrator")
        app.state.services.pairing.generate_code()
        second_id, _ = _pair(client, app, "Recovery administrator")
        assert client.portal is not None
        client.portal.call(
            partial(
                app.state.services.core_admin.set_admin,
                second_id,
                enabled=True,
            )
        )

        with client.websocket_connect(
            "/ws/v1/chat",
            headers={"Authorization": f"Bearer {first_token}"},
        ) as websocket:
            client.portal.call(app.state.services.core_admin.revoke_client, first_id)
            with pytest.raises(WebSocketDisconnect) as closed:
                websocket.receive_json()
            assert closed.value.code == 4403

        denied = client.get(
            "/api/v1/status",
            headers={"Authorization": f"Bearer {first_token}"},
        )
        assert denied.status_code == 401
