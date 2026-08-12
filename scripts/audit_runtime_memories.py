"""Read-only audit for a deployed Nivelle Archive database.

The command reports IDs and counts, never full memory content or credentials.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import unicodedata
from pathlib import Path
from typing import Any

import yaml


def normalize_memory_content(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    characters: list[str] = []
    for character in normalized:
        category = unicodedata.category(character)
        if character.isspace() or category.startswith(("P", "Z")):
            characters.append(" ")
        elif not category.startswith("C"):
            characters.append(character)
    return " ".join("".join(characters).split())


def default_data_dir() -> Path:
    configured = os.environ.get("NIVELLE_CORE_DATA_DIR")
    if configured:
        return Path(configured)
    # One-release compatibility fallback for 0.3.1 deployments.
    configured = os.environ.get("NOZOMI_SERVER_DATA_DIR")
    if configured:
        print(
            "warning: NOZOMI_SERVER_DATA_DIR is legacy; use NIVELLE_CORE_DATA_DIR",
            file=sys.stderr,
        )
        return Path(configured)
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "Nivelle" / "NivelleCore"
    return Path.home() / ".local" / "share" / "Nivelle" / "NivelleCore"


def resolve_database_path(data_dir: Path, explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    canonical = data_dir / "database" / "nivelle.db"
    if canonical.is_file():
        return canonical
    # Read-only compatibility for auditing an unmigrated 0.3.1 database.
    legacy = data_dir / "database" / "nozomi.db"
    if legacy.is_file():
        print(
            "warning: auditing a legacy 0.3.1 database; migrate it before Nivelle Core starts",
            file=sys.stderr,
        )
        return legacy
    return canonical


def table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _load_mapping(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    value = yaml.safe_load(path.read_text("utf-8"))
    return value if isinstance(value, dict) else {}


def _is_negative_capability_statement(content: str) -> bool:
    normalized = normalize_memory_content(content)
    return any(
        marker in normalized
        for marker in (
            "없다",
            "없음",
            "미사용",
            "미구현",
            "미등록",
            "비활성",
            "적용되지 않았다",
            "등록되어 있지 않다",
            "설정되어 있지 않다",
            "not configured",
            "not available",
            "disabled",
        )
    )


def _runtime_conflicts(
    memories: list[sqlite3.Row], *, has_v4: bool, data_dir: Path
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    models = _load_mapping(data_dir / "config" / "models.yaml")
    memory_settings = _load_mapping(data_dir / "config" / "memory.yaml")
    configured_models = models.get("models")
    fallback_enabled = models.get("fallback_enabled") is True
    enabled_fallbacks = [
        model
        for model in configured_models
        if isinstance(model, dict)
        and fallback_enabled
        and model.get("role") == "fallback"
        and model.get("enabled", True) is True
    ] if isinstance(configured_models, list) else []
    embedding_provider = memory_settings.get("embedding_provider")
    search_backend = memory_settings.get("search_backend", "sqlite")
    conflicts: list[dict[str, str]] = []
    for row in memories:
        eligible = (
            bool(row["explicitly_saved"])
            and bool(row["active"])
            and (not has_v4 or row["superseded_by"] is None)
        )
        if not eligible:
            continue
        content = str(row["content"])
        normalized = normalize_memory_content(content)
        if _is_negative_capability_statement(content):
            continue
        memory_id = str(row["id"])
        if not enabled_fallbacks and any(
            marker in normalized for marker in ("fallback", "대체 모델", "대체모델")
        ):
            conflicts.append(
                {"memory_id": memory_id, "reason": "claims_unconfigured_fallback_model"}
            )
        if embedding_provider is None and any(
            marker in normalized
            for marker in ("qwen3 embedding", "qwen3임베딩", "임베딩", "embedding")
        ):
            conflicts.append(
                {"memory_id": memory_id, "reason": "claims_unavailable_embedding"}
            )
        if str(search_backend).casefold() == "sqlite" and any(
            marker in normalized for marker in ("sqlite vec", "sqlitevec", "벡터 검색")
        ):
            conflicts.append(
                {"memory_id": memory_id, "reason": "claims_unavailable_sqlite_vec"}
            )
    conflicts.sort(key=lambda item: (item["memory_id"], item["reason"]))
    return conflicts, {
        "fallback_enabled": fallback_enabled,
        "enabled_fallback_model_count": len(enabled_fallbacks),
        "embedding_provider": embedding_provider,
        "search_backend": search_backend,
    }


def audit(database_path: Path, *, data_dir: Path | None = None) -> dict[str, Any]:
    uri = f"file:{database_path.resolve().as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        integrity_row = connection.execute("PRAGMA integrity_check").fetchone()
        integrity = str(integrity_row[0]) if integrity_row else "no_result"
        schema_row = connection.execute(
            "SELECT COALESCE(MAX(version),0) FROM schema_versions"
        ).fetchone()
        schema_version = int(schema_row[0]) if schema_row else 0
        columns = table_columns(connection, "memories")
        has_v4 = {"normalized_content", "superseded_by", "superseded_at"} <= columns
        select_columns = "id,content,active,explicitly_saved"
        if has_v4:
            select_columns += ",normalized_content,superseded_by"
        memories = list(connection.execute(f"SELECT {select_columns} FROM memories"))

        active = sum(
            1
            for row in memories
            if bool(row["explicitly_saved"])
            and bool(row["active"])
            and (not has_v4 or row["superseded_by"] is None)
        )
        inactive = sum(
            1
            for row in memories
            if bool(row["explicitly_saved"]) and not bool(row["active"])
        )
        superseded = (
            sum(1 for row in memories if row["superseded_by"] is not None) if has_v4 else 0
        )
        normalized_mismatch_ids: list[str] = []
        duplicate_groups: dict[str, list[str]] = {}
        for row in memories:
            canonical = normalize_memory_content(str(row["content"]))
            if has_v4 and str(row["normalized_content"]) != canonical:
                normalized_mismatch_ids.append(str(row["id"]))
            if (
                bool(row["explicitly_saved"])
                and bool(row["active"])
                and (not has_v4 or row["superseded_by"] is None)
            ):
                duplicate_groups.setdefault(canonical, []).append(str(row["id"]))
        active_duplicate_ids = [
            sorted(identifiers)
            for identifiers in duplicate_groups.values()
            if len(identifiers) > 1
        ]

        revision_count = 0
        if "memory_revisions" in {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }:
            revision_count = int(
                connection.execute("SELECT COUNT(*) FROM memory_revisions").fetchone()[0]
            )
        fts_present = (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memories_fts'"
            ).fetchone()
            is not None
        )
        message_columns = table_columns(connection, "messages")
        message_indexes = {
            str(row[1]) for row in connection.execute("PRAGMA index_list(messages)")
        }
        runtime_root = data_dir or database_path.parent.parent
        runtime_conflicts, runtime_configuration = _runtime_conflicts(
            memories, has_v4=has_v4, data_dir=runtime_root
        )
        issues: list[str] = []
        if integrity.casefold() != "ok":
            issues.append("database_integrity_failed")
        if not has_v4:
            issues.append("memory_schema_v4_missing")
        if (
            "client_message_id" not in message_columns
            or "uq_messages_client_message_id" not in message_indexes
        ):
            issues.append("message_idempotency_v5_missing")
        if (
            "retry_of_client_message_id" not in message_columns
            or "uq_messages_retry_target" not in message_indexes
        ):
            issues.append("controlled_retry_v6_missing")
        if (
            "request_id" not in message_columns
            or "uq_messages_request_id" not in message_indexes
        ):
            issues.append("request_identity_v7_missing")
        if normalized_mismatch_ids:
            issues.append("normalized_content_mismatch")
        if active_duplicate_ids:
            issues.append("active_normalized_duplicates")
        if runtime_conflicts:
            issues.append("runtime_memory_conflict")
        return {
            "database": str(database_path.resolve()),
            "integrity_check": integrity,
            "schema_version": schema_version,
            "memory_counts": {
                "total_rows": len(memories),
                "active": active,
                "inactive": inactive,
                "superseded": superseded,
                "revisions": revision_count,
            },
            "search": {
                "fts5_index_present": fts_present,
                "embedding_provider": None,
                "backend": "sqlite_hybrid" if has_v4 else "legacy_sqlite",
            },
            "normalized_mismatch_ids": sorted(normalized_mismatch_ids),
            "active_duplicate_id_groups": active_duplicate_ids,
            "runtime_configuration": runtime_configuration,
            "runtime_conflicts": runtime_conflicts,
            "issues": issues,
        }
    finally:
        connection.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument(
        "--strict", action="store_true", help="return exit code 2 when audit issues exist"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    data_dir = args.data_dir or (
        args.database.parent.parent if args.database is not None else default_data_dir()
    )
    database_path = resolve_database_path(data_dir, args.database)
    if not database_path.is_file():
        print(f"Nivelle Core database was not found: {database_path}", file=sys.stderr)
        return 2
    try:
        result = audit(database_path, data_dir=data_dir)
    except sqlite3.Error as exc:
        print(f"Nivelle Core database audit failed: {exc}", file=sys.stderr)
        return 2
    if args.as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Database: {result['database']}")
        print(f"Integrity: {result['integrity_check']}")
        print(f"Schema version: {result['schema_version']}")
        print(f"Memory counts: {result['memory_counts']}")
        print(f"Search: {result['search']}")
        print(f"Normalized mismatch IDs: {result['normalized_mismatch_ids']}")
        print(f"Active duplicate ID groups: {result['active_duplicate_id_groups']}")
        print(f"Runtime configuration: {result['runtime_configuration']}")
        print(f"Runtime conflicts: {result['runtime_conflicts']}")
        print(f"Issues: {result['issues'] or 'none'}")
    return 2 if args.strict and result["issues"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
