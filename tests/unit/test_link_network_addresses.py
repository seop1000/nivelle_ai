from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from nivelle_link.storage import (
    connection_profile_from_endpoint,
    load_connection_profiles,
    validate_connection_host,
)
from nivelle_link.windows import ConnectionDialog, ServerConsoleWindow
from PySide6.QtWidgets import QDialog


@pytest.mark.parametrize(
    "host",
    [
        "0.0.0.0",
        "::",
        "169.254.10.20",
        "224.0.0.1",
        "255.255.255.255",
        "192.168.10.255",
    ],
)
def test_link_rejects_non_destination_addresses(host: str) -> None:
    with pytest.raises(ValueError):
        validate_connection_host(host)
    bracketed = f"[{host}]" if ":" in host else host
    with pytest.raises(ValueError):
        connection_profile_from_endpoint(f"http://{bracketed}:8765")


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost", "core-pc"])
def test_link_allows_explicit_loopback_and_host_names(host: str) -> None:
    assert validate_connection_host(host) == host


def test_invalid_persisted_endpoint_is_not_rewritten_or_connected(tmp_path: Path) -> None:
    path = tmp_path / "connections.yaml"
    payload = {
        "connections": [
            {
                "id": "invalid",
                "type": "local",
                "host": "0.0.0.0",
                "port": 8765,
                "tls": False,
                "priority": 1,
                "enabled": True,
            },
            {
                "id": "lan",
                "type": "vpn",
                "host": "192.168.10.20",
                "port": 8765,
                "tls": False,
                "priority": 2,
                "enabled": True,
            },
        ]
    }
    original = yaml.safe_dump(payload, sort_keys=False)
    path.write_text(original, encoding="utf-8")

    profiles = load_connection_profiles(path)

    assert [profile.id for profile in profiles] == ["lan"]
    assert path.read_text(encoding="utf-8") == original


def test_connection_dialog_starts_blank_and_explains_invalid_addresses(qtbot: Any) -> None:
    dialog = ConnectionDialog()
    qtbot.addWidget(dialog)
    assert dialog.host.text() == ""

    dialog.host.setText("0.0.0.0")
    dialog.accept()

    assert dialog.result() == 0
    assert "실제 LAN IPv4" in dialog.error.text()


def test_connection_dialog_warns_but_allows_loopback(qtbot: Any) -> None:
    dialog = ConnectionDialog()
    qtbot.addWidget(dialog)
    dialog.host.setText("127.0.0.1")

    assert "같은 PC" in dialog.warning.text()

    dialog.accept()
    assert dialog.result() == int(QDialog.DialogCode.Accepted)
    assert dialog.connection_profile().host == "127.0.0.1"


def test_server_console_supports_auto_advertised_host_and_effective_status(
    qtbot: Any,
) -> None:
    window = ServerConsoleWindow()
    qtbot.addWidget(window)
    window.set_settings(
        {
            "server": {
                "host": "0.0.0.0",
                "advertised_host": None,
                "port": 8765,
                "log_level": "INFO",
                "mock_mode": False,
            }
        }
    )
    window.set_status(
        {
            "network": {
                "bind_host": "0.0.0.0",
                "bind_port": 8765,
                "advertised_host": "192.168.219.100",
                "advertised_endpoint": "http://192.168.219.100:8765",
                "advertised_source": "auto_detection",
            }
        }
    )

    assert window.settings_payload("server")["advertised_host"] is None
    assert "0.0.0.0:8765" in window.server_effective_network.text()
    assert "192.168.219.100:8765" in window.server_effective_network.text()
    assert "auto_detection" in window.server_effective_network.text()


def test_server_console_omits_unknown_field_for_older_server_payload(qtbot: Any) -> None:
    window = ServerConsoleWindow()
    qtbot.addWidget(window)
    window.set_settings(
        {
            "server": {
                "host": "127.0.0.1",
                "port": 8765,
                "log_level": "INFO",
                "mock_mode": False,
            }
        }
    )

    assert "advertised_host" not in window.settings_payload("server")
    window.set_status({})
    assert "제공하지 않습니다" in window.server_effective_network.text()
