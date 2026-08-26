from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from nivelle_core import admin_ui
from nivelle_core import main as core_main
from nivelle_core.admin_control import CoreAdminError
from nivelle_core.admin_ui import CoreAdminWindow
from nivelle_core.app import Services
from nivelle_core.main import parse_args
from PySide6.QtWidgets import QLabel

import nivelle


async def _services_with_two_clients(root: Path) -> tuple[Services, str, str, str]:
    services = Services(root)
    await services.db.initialize()
    services.server_id = await services.db.load_or_create_server_id()
    first_code = services.pairing.generate_code()
    first_id, first_token = await services.pairing.complete(first_code, "Core owner")
    second_code = services.pairing.generate_code()
    second_id, _ = await services.pairing.complete(second_code, "Living room Link")
    return services, first_id, first_token, second_id


@pytest.mark.asyncio
async def test_core_admin_snapshot_never_exposes_token_material(tmp_path: Path) -> None:
    services, first_id, _, second_id = await _services_with_two_clients(tmp_path)
    try:
        snapshot = await services.core_admin.snapshot()
        assert snapshot["server_id"] == services.server_id
        clients = snapshot["clients"]
        assert isinstance(clients, list)
        assert {item["id"] for item in clients} == {first_id, second_id}
        assert all("token_hash" not in item and "token_salt" not in item for item in clients)
        pairing = snapshot["pairing"]
        assert isinstance(pairing, dict)
        assert pairing["code"] == services.pairing.code
    finally:
        await services.audio_analysis.shutdown()


@pytest.mark.asyncio
async def test_core_admin_preserves_last_admin_and_revokes_token(
    tmp_path: Path,
) -> None:
    services, first_id, first_token, second_id = await _services_with_two_clients(tmp_path)
    disconnected: list[str] = []

    async def record_disconnect(client_id: str) -> None:
        disconnected.append(client_id)

    services.core_admin._disconnect_client = record_disconnect
    try:
        with pytest.raises(CoreAdminError, match="마지막 관리자"):
            await services.core_admin.set_admin(first_id, enabled=False)
        with pytest.raises(CoreAdminError, match="마지막 관리자"):
            await services.core_admin.revoke_client(first_id)

        await services.core_admin.set_admin(second_id, enabled=True)
        await services.core_admin.revoke_client(first_id)
        assert disconnected == [first_id]
        assert await services.pairing.verify(first_token) is None
        row = await services.db.fetchone(
            "SELECT revoked_at FROM clients WHERE id=?", (first_id,)
        )
        assert row is not None and row["revoked_at"] is not None
    finally:
        await services.audio_analysis.shutdown()


def test_core_admin_window_is_security_only(qtbot: Any) -> None:
    window = CoreAdminWindow("D:/NivelleData")
    qtbot.addWidget(window)
    window.apply_snapshot(
        {
            "server_id": "8e2df2ea-e148-4a30-a1b7-903cf09bfb44",
            "network": {
                "bind_endpoint": "0.0.0.0:41234",
                "advertised_endpoint": "192.168.0.20:41234",
                "advertised_source": "auto",
            },
            "pairing": {
                "required": False,
                "available": True,
                "code": "123456",
                "expires_at": "2026-08-26T12:30:00+09:00",
            },
            "clients": [
                {
                    "id": "a0ceea9c-5320-4b4d-bf04-2039554cbebf",
                    "name": "Office Link",
                    "created_at": "2026-08-26T12:00:00+09:00",
                    "last_seen_at": "2026-08-26T12:10:00+09:00",
                    "revoked_at": None,
                    "is_admin": True,
                    "online": True,
                }
            ],
        }
    )

    assert "123456" in window.pairing_code_label.text()
    assert "192.168.0.20:41234" in window.network_label.text()
    assert window.clients_table.rowCount() == 1
    visible_labels = " ".join(label.text() for label in window.findChildren(QLabel))
    assert "오디오" not in visible_labels
    window.clients_table.selectRow(0)
    assert window.admin_button.text() == "일반 권한으로 변경"
    assert window.revoke_button.isEnabled()


def test_core_ui_is_default_and_headless_mode_is_explicit() -> None:
    assert parse_args([]).ui is False
    assert parse_args([]).headless is False
    assert parse_args(["--ui"]).ui is True
    assert parse_args(["--headless"]).headless is True
    assert nivelle.core_command()[-2:] == ["-m", "nivelle_core.main"]
    assert nivelle.core_command(ui=True)[-1] == "--ui"


def test_core_main_opens_ui_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    services = SimpleNamespace(
        config=SimpleNamespace(
            load=lambda section: (
                {"mode": "mock"} if section == "models" else {}
            ),
            resolved_sources={},
        ),
        network_runtime=None,
    )
    app = SimpleNamespace(state=SimpleNamespace(services=services))
    network = SimpleNamespace(bind_host="127.0.0.1", port=8765)
    opened: list[tuple[str, int, str]] = []
    monkeypatch.setattr(core_main, "server_data_dir", lambda: tmp_path)
    monkeypatch.setattr(core_main, "create_app", lambda *_args, **_kwargs: app)
    monkeypatch.setattr(core_main, "resolve_gateway_network", lambda **_kwargs: network)
    monkeypatch.setattr(core_main, "_print_network_diagnostics", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(core_main, "emit_startup_log", lambda _component: None)
    monkeypatch.setattr(
        admin_ui,
        "run_core_admin_ui",
        lambda _app, *, host, port, log_level: opened.append(
            (host, port, log_level)
        )
        or 0,
    )

    assert core_main.main([]) == 0
    assert opened == [("127.0.0.1", 8765, "info")]
