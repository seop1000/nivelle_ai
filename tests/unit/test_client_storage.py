from pathlib import Path

import pytest
from nivelle_link import storage
from nivelle_protocol.settings import ConnectionProfile


def test_profiles_use_client_data_dir_and_round_trip(
    tmp_path: Path, monkeypatch: object
) -> None:
    monkeypatch.setenv("NIVELLE_LINK_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    assert storage.load_connection_profiles() == []

    profile = ConnectionProfile(id="primary", host="192.168.0.10", port=9000, tls=True)
    written = storage.save_connection_profiles([profile])

    assert written == tmp_path / "connections.yaml"
    assert storage.load_connection_profiles() == [profile]
    assert list(tmp_path.glob("*.tmp")) == []


def test_local_profile_migrates_legacy_default_token(monkeypatch: object) -> None:
    values = {(storage.LEGACY_SERVICE, "default"): "legacy-token"}
    monkeypatch.setattr(  # type: ignore[attr-defined]
        storage.keyring, "get_password", lambda service, key: values.get((service, key))
    )
    monkeypatch.setattr(  # type: ignore[attr-defined]
        storage.keyring,
        "set_password",
        lambda service, key, value: values.__setitem__((service, key), value),
    )

    profile = ConnectionProfile(id="primary", host="localhost", port=8765)
    assert storage.load_token_for_profile(profile) == "legacy-token"
    assert values[(storage.SERVICE, "default")] == "legacy-token"
    assert values[(storage.SERVICE, "localhost:8765")] == "legacy-token"
    assert values[(storage.LEGACY_SERVICE, "default")] == "legacy-token"


def test_remote_profile_does_not_reuse_legacy_default_token(monkeypatch: object) -> None:
    values = {(storage.LEGACY_SERVICE, "default"): "legacy-token"}
    monkeypatch.setattr(  # type: ignore[attr-defined]
        storage.keyring, "get_password", lambda service, key: values.get((service, key))
    )

    profile = ConnectionProfile(id="primary", host="192.168.0.20", port=8765)
    assert storage.load_token_for_profile(profile) is None


def test_server_identity_token_is_shared_across_address_aliases(
    monkeypatch: object,
) -> None:
    server_id = "31c2cc21-65cc-4ab7-9258-b77497347b1b"
    values = {
        (storage.SERVICE, storage.token_key_for_server(server_id)): "shared-token"
    }
    monkeypatch.setattr(  # type: ignore[attr-defined]
        storage.keyring, "get_password", lambda service, key: values.get((service, key))
    )

    localhost = ConnectionProfile(id="local", host="localhost", server_id=server_id)
    lan = ConnectionProfile(id="lan", host="192.168.0.20", server_id=server_id)

    assert storage.load_token_for_server(localhost, server_id) == "shared-token"
    assert storage.load_token_for_server(lan, server_id) == "shared-token"


def test_endpoint_token_is_not_promoted_before_authenticated_confirmation(
    monkeypatch: object,
) -> None:
    server_id = "31c2cc21-65cc-4ab7-9258-b77497347b1b"
    profile = ConnectionProfile(id="lan", host="192.168.0.20")
    values = {(storage.SERVICE, "192.168.0.20:8765"): "legacy-token"}
    writes: list[tuple[str, str, str]] = []
    monkeypatch.setattr(  # type: ignore[attr-defined]
        storage.keyring, "get_password", lambda service, key: values.get((service, key))
    )
    monkeypatch.setattr(  # type: ignore[attr-defined]
        storage.keyring,
        "set_password",
        lambda service, key, value: writes.append((service, key, value)),
    )

    assert storage.load_token_for_server(profile, server_id) == "legacy-token"
    assert writes == []

    storage.save_token_for_server(server_id, "legacy-token")
    assert writes == [
        (storage.SERVICE, storage.token_key_for_server(server_id), "legacy-token")
    ]


def test_matching_legacy_credential_is_copied_and_verified(monkeypatch: object) -> None:
    values = {(storage.LEGACY_SERVICE, "server-a"): "old-secret"}
    writes: list[tuple[str, str, str]] = []

    def set_password(service: str, key: str, value: str) -> None:
        writes.append((service, key, value))
        values[(service, key)] = value

    monkeypatch.setattr(  # type: ignore[attr-defined]
        storage.keyring, "get_password", lambda service, key: values.get((service, key))
    )
    monkeypatch.setattr(storage.keyring, "set_password", set_password)  # type: ignore[attr-defined]

    assert storage.load_token("server-a") == "old-secret"
    assert writes == [(storage.SERVICE, "server-a", "old-secret")]
    assert values[(storage.LEGACY_SERVICE, "server-a")] == "old-secret"


def test_current_credential_wins_without_reading_legacy_service(
    monkeypatch: object,
) -> None:
    reads: list[tuple[str, str]] = []

    def get_password(service: str, key: str) -> str | None:
        reads.append((service, key))
        return "current-secret" if service == storage.SERVICE else "old-secret"

    monkeypatch.setattr(storage.keyring, "get_password", get_password)  # type: ignore[attr-defined]

    assert storage.load_token("server-a") == "current-secret"
    assert reads == [(storage.SERVICE, "server-a")]


def test_legacy_credential_copy_must_verify(monkeypatch: object) -> None:
    def get_password(service: str, _key: str) -> str | None:
        if service == storage.LEGACY_SERVICE:
            return "legacy-secret"
        return "different-secret" if writes else None

    writes: list[str] = []
    monkeypatch.setattr(storage.keyring, "get_password", get_password)  # type: ignore[attr-defined]
    monkeypatch.setattr(  # type: ignore[attr-defined]
        storage.keyring, "set_password", lambda _service, _key, value: writes.append(value)
    )

    with pytest.raises(RuntimeError, match="verification failed"):
        storage.load_token("server-a")


def test_delete_token_only_touches_current_service(monkeypatch: object) -> None:
    deleted: list[tuple[str, str]] = []
    monkeypatch.setattr(  # type: ignore[attr-defined]
        storage.keyring,
        "delete_password",
        lambda service, key: deleted.append((service, key)),
    )

    storage.delete_token("server-a")

    assert deleted == [(storage.SERVICE, "server-a")]


def test_legacy_data_dir_override_is_warned(tmp_path: Path, monkeypatch: object) -> None:
    monkeypatch.delenv("NIVELLE_LINK_DATA_DIR", raising=False)  # type: ignore[attr-defined]
    monkeypatch.delenv("NIVELLE_CLIENT_DATA_DIR", raising=False)  # type: ignore[attr-defined]
    monkeypatch.setenv("NOZOMI_CLIENT_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]

    with pytest.warns(FutureWarning, match="NIVELLE_LINK_DATA_DIR"):
        assert storage.client_data_dir() == tmp_path


def test_current_data_dir_override_has_priority(
    tmp_path: Path, monkeypatch: object
) -> None:
    current = tmp_path / "current"
    legacy = tmp_path / "legacy"
    monkeypatch.setenv("NIVELLE_LINK_DATA_DIR", str(current))  # type: ignore[attr-defined]
    monkeypatch.setenv("NOZOMI_CLIENT_DATA_DIR", str(legacy))  # type: ignore[attr-defined]

    assert storage.client_data_dir() == current


def test_default_client_root_migrates_to_nivelle_link(
    tmp_path: Path, monkeypatch: object
) -> None:
    legacy = tmp_path / "Nozomi" / "NozomiClient"
    current = tmp_path / "Nivelle" / "NivelleLink"
    legacy.mkdir(parents=True)
    (legacy / "connections.yaml").write_text("connections: []\n", encoding="utf-8")
    for variable in (
        "NIVELLE_LINK_DATA_DIR",
        "NIVELLE_CLIENT_DATA_DIR",
        "NOZOMI_CLIENT_DATA_DIR",
    ):
        monkeypatch.delenv(variable, raising=False)  # type: ignore[attr-defined]

    def fake_user_data_path(app_name: str, app_author: str) -> Path:
        return tmp_path / app_author / app_name

    monkeypatch.setattr(storage, "user_data_path", fake_user_data_path)  # type: ignore[attr-defined]

    assert storage.client_data_dir() == current
    assert (current / "connections.yaml").is_file()
    assert (legacy / "connections.yaml").is_file()
