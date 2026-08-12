import json
import sqlite3
from pathlib import Path

import pytest
from nivelle_core import paths as server_paths
from nivelle_protocol import local_migration
from nivelle_protocol.local_migration import (
    MIGRATION_MARKER,
    LocalDataMigrationError,
    MigrationConflictError,
    UnsafeMigrationPathError,
    migrate_core_data,
    migrate_link_data,
    resolve_data_root,
)


def _create_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE example(value TEXT NOT NULL)")
        connection.execute("INSERT INTO example(value) VALUES('preserved')")
        connection.commit()
    finally:
        connection.close()


def test_link_migration_is_staged_backed_up_and_idempotent(tmp_path: Path) -> None:
    legacy = tmp_path / "Nozomi" / "NozomiClient"
    current = tmp_path / "Nivelle" / "NivelleLink"
    connections = legacy / "connections.yaml"
    connections.parent.mkdir(parents=True)
    connections.write_text("connections: []\n", encoding="utf-8")
    (legacy / "logs").mkdir()

    result = migrate_link_data(legacy, current)

    assert result.status == "migrated"
    assert (current / "connections.yaml").read_text(encoding="utf-8") == "connections: []\n"
    assert connections.read_text(encoding="utf-8") == "connections: []\n"
    assert result.marker == current / MIGRATION_MARKER
    assert result.backup is not None
    assert (result.backup / "connections.yaml").read_text(encoding="utf-8") == (
        "connections: []\n"
    )
    marker = json.loads((current / MIGRATION_MARKER).read_text(encoding="utf-8"))
    assert marker["status"] == "completed"
    assert marker["legacy_source_preserved"] is True
    assert marker["backup"].startswith("backups/pre_nivelle_0.4.0_")
    assert list(current.parent.glob(f".{current.name}.migration-*")) == []

    repeated = migrate_link_data(legacy, current)

    assert repeated.status == "already_migrated"
    assert repeated.backup == result.backup


def test_core_database_uses_verified_sqlite_backup_and_new_name(tmp_path: Path) -> None:
    legacy = tmp_path / "Nozomi" / "NozomiServer"
    current = tmp_path / "Nivelle" / "NivelleCore"
    legacy_database = legacy / "database" / "nozomi.db"
    _create_database(legacy_database)
    config = legacy / "config" / "server.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("host: 127.0.0.1\n", encoding="utf-8")

    result = migrate_core_data(legacy, current)

    current_database = current / "database" / "nivelle.db"
    assert current_database.is_file()
    assert not (current / "database" / "nozomi.db").exists()
    with sqlite3.connect(current_database) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("SELECT value FROM example").fetchone() == ("preserved",)
    assert result.backup is not None
    backup_database = result.backup / "database" / "nozomi.db"
    with sqlite3.connect(backup_database) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("SELECT value FROM example").fetchone() == ("preserved",)
    assert legacy_database.is_file()
    assert config.is_file()
    assert (current / "config" / "server.yaml").is_file()


def test_migration_accepts_an_existing_empty_destination(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy"
    current = tmp_path / "current"
    legacy.mkdir()
    (legacy / "connections.yaml").write_text("connections: []\n", encoding="utf-8")
    (current / "empty" / "nested").mkdir(parents=True)

    assert migrate_link_data(legacy, current).status == "migrated"
    assert (current / "connections.yaml").is_file()


def test_migration_refuses_to_merge_independent_state(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy"
    current = tmp_path / "current"
    legacy.mkdir()
    current.mkdir()
    (legacy / "connections.yaml").write_text("legacy", encoding="utf-8")
    (current / "connections.yaml").write_text("current", encoding="utf-8")

    with pytest.raises(MigrationConflictError, match="refusing to merge"):
        migrate_link_data(legacy, current)

    assert (legacy / "connections.yaml").read_text(encoding="utf-8") == "legacy"
    assert (current / "connections.yaml").read_text(encoding="utf-8") == "current"


def test_migration_refuses_overlapping_roots(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "connections.yaml").write_text("legacy", encoding="utf-8")

    with pytest.raises(LocalDataMigrationError, match="must not overlap"):
        migrate_link_data(legacy, legacy / "NivelleLink")


def test_core_migration_refuses_two_competing_database_files(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy"
    current = tmp_path / "current"
    _create_database(legacy / "database" / "nozomi.db")
    _create_database(legacy / "database" / "nivelle.db")

    with pytest.raises(MigrationConflictError, match="both old and canonical"):
        migrate_core_data(legacy, current)

    assert not current.exists()


def test_invalid_marker_never_silently_overwrites_current_state(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy"
    current = tmp_path / "current"
    legacy.mkdir()
    current.mkdir()
    (legacy / "connections.yaml").write_text("legacy", encoding="utf-8")
    (current / MIGRATION_MARKER).write_text("{}", encoding="utf-8")

    with pytest.raises(MigrationConflictError, match="does not match"):
        migrate_link_data(legacy, current)


def test_corrupt_legacy_database_is_not_promoted(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy"
    current = tmp_path / "current"
    database = legacy / "database" / "nozomi.db"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"not a sqlite database")

    with pytest.raises((LocalDataMigrationError, sqlite3.DatabaseError)):
        migrate_core_data(legacy, current)

    assert database.read_bytes() == b"not a sqlite database"
    assert not current.exists()


def test_no_legacy_state_does_not_create_or_mark_destination(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy"
    current = tmp_path / "current"

    result = migrate_link_data(legacy, current)

    assert result.status == "not_needed"
    assert not current.exists()


def test_migration_rejects_reparse_points_before_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = tmp_path / "legacy"
    current = tmp_path / "current"
    legacy.mkdir()
    protected = legacy / "connections.yaml"
    protected.write_text("legacy", encoding="utf-8")
    original = local_migration._is_reparse_point

    def fake_reparse(path: Path) -> bool:
        return path == protected or original(path)

    monkeypatch.setattr(local_migration, "_is_reparse_point", fake_reparse)

    with pytest.raises(UnsafeMigrationPathError, match="symlink or junction"):
        migrate_link_data(legacy, current)
    assert not current.exists()


def test_resolver_prefers_current_environment_without_touching_legacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current_override = tmp_path / "override"
    legacy_override = tmp_path / "old-override"
    monkeypatch.setenv("NIVELLE_LINK_DATA_DIR", str(current_override))
    monkeypatch.setenv("NOZOMI_CLIENT_DATA_DIR", str(legacy_override))

    resolved = resolve_data_root(
        current_environment_variables=("NIVELLE_LINK_DATA_DIR",),
        legacy_environment_variable="NOZOMI_CLIENT_DATA_DIR",
        current_default=tmp_path / "current-default",
        legacy_default=tmp_path / "legacy-default",
        component="link",
    )

    assert resolved == current_override
    assert not current_override.exists()


def test_resolver_warns_for_legacy_environment_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy_override = tmp_path / "old-override"
    monkeypatch.delenv("NIVELLE_CORE_DATA_DIR", raising=False)
    monkeypatch.setenv("NOZOMI_SERVER_DATA_DIR", str(legacy_override))

    with pytest.warns(FutureWarning, match="NIVELLE_CORE_DATA_DIR"):
        resolved = resolve_data_root(
            current_environment_variables=("NIVELLE_CORE_DATA_DIR",),
            legacy_environment_variable="NOZOMI_SERVER_DATA_DIR",
            current_default=tmp_path / "current-default",
            legacy_default=tmp_path / "legacy-default",
            component="core",
        )

    assert resolved == legacy_override


def test_default_server_root_migrates_to_nivelle_core_before_directory_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = tmp_path / "Nozomi" / "NozomiServer"
    current = tmp_path / "Nivelle" / "NivelleCore"
    _create_database(legacy / "database" / "nivelle.db")
    for variable in (
        "NIVELLE_CORE_DATA_DIR",
        "NIVELLE_SERVER_DATA_DIR",
        "NOZOMI_SERVER_DATA_DIR",
    ):
        monkeypatch.delenv(variable, raising=False)

    def fake_user_data_path(app_name: str, app_author: str) -> Path:
        return tmp_path / app_author / app_name

    monkeypatch.setattr(server_paths, "user_data_path", fake_user_data_path)

    assert server_paths.server_data_dir() == current
    assert (current / "database" / "nivelle.db").is_file()
    assert (legacy / "database" / "nivelle.db").is_file()
    for directory in ("config", "database", "logs", "backups", "runtime", "pairing"):
        assert (current / directory).is_dir()
