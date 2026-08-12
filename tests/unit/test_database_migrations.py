import sqlite3
from pathlib import Path

import pytest
from nivelle_core.database import MIGRATIONS, Database


def _create_v3_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE schema_versions(version INTEGER PRIMARY KEY,applied_at TEXT NOT NULL)"
        )
        for version, sql in MIGRATIONS:
            if version > 3:
                break
            connection.executescript(sql)
            connection.execute(
                "INSERT INTO schema_versions(version,applied_at) VALUES(?,datetime('now'))",
                (version,),
            )
        rows = (
            ("canonical", "사용자의 기본 호칭은 히냥이이다.", 70, "2026-01-02T00:00:00+00:00"),
            ("duplicate", "  사용자의 기본 호칭은 히냥이이다！  ", 60, "2026-01-01T00:00:00+00:00"),
        )
        for memory_id, content, priority, updated_at in rows:
            connection.execute(
                """
                INSERT INTO memories(
                    id,content,category,active,priority,explicitly_saved,created_at,updated_at
                ) VALUES(?,?,?,1,?,1,?,?)
                """,
                (memory_id, content, "preference", priority, updated_at, updated_at),
            )
        connection.commit()
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_old_schema_migration_backs_up_repairs_and_preserves_ids(tmp_path: Path) -> None:
    database_path = tmp_path / "nivelle.db"
    _create_v3_database(database_path)

    database = Database(database_path)
    await database.initialize()

    assert database.last_migration_backup is not None
    assert database.last_migration_backup.stat().st_size > 0
    backup = sqlite3.connect(database.last_migration_backup)
    try:
        assert backup.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert backup.execute("SELECT MAX(version) FROM schema_versions").fetchone() == (3,)
    finally:
        backup.close()

    versions = await database.fetchall("SELECT version FROM schema_versions ORDER BY version")
    assert [row["version"] for row in versions] == [1, 2, 3, 4, 5, 6, 7, 8]
    rows = await database.fetchall(
        """
        SELECT id,content,normalized_content,active,superseded_by
        FROM memories ORDER BY id
        """
    )
    assert {row["id"] for row in rows} == {"canonical", "duplicate"}
    assert next(row for row in rows if row["id"] == "canonical")["active"] == 1
    duplicate = next(row for row in rows if row["id"] == "duplicate")
    assert duplicate["active"] == 0
    assert duplicate["superseded_by"] == "canonical"
    assert "히냥이" in duplicate["content"]
    revisions = await database.fetchall(
        "SELECT memory_id,change_source FROM memory_revisions"
    )
    assert [(row["memory_id"], row["change_source"]) for row in revisions] == [
        ("duplicate", "migration_v4")
    ]
    message_columns = await database.fetchall("PRAGMA table_info(messages)")
    message_column_names = {row["name"] for row in message_columns}
    assert "client_message_id" in message_column_names
    assert "retry_of_client_message_id" in message_column_names
    assert "request_id" in message_column_names
    indexes = await database.fetchall("PRAGMA index_list(messages)")
    index_names = {row["name"] for row in indexes}
    assert "uq_messages_client_message_id" in index_names
    assert "uq_messages_retry_target" in index_names
    assert "uq_messages_request_id" in index_names

    second_start = Database(database_path)
    await second_start.initialize()
    assert second_start.last_migration_backup is None
    assert [
        row["version"]
        for row in await second_start.fetchall(
            "SELECT version FROM schema_versions ORDER BY version"
        )
    ] == [1, 2, 3, 4, 5, 6, 7, 8]


@pytest.mark.asyncio
async def test_v7_backfills_only_first_historical_request_id_duplicate(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "nivelle.db"
    database = Database(database_path)
    await database.initialize()

    # Recreate the exact message-related shape expected immediately before v7.
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("DROP INDEX uq_messages_request_id")
        connection.execute("ALTER TABLE messages DROP COLUMN request_id")
        connection.execute("DELETE FROM schema_versions WHERE version=7")
        connection.execute(
            """
            INSERT INTO conversations(id,title,created_at,updated_at)
            VALUES('conversation','history','2026-01-01','2026-01-01')
            """
        )
        historical_messages = (
            (
                "user-first",
                "user",
                "2026-01-01T00:00:00+00:00",
                '{"request_id":"shared-request"}',
            ),
            (
                "user-duplicate",
                "user",
                "2026-01-01T00:00:01+00:00",
                '{"request_id":"shared-request"}',
            ),
            (
                "user-unique",
                "user",
                "2026-01-01T00:00:02+00:00",
                '{"request_id":"unique-request"}',
            ),
            (
                "assistant-history",
                "assistant",
                "2026-01-01T00:00:03+00:00",
                '{"request_id":"assistant-metadata-is-not-a-user-request"}',
            ),
        )
        connection.executemany(
            """
            INSERT INTO messages(
                id,conversation_id,role,content,created_at,state,metadata_json
            ) VALUES(?,'conversation',?,'history',?,'completed',?)
            """,
            historical_messages,
        )
        connection.commit()
    finally:
        connection.close()

    migrated = Database(database_path)
    await migrated.initialize()

    rows = await migrated.fetchall(
        "SELECT id,request_id FROM messages ORDER BY created_at,id"
    )
    assert [(row["id"], row["request_id"]) for row in rows] == [
        ("user-first", "shared-request"),
        ("user-duplicate", None),
        ("user-unique", "unique-request"),
        ("assistant-history", None),
    ]
    versions = await migrated.fetchall(
        "SELECT version FROM schema_versions ORDER BY version"
    )
    assert [row["version"] for row in versions] == [1, 2, 3, 4, 5, 6, 7, 8]
    indexes = await migrated.fetchall("PRAGMA index_list(messages)")
    assert "uq_messages_request_id" in {row["name"] for row in indexes}

    connection = sqlite3.connect(database_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO messages(
                    id,conversation_id,role,content,created_at,state,
                    metadata_json,request_id
                ) VALUES(
                    'new-duplicate','conversation','user','new',
                    '2026-01-02','completed','{}','shared-request'
                )
                """
            )
    finally:
        connection.close()
