"""Safe one-time migration of legacy Nozomi local application data.

This module intentionally has no dependency on either desktop application so
both Nivelle Core and Nivelle Link can perform their migration before opening
configuration files or databases.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import warnings
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

MIGRATION_VERSION = "0.4.0"
MIGRATION_MARKER = ".nivelle-0.4-migration.json"


class LocalDataMigrationError(RuntimeError):
    """Base error for a local-data migration that was not safe to complete."""


class MigrationConflictError(LocalDataMigrationError):
    """Both the legacy and canonical roots contain independent state."""


class UnsafeMigrationPathError(LocalDataMigrationError):
    """A migration root contains a link or another unsupported file type."""


@dataclass(frozen=True)
class LocalDataMigrationResult:
    status: Literal["not_needed", "migrated", "already_migrated"]
    destination: Path
    marker: Path | None = None
    backup: Path | None = None


def _normalized_path(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def _roots_overlap(first: Path, second: Path) -> bool:
    first_path = _normalized_path(first)
    second_path = _normalized_path(second)
    try:
        common = os.path.normcase(os.path.commonpath((first_path, second_path)))
    except ValueError:
        # Different Windows drives cannot contain one another.
        return False
    return common in {first_path, second_path}


def _is_reparse_point(path: Path) -> bool:
    """Return true for symlinks, junctions, and other Windows reparse points."""

    metadata = path.lstat()
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    reparse_attribute = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return path.is_symlink() or bool(attributes & reparse_attribute)


def _assert_plain_path(path: Path) -> None:
    if _is_reparse_point(path):
        raise UnsafeMigrationPathError(
            f"local-data migration refuses a symlink or junction: {path}"
        )


def _assert_safe_tree(root: Path) -> None:
    if not root.exists():
        return
    _assert_plain_path(root)
    if not root.is_dir():
        raise UnsafeMigrationPathError(f"local-data root is not a directory: {root}")
    pending = [root]
    while pending:
        directory = pending.pop()
        _assert_plain_path(directory)
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path)
                _assert_plain_path(path)
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)
                elif not entry.is_file(follow_symlinks=False):
                    raise UnsafeMigrationPathError(
                        f"local-data migration found an unsupported file: {path}"
                    )


def _tree_has_state(root: Path) -> bool:
    if not root.exists():
        return False
    _assert_safe_tree(root)
    return any(path.is_file() for path in root.rglob("*"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_verified(source: Path, destination: Path) -> None:
    _assert_plain_path(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination, follow_symlinks=False)
    if source.stat().st_size != destination.stat().st_size or _sha256(source) != _sha256(
        destination
    ):
        raise LocalDataMigrationError(f"copied file failed verification: {source}")


def _sqlite_backup_verified(source: Path, destination: Path) -> None:
    """Take a consistent SQLite snapshot and verify both source and result."""

    _assert_plain_path(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_uri = f"file:{source.resolve().as_posix()}?mode=ro"
    source_db = sqlite3.connect(source_uri, uri=True)
    destination_db = sqlite3.connect(destination)
    try:
        source_integrity = source_db.execute("PRAGMA integrity_check").fetchone()
        if source_integrity is None or str(source_integrity[0]).casefold() != "ok":
            raise LocalDataMigrationError(
                f"legacy SQLite database failed integrity_check: {source}"
            )
        source_db.backup(destination_db)
        destination_db.commit()
        destination_integrity = destination_db.execute("PRAGMA integrity_check").fetchone()
        if (
            destination_integrity is None
            or str(destination_integrity[0]).casefold() != "ok"
        ):
            raise LocalDataMigrationError(
                f"migrated SQLite database failed integrity_check: {destination}"
            )
    finally:
        destination_db.close()
        source_db.close()
    if not destination.exists() or destination.stat().st_size <= 0:
        raise LocalDataMigrationError(f"SQLite backup is empty: {destination}")


def _is_sqlite_sidecar(relative: Path, database_relative: Path | None) -> bool:
    if database_relative is None or relative.parent != database_relative.parent:
        return False
    relative_name = relative.name.casefold()
    database_name = database_relative.name.casefold()
    return relative_name in {
        f"{database_name}-journal",
        f"{database_name}-shm",
        f"{database_name}-wal",
    }


def _copy_tree(
    source_root: Path,
    destination_root: Path,
    *,
    database_relative: Path | None,
    database_target: Path | None,
) -> None:
    """Copy one already-validated tree without following links."""

    def copy_directory(source: Path, relative_directory: Path) -> None:
        target_directory = destination_root / relative_directory
        target_directory.mkdir(parents=True, exist_ok=True)
        with os.scandir(source) as entries:
            for entry in entries:
                source_path = Path(entry.path)
                _assert_plain_path(source_path)
                relative = relative_directory / entry.name
                if _is_sqlite_sidecar(relative, database_relative):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    copy_directory(source_path, relative)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    raise UnsafeMigrationPathError(
                        f"local-data migration found an unsupported file: {source_path}"
                    )
                if database_relative is not None and relative == database_relative:
                    target = destination_root / (database_target or database_relative)
                    _sqlite_backup_verified(source_path, target)
                else:
                    _copy_verified(source_path, destination_root / relative)

    copy_directory(source_root, Path())


def _next_backup_path(root: Path, timestamp: str) -> Path:
    base = root / "backups" / f"pre_nivelle_0.4.0_{timestamp}"
    candidate = base
    suffix = 1
    while candidate.exists():
        candidate = base.with_name(f"{base.name}_{suffix}")
        suffix += 1
    return candidate


def _write_marker(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", text=True
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _completed_marker(
    marker: Path, *, source: Path, destination: Path, component: str
) -> dict[str, object] | None:
    if not marker.exists():
        return None
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MigrationConflictError(f"invalid local-data migration marker: {marker}") from exc
    if not isinstance(payload, dict):
        raise MigrationConflictError(f"invalid local-data migration marker: {marker}")
    expected = {
        "status": "completed",
        "migration_version": MIGRATION_VERSION,
        "component": component,
        "source": _normalized_path(source),
        "destination": _normalized_path(destination),
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise MigrationConflictError(
            f"local-data migration marker does not match this installation: {marker}"
        )
    return payload


def _remove_verified_empty_tree(root: Path) -> None:
    """Remove only an empty directory tree immediately before atomic promotion."""

    _assert_safe_tree(root)
    if _tree_has_state(root):
        raise MigrationConflictError(
            f"canonical local-data root gained state during migration: {root}"
        )
    directories = sorted(
        (path for path in root.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for directory in directories:
        directory.rmdir()
    root.rmdir()


def migrate_local_data(
    source: Path,
    destination: Path,
    *,
    component: Literal["core", "link"],
    database_relative: Path | None = None,
    database_target: Path | None = None,
) -> LocalDataMigrationResult:
    """Stage, verify, and atomically promote one legacy local-data root.

    The source tree is never renamed, modified, or deleted. If both roots have
    independent state, migration stops instead of guessing how to merge them.
    """

    source = source.expanduser()
    destination = destination.expanduser()
    if _roots_overlap(source, destination):
        raise LocalDataMigrationError(
            "legacy and canonical local-data roots must not overlap"
        )
    if (database_relative is None) != (database_target is None):
        raise ValueError("database_relative and database_target must be provided together")

    _assert_safe_tree(source)
    _assert_safe_tree(destination)
    marker = destination / MIGRATION_MARKER
    completed = _completed_marker(
        marker, source=source, destination=destination, component=component
    )
    if completed is not None:
        backup_value = completed.get("backup")
        backup = destination / str(backup_value) if backup_value else None
        return LocalDataMigrationResult(
            "already_migrated", destination, marker=marker, backup=backup
        )

    source_has_state = _tree_has_state(source)
    destination_has_state = _tree_has_state(destination)
    if not source_has_state:
        return LocalDataMigrationResult("not_needed", destination)
    if destination_has_state:
        raise MigrationConflictError(
            "legacy and canonical local-data roots both contain state; refusing to merge"
        )
    if (
        database_relative is not None
        and database_target is not None
        and database_relative != database_target
        and (source / database_relative).exists()
        and (source / database_target).exists()
    ):
        raise MigrationConflictError(
            "legacy root contains both old and canonical database files; refusing to choose"
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    _assert_plain_path(destination.parent)
    staging = destination.parent / f".{destination.name}.migration-{uuid4().hex}"
    staging.mkdir(parents=False, exist_ok=False)
    _copy_tree(
        source,
        staging,
        database_relative=database_relative,
        database_target=database_target,
    )

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = _next_backup_path(staging, timestamp)
    _copy_tree(
        source,
        backup,
        database_relative=database_relative,
        database_target=database_relative,
    )
    completed_at = datetime.now(UTC).isoformat()
    backup_relative = backup.relative_to(staging)
    staging_marker = staging / MIGRATION_MARKER
    _write_marker(
        staging_marker,
        {
            "status": "completed",
            "migration_version": MIGRATION_VERSION,
            "component": component,
            "source": _normalized_path(source),
            "destination": _normalized_path(destination),
            "completed_at": completed_at,
            "backup": backup_relative.as_posix(),
            "legacy_source_preserved": True,
        },
    )
    _assert_safe_tree(staging)
    if destination.exists():
        _remove_verified_empty_tree(destination)
    os.replace(staging, destination)

    promoted_marker = destination / MIGRATION_MARKER
    promoted_backup = destination / backup_relative
    if not promoted_marker.is_file() or not promoted_backup.is_dir():
        raise LocalDataMigrationError("promoted local-data migration failed verification")
    return LocalDataMigrationResult(
        "migrated",
        destination,
        marker=promoted_marker,
        backup=promoted_backup,
    )


def migrate_core_data(source: Path, destination: Path) -> LocalDataMigrationResult:
    return migrate_local_data(
        source,
        destination,
        component="core",
        database_relative=Path("database/nozomi.db"),
        database_target=Path("database/nivelle.db"),
    )


def migrate_link_data(source: Path, destination: Path) -> LocalDataMigrationResult:
    return migrate_local_data(source, destination, component="link")


def resolve_data_root(
    *,
    current_environment_variables: tuple[str, ...],
    legacy_environment_variable: str,
    current_default: Path,
    legacy_default: Path,
    component: Literal["core", "link"],
) -> Path:
    """Resolve env overrides, then run the default-root migration once."""

    for variable in current_environment_variables:
        configured = os.environ.get(variable)
        if configured:
            return Path(configured).expanduser()

    legacy_configured = os.environ.get(legacy_environment_variable)
    if legacy_configured:
        warnings.warn(
            f"{legacy_environment_variable} is deprecated; use "
            f"{current_environment_variables[0]} instead.",
            FutureWarning,
            stacklevel=2,
        )
        return Path(legacy_configured).expanduser()

    if component == "core":
        migrate_core_data(legacy_default, current_default)
    else:
        migrate_link_data(legacy_default, current_default)
    return current_default


__all__ = [
    "LocalDataMigrationError",
    "LocalDataMigrationResult",
    "MIGRATION_MARKER",
    "MIGRATION_VERSION",
    "MigrationConflictError",
    "UnsafeMigrationPathError",
    "migrate_core_data",
    "migrate_link_data",
    "migrate_local_data",
    "resolve_data_root",
]
