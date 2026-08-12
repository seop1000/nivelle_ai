import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from .memory_retriever import normalize_memory_content

SCHEMA_VERSION_TABLE = """
CREATE TABLE IF NOT EXISTS schema_versions(
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
"""

MIGRATIONS: tuple[tuple[int, str], ...] = (
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS clients(id TEXT PRIMARY KEY,name TEXT NOT NULL,token_hash TEXT NOT NULL,token_salt TEXT NOT NULL,created_at TEXT NOT NULL,last_seen_at TEXT,revoked_at TEXT,is_admin INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY,title TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,archived_at TEXT);
        CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL REFERENCES conversations(id),role TEXT NOT NULL,content TEXT NOT NULL,created_at TEXT NOT NULL,state TEXT NOT NULL,prompt_tokens INTEGER,completion_tokens INTEGER,metadata_json TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE IF NOT EXISTS settings_revisions(id INTEGER PRIMARY KEY AUTOINCREMENT,section TEXT NOT NULL,previous_json TEXT NOT NULL,new_json TEXT NOT NULL,created_at TEXT NOT NULL,client_id TEXT,apply_status TEXT NOT NULL,error_message TEXT);
        CREATE TABLE IF NOT EXISTS runtime_samples(id INTEGER PRIMARY KEY AUTOINCREMENT,created_at TEXT NOT NULL,model_id TEXT,prompt_tokens INTEGER,completion_tokens INTEGER,tokens_per_second REAL,latency_ms REAL,metadata_json TEXT NOT NULL DEFAULT '{}');
        """,
    ),
    (
        2,
        """
        CREATE TABLE IF NOT EXISTS memories(
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL CHECK(length(content) BETWEEN 1 AND 500),
            category TEXT NOT NULL CHECK(category IN ('preference','project','workflow','instruction','other')),
            active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
            priority INTEGER NOT NULL DEFAULT 50 CHECK(priority BETWEEN 0 AND 100),
            explicitly_saved INTEGER NOT NULL DEFAULT 1 CHECK(explicitly_saved IN (0,1)),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_memories_prompt
            ON memories(explicitly_saved, active, priority DESC, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_memories_category ON memories(category);
        """,
    ),
    (
        3,
        """
        CREATE INDEX IF NOT EXISTS idx_messages_conversation_history
            ON messages(conversation_id,state,created_at DESC,id DESC);
        """,
    ),
    (
        4,
        """
        CREATE TABLE IF NOT EXISTS memory_revisions(
            revision_id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id TEXT NOT NULL,
            old_content TEXT NOT NULL,
            new_content TEXT NOT NULL,
            changed_at TEXT NOT NULL,
            change_source TEXT NOT NULL,
            reason TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_memory_revisions_memory
            ON memory_revisions(memory_id, changed_at DESC, revision_id DESC);
        """,
    ),
    (
        5,
        """
        SELECT 1;
        """,
    ),
    (
        6,
        """
        SELECT 1;
        """,
    ),
    (
        7,
        """
        SELECT 1;
        """,
    ),
    (
        8,
        """
        SELECT 1;
        """,
    ),
)

LATEST_SCHEMA_VERSION = MIGRATIONS[-1][0]

# Kept for compatibility with code that imported the original schema constant.
SCHEMA = SCHEMA_VERSION_TABLE + "\n" + "\n".join(sql for _, sql in MIGRATIONS)

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content,
    category,
    content='memories',
    content_rowid='rowid',
    tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS memories_fts_insert AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid,content,category)
    VALUES(new.rowid,new.content,new.category);
END;
CREATE TRIGGER IF NOT EXISTS memories_fts_delete AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts,rowid,content,category)
    VALUES('delete',old.rowid,old.content,old.category);
END;
CREATE TRIGGER IF NOT EXISTS memories_fts_update AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts,rowid,content,category)
    VALUES('delete',old.rowid,old.content,old.category);
    INSERT INTO memories_fts(rowid,content,category)
    VALUES(new.rowid,new.content,new.category);
END;
"""

TRIGRAM_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS memories_trigram USING fts5(
    content,
    category,
    content='memories',
    content_rowid='rowid',
    tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS memories_trigram_insert AFTER INSERT ON memories BEGIN
    INSERT INTO memories_trigram(rowid,content,category)
    VALUES(new.rowid,new.content,new.category);
END;
CREATE TRIGGER IF NOT EXISTS memories_trigram_delete AFTER DELETE ON memories BEGIN
    INSERT INTO memories_trigram(memories_trigram,rowid,content,category)
    VALUES('delete',old.rowid,old.content,old.category);
END;
CREATE TRIGGER IF NOT EXISTS memories_trigram_update AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_trigram(memories_trigram,rowid,content,category)
    VALUES('delete',old.rowid,old.content,old.category);
    INSERT INTO memories_trigram(rowid,content,category)
    VALUES(new.rowid,new.content,new.category);
END;
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.fts_available = False
        self.trigram_available = False
        self.last_migration_backup: Path | None = None

    async def initialize(self) -> None:
        database_existed = self.path.exists() and self.path.stat().st_size > 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            await db.executescript(SCHEMA_VERSION_TABLE)
            version_cursor = await db.execute("SELECT COALESCE(MAX(version),0) FROM schema_versions")
            version_row = await version_cursor.fetchone()
            await version_cursor.close()
            initial_version = int(version_row[0]) if version_row else 0
            if database_existed and initial_version < LATEST_SCHEMA_VERSION:
                self.last_migration_backup = await self._backup_before_migration(
                    db, target_version=LATEST_SCHEMA_VERSION
                )
            for version, migration in MIGRATIONS:
                cursor = await db.execute(
                    "SELECT 1 FROM schema_versions WHERE version=?", (version,)
                )
                already_applied = await cursor.fetchone()
                await cursor.close()
                if already_applied:
                    continue
                if version in {4, 5, 6, 7, 8}:
                    await self._apply_transactional_migration(db, version)
                    continue
                await db.executescript(migration)
                await db.execute(
                    "INSERT INTO schema_versions(version,applied_at) VALUES(?,datetime('now'))",
                    (version,),
                )
                await db.commit()
            try:
                await db.executescript(FTS_SCHEMA)
                # Rebuild also covers databases created by a pre-FTS development build.
                await db.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
                await db.commit()
                self.fts_available = True
            except aiosqlite.OperationalError:
                # Some embedded SQLite builds omit FTS5. Repository search safely falls back to LIKE.
                await db.rollback()
                self.fts_available = False
            if self.fts_available:
                try:
                    # The trigram tokenizer is an optional FTS5 build feature.
                    # Probe it by creating the real bounded search index and
                    # fall back cleanly when the deployed SQLite omits it.
                    await db.executescript(TRIGRAM_FTS_SCHEMA)
                    await db.execute(
                        "INSERT INTO memories_trigram(memories_trigram) VALUES('rebuild')"
                    )
                    await db.commit()
                    self.trigram_available = True
                except aiosqlite.OperationalError:
                    await db.rollback()
                    self.trigram_available = False

    async def _backup_before_migration(
        self, db: aiosqlite.Connection, *, target_version: int
    ) -> Path:
        """Create one consistent SQLite backup before changing an old schema."""

        backup_root = (
            self.path.parent.parent / "backups"
            if self.path.parent.name.casefold() == "database"
            else self.path.parent / "backups"
        )
        backup_root.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup_path = backup_root / f"{self.path.stem}.pre-v{target_version}.{timestamp}.db"
        suffix = 1
        while backup_path.exists():
            backup_path = backup_root / (
                f"{self.path.stem}.pre-v{target_version}.{timestamp}.{suffix}.db"
            )
            suffix += 1
        destination = sqlite3.connect(backup_path)
        try:
            await db.backup(destination)
        finally:
            destination.close()
        if not backup_path.exists() or backup_path.stat().st_size <= 0:
            raise RuntimeError(f"database migration backup is empty: {backup_path}")
        verifier = sqlite3.connect(f"file:{backup_path.as_posix()}?mode=ro", uri=True)
        try:
            integrity = verifier.execute("PRAGMA integrity_check").fetchone()
        finally:
            verifier.close()
        if integrity is None or str(integrity[0]).casefold() != "ok":
            raise RuntimeError(f"database migration backup failed integrity_check: {backup_path}")
        return backup_path

    async def _apply_transactional_migration(
        self, db: aiosqlite.Connection, version: int
    ) -> None:
        """Apply data-sensitive migrations and their version marker atomically."""

        await db.execute("BEGIN IMMEDIATE")
        try:
            if version == 4:
                await db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS memory_revisions(
                        revision_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        memory_id TEXT NOT NULL,
                        old_content TEXT NOT NULL,
                        new_content TEXT NOT NULL,
                        changed_at TEXT NOT NULL,
                        change_source TEXT NOT NULL,
                        reason TEXT
                    )
                    """
                )
                await db.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_memory_revisions_memory
                    ON memory_revisions(memory_id,changed_at DESC,revision_id DESC)
                    """
                )
                await self._migrate_memory_v4(db)
            elif version == 5:
                await self._migrate_messages_v5(db)
            elif version == 6:
                await self._migrate_messages_v6(db)
            elif version == 7:
                await self._migrate_messages_v7(db)
            elif version == 8:
                await self._migrate_tools_v8(db)
            else:  # pragma: no cover - caller is intentionally closed over known versions
                raise ValueError(f"unsupported transactional migration: {version}")
            await db.execute(
                "INSERT INTO schema_versions(version,applied_at) VALUES(?,datetime('now'))",
                (version,),
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    @staticmethod
    async def _column_names(db: aiosqlite.Connection, table: str) -> set[str]:
        cursor = await db.execute(f"PRAGMA table_info({table})")
        rows = await cursor.fetchall()
        await cursor.close()
        return {str(row[1]) for row in rows}

    async def _migrate_memory_v4(self, db: aiosqlite.Connection) -> None:
        """Add canonical content/history and repair old active duplicates safely."""

        columns = await self._column_names(db, "memories")
        if "normalized_content" not in columns:
            await db.execute(
                "ALTER TABLE memories ADD COLUMN normalized_content TEXT NOT NULL DEFAULT ''"
            )
        if "superseded_by" not in columns:
            await db.execute("ALTER TABLE memories ADD COLUMN superseded_by TEXT")
        if "superseded_at" not in columns:
            await db.execute("ALTER TABLE memories ADD COLUMN superseded_at TEXT")

        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT id,content,active,explicitly_saved,priority,created_at,updated_at,
                   superseded_by
            FROM memories
            ORDER BY priority DESC, updated_at DESC, created_at, id
            """
        )
        rows = list(await cursor.fetchall())
        await cursor.close()
        canonical_by_content: dict[str, str] = {}
        repaired_at = datetime.now(UTC).isoformat()
        for row in rows:
            normalized = normalize_memory_content(str(row["content"]))
            await db.execute(
                "UPDATE memories SET normalized_content=? WHERE id=?",
                (normalized, str(row["id"])),
            )
            eligible = (
                bool(row["explicitly_saved"])
                and bool(row["active"])
                and row["superseded_by"] is None
            )
            if not eligible:
                continue
            canonical_id = canonical_by_content.get(normalized)
            if canonical_id is None:
                canonical_by_content[normalized] = str(row["id"])
                continue
            memory_id = str(row["id"])
            await db.execute(
                """
                UPDATE memories
                SET active=0,superseded_by=?,superseded_at=?
                WHERE id=?
                """,
                (canonical_id, repaired_at, memory_id),
            )
            await db.execute(
                """
                INSERT INTO memory_revisions(
                    memory_id,old_content,new_content,changed_at,change_source,reason
                )
                SELECT ?,?,?,?,?,?
                WHERE NOT EXISTS(
                    SELECT 1 FROM memory_revisions
                    WHERE memory_id=? AND change_source='migration_v4'
                )
                """,
                (
                    memory_id,
                    str(row["content"]),
                    str(row["content"]),
                    repaired_at,
                    "migration_v4",
                    f"exact duplicate superseded by {canonical_id}",
                    memory_id,
                ),
            )
        await db.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_memories_active_normalized
            ON memories(normalized_content)
            WHERE explicitly_saved=1 AND active=1 AND superseded_by IS NULL
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_memories_retrieval
            ON memories(explicitly_saved,active,superseded_by,priority DESC,updated_at DESC)
            """
        )

    async def _migrate_messages_v5(self, db: aiosqlite.Connection) -> None:
        """Reserve a globally unique client message key for reconnect idempotency."""

        columns = await self._column_names(db, "messages")
        if "client_message_id" not in columns:
            await db.execute("ALTER TABLE messages ADD COLUMN client_message_id TEXT")
        await db.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_messages_client_message_id
            ON messages(client_message_id)
            WHERE client_message_id IS NOT NULL
            """
        )

    async def _migrate_messages_v6(self, db: aiosqlite.Connection) -> None:
        """Persist one controlled retry relationship per original request."""

        columns = await self._column_names(db, "messages")
        if "retry_of_client_message_id" not in columns:
            await db.execute(
                "ALTER TABLE messages ADD COLUMN retry_of_client_message_id TEXT"
            )
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT id,metadata_json FROM messages
            WHERE role='user' AND retry_of_client_message_id IS NULL
            ORDER BY created_at,id
            """
        )
        rows = await cursor.fetchall()
        await cursor.close()
        seen_retry_targets: set[str] = set()
        for row in rows:
            try:
                metadata = json.loads(str(row["metadata_json"] or "{}"))
            except (json.JSONDecodeError, TypeError):
                continue
            retry_of = (
                metadata.get("retry_of_client_message_id")
                if isinstance(metadata, dict)
                else None
            )
            if not retry_of or str(retry_of) in seen_retry_targets:
                continue
            seen_retry_targets.add(str(retry_of))
            await db.execute(
                "UPDATE messages SET retry_of_client_message_id=? WHERE id=?",
                (str(retry_of), str(row["id"])),
            )
        await db.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_messages_retry_target
            ON messages(retry_of_client_message_id)
            WHERE retry_of_client_message_id IS NOT NULL
            """
        )

    async def _migrate_messages_v7(self, db: aiosqlite.Connection) -> None:
        """Persist a unique user-turn request ID across sockets and restarts."""

        columns = await self._column_names(db, "messages")
        if "request_id" not in columns:
            await db.execute("ALTER TABLE messages ADD COLUMN request_id TEXT")
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT id,metadata_json FROM messages
            WHERE role='user' AND request_id IS NULL
            ORDER BY created_at,id
            """
        )
        rows = await cursor.fetchall()
        await cursor.close()
        seen_request_ids: set[str] = set()
        for row in rows:
            try:
                metadata = json.loads(str(row["metadata_json"] or "{}"))
            except (json.JSONDecodeError, TypeError):
                continue
            request_id = metadata.get("request_id") if isinstance(metadata, dict) else None
            normalized = str(request_id or "").strip()
            if not normalized or normalized in seen_request_ids:
                continue
            seen_request_ids.add(normalized)
            await db.execute(
                "UPDATE messages SET request_id=? WHERE id=?",
                (normalized, str(row["id"])),
            )
        await db.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_messages_request_id
            ON messages(request_id)
            WHERE role='user' AND request_id IS NOT NULL
            """
        )

    async def _migrate_tools_v8(self, db: aiosqlite.Connection) -> None:
        """Add the durable Phase 3 tool state, capability, and replay ledger."""

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS tool_calls(
                tool_call_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                conversation_id TEXT NOT NULL REFERENCES conversations(id),
                user_message_id TEXT NOT NULL REFERENCES messages(id),
                assistant_message_id TEXT REFERENCES messages(id),
                target_client_id TEXT NOT NULL,
                target_session_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                tool_version TEXT NOT NULL,
                risk_level TEXT NOT NULL CHECK(risk_level IN (
                    'SAFE_STATUS','LOCAL_READ','INTERACTIVE','LOCAL_WRITE',
                    'UNSUPPORTED_DANGEROUS'
                )),
                arguments_summary TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL CHECK(status IN (
                    'proposed','validated','awaiting_approval','approved',
                    'queued','running','completed','validation_failed','denied',
                    'timed_out','cancelled','failed','client_disconnected'
                )),
                approval_mode TEXT NOT NULL CHECK(approval_mode IN (
                    'not_required','deny','allow_once','allow_session',
                    'allow_always_exact'
                )),
                approved_at TEXT,
                started_at TEXT,
                completed_at TEXT,
                duration_ms INTEGER CHECK(duration_ms IS NULL OR duration_ms >= 0),
                result_summary TEXT,
                error_code TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_tool_calls_request
            ON tool_calls(request_id,created_at,tool_call_id)
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_tool_calls_target_status
            ON tool_calls(target_client_id,target_session_id,status,updated_at)
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS tool_call_events(
                event_id TEXT PRIMARY KEY,
                tool_call_id TEXT NOT NULL REFERENCES tool_calls(tool_call_id)
                    ON DELETE CASCADE,
                request_id TEXT NOT NULL,
                sequence INTEGER NOT NULL CHECK(sequence > 0),
                event_type TEXT NOT NULL,
                from_status TEXT,
                to_status TEXT NOT NULL,
                target_client_id TEXT NOT NULL,
                target_session_id TEXT NOT NULL,
                safe_summary TEXT NOT NULL DEFAULT '',
                error_code TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(tool_call_id,sequence)
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_tool_call_events_call
            ON tool_call_events(tool_call_id,sequence)
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS client_capabilities(
                client_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                app_version TEXT NOT NULL,
                protocol_version TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                tool_version TEXT NOT NULL,
                enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
                implementation_available INTEGER NOT NULL
                    CHECK(implementation_available IN (0,1)),
                risk_level TEXT NOT NULL,
                default_approval_required INTEGER NOT NULL
                    CHECK(default_approval_required IN (0,1)),
                default_timeout_ms INTEGER NOT NULL CHECK(default_timeout_ms > 0),
                maximum_timeout_ms INTEGER NOT NULL
                    CHECK(maximum_timeout_ms >= default_timeout_ms),
                maximum_result_size INTEGER NOT NULL CHECK(maximum_result_size > 0),
                connected_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                disconnected_at TEXT,
                PRIMARY KEY(client_id,session_id,tool_name,tool_version)
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_client_capabilities_expiry
            ON client_capabilities(client_id,session_id,expires_at,disconnected_at)
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS tool_idempotency(
                idempotency_key TEXT PRIMARY KEY,
                tool_call_id TEXT NOT NULL UNIQUE REFERENCES tool_calls(tool_call_id)
                    ON DELETE CASCADE,
                request_id TEXT NOT NULL,
                target_client_id TEXT NOT NULL,
                target_session_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                outcome_status TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_tool_idempotency_expiry
            ON tool_idempotency(expires_at)
            """
        )

    async def execute(self, sql: str, args: tuple[Any, ...] = ()) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute(sql, args)
            await db.commit()

    async def fetchone(self, sql: str, args: tuple[Any, ...] = ()) -> aiosqlite.Row | None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, args) as cursor:
                return await cursor.fetchone()

    async def fetchall(self, sql: str, args: tuple[Any, ...] = ()) -> list[aiosqlite.Row]:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, args) as cursor:
                return list(await cursor.fetchall())
