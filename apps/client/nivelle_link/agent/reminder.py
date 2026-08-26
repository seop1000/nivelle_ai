from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from contextlib import closing
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from nivelle_protocol.tools import SetReminderArguments
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .errors import AgentError
from .models import AgentToolRequest

REMINDER_SCHEMA_VERSION = 1
BUSINESS_RECONCILIATION_RETENTION = timedelta(days=7)


class ReminderInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    reminder_text: str = Field(min_length=1, max_length=10_000)
    scheduled_at: str = Field(min_length=1, max_length=100)
    timezone: str = Field(min_length=1, max_length=100)

    @field_validator("title", "reminder_text")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Reminder text cannot be blank")
        return value


class ReminderStore:
    """Local notification records only; it never invokes Task Scheduler."""

    def __init__(
        self,
        path: Path,
        *,
        now: Any | None = None,
        reconciliation_retention: timedelta = BUSINESS_RECONCILIATION_RETENTION,
    ) -> None:
        self.path = path
        self._now = now or (lambda: datetime.now(UTC))
        self.reconciliation_retention = reconciliation_retention
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            version_row = connection.execute("PRAGMA user_version").fetchone()
            current_version = int(version_row[0]) if version_row is not None else 0
            if current_version not in {0, REMINDER_SCHEMA_VERSION}:
                raise AgentError(
                    "execution_failed", "The local reminder database version is unsupported."
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS reminders (
                    reminder_id TEXT PRIMARY KEY,
                    idempotency_key_hash TEXT NOT NULL UNIQUE,
                    request_fingerprint TEXT NOT NULL,
                    title TEXT NOT NULL,
                    reminder_text TEXT NOT NULL,
                    scheduled_at_utc TEXT NOT NULL,
                    scheduled_at_local TEXT NOT NULL,
                    timezone TEXT NOT NULL,
                    origin_conversation_id TEXT NOT NULL,
                    origin_request_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_reminders_request_fingerprint
                ON reminders (request_fingerprint)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS reminder_idempotency_aliases (
                    idempotency_key_hash TEXT PRIMARY KEY,
                    request_fingerprint TEXT NOT NULL,
                    reminder_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (reminder_id) REFERENCES reminders (reminder_id)
                )
                """
            )
            if current_version == 0:
                connection.execute(f"PRAGMA user_version = {REMINDER_SCHEMA_VERSION}")
            connection.commit()

    @staticmethod
    def _hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _fingerprint(
        arguments: SetReminderArguments | ReminderInput, conversation_id: str
    ) -> str:
        payload = arguments.model_dump(mode="json") | {"conversation_id": conversation_id}
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()

    def _parse_schedule(
        self, arguments: SetReminderArguments | ReminderInput
    ) -> tuple[datetime, datetime]:
        zone: tzinfo
        if arguments.timezone == "Asia/Seoul":
            zone = timezone(timedelta(hours=9), name="Asia/Seoul")
        elif arguments.timezone in {"UTC", "Etc/UTC"}:
            zone = UTC
        else:
            try:
                zone = ZoneInfo(arguments.timezone)
            except ZoneInfoNotFoundError as exc:
                raise AgentError("validation_failed", "The reminder timezone is invalid.") from exc
        if isinstance(arguments.scheduled_at, datetime):
            parsed = arguments.scheduled_at
        else:
            try:
                parsed = datetime.fromisoformat(arguments.scheduled_at)
            except ValueError as exc:
                raise AgentError("validation_failed", "The reminder time is invalid.") from exc
        if parsed.tzinfo is None:
            local = parsed.replace(tzinfo=zone)
            round_trip = local.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
            if round_trip != parsed:
                raise AgentError(
                    "validation_failed", "The reminder time does not exist in that timezone."
                )
        else:
            local = parsed.astimezone(zone)
        scheduled_utc = local.astimezone(UTC)
        current = self._now()
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        if scheduled_utc <= current.astimezone(UTC):
            raise AgentError("validation_failed", "The reminder time must be in the future.")
        return scheduled_utc, local

    @staticmethod
    def _public_result(row: sqlite3.Row, *, replayed: bool) -> dict[str, Any]:
        return {
            "reminder_id": row["reminder_id"],
            "title": row["title"],
            "scheduled_at": row["scheduled_at_local"],
            "timezone": row["timezone"],
            "origin_conversation_id": row["origin_conversation_id"],
            "replayed": replayed,
        }

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    def _existing_for_key(
        self, connection: sqlite3.Connection, key_hash: str
    ) -> sqlite3.Row | None:
        existing = cast(
            sqlite3.Row | None,
            connection.execute(
                "SELECT * FROM reminders WHERE idempotency_key_hash = ?", (key_hash,)
            ).fetchone(),
        )
        if existing is not None:
            return existing
        return cast(
            sqlite3.Row | None,
            connection.execute(
                """
                SELECT reminders.*
                FROM reminder_idempotency_aliases AS aliases
                JOIN reminders ON reminders.reminder_id = aliases.reminder_id
                WHERE aliases.idempotency_key_hash = ?
                """,
                (key_hash,),
            ).fetchone(),
        )

    def _recent_business_match(
        self,
        connection: sqlite3.Connection,
        *,
        fingerprint: str,
        current: datetime,
    ) -> sqlite3.Row | None:
        cutoff = self._aware(current).astimezone(UTC) - self.reconciliation_retention
        candidates = cast(
            list[sqlite3.Row],
            connection.execute(
                """
                SELECT * FROM reminders
                WHERE request_fingerprint = ?
                ORDER BY created_at DESC
                """,
                (fingerprint,),
            ).fetchall(),
        )
        for candidate in candidates:
            try:
                created_at = self._aware(datetime.fromisoformat(candidate["created_at"]))
            except (TypeError, ValueError):
                continue
            if created_at.astimezone(UTC) >= cutoff:
                return candidate
        return None

    def create(
        self, request: AgentToolRequest, arguments_payload: dict[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        arguments = SetReminderArguments.model_validate(arguments_payload)
        key_hash = self._hash(request.idempotency_key)
        fingerprint = self._fingerprint(arguments, request.conversation_id)
        with self._lock, closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = self._existing_for_key(connection, key_hash)
                if existing is not None:
                    if existing["request_fingerprint"] != fingerprint:
                        raise AgentError(
                            "duplicate_request",
                            "The idempotency key belongs to a different reminder.",
                        )
                    connection.commit()
                    return self._public_result(existing, replayed=True), True
                current = self._aware(self._now())
                business_match = self._recent_business_match(
                    connection,
                    fingerprint=fingerprint,
                    current=current,
                )
                if business_match is not None:
                    connection.execute(
                        """
                        INSERT INTO reminder_idempotency_aliases (
                            idempotency_key_hash, request_fingerprint, reminder_id, created_at
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            key_hash,
                            fingerprint,
                            business_match["reminder_id"],
                            business_match["created_at"],
                        ),
                    )
                    connection.commit()
                    return self._public_result(business_match, replayed=True), True
                scheduled_utc, scheduled_local = self._parse_schedule(arguments)
                created_at = current
                reminder_id = str(uuid.uuid4())
                connection.execute(
                    """
                    INSERT INTO reminders (
                        reminder_id, idempotency_key_hash, request_fingerprint, title,
                        reminder_text, scheduled_at_utc, scheduled_at_local, timezone,
                        origin_conversation_id, origin_request_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        reminder_id,
                        key_hash,
                        fingerprint,
                        arguments.title,
                        arguments.reminder_text,
                        scheduled_utc.isoformat(),
                        scheduled_local.isoformat(),
                        arguments.timezone,
                        request.conversation_id,
                        request.request_id,
                        created_at.isoformat(),
                        created_at.isoformat(),
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM reminders WHERE reminder_id = ?", (reminder_id,)
                ).fetchone()
                if row is None:
                    raise AgentError("execution_failed", "The reminder could not be stored.")
                connection.commit()
                return self._public_result(row, replayed=False), False
            except Exception:
                connection.rollback()
                raise

    def get(self, reminder_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM reminders WHERE reminder_id = ?", (reminder_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def update(
        self,
        reminder_id: str,
        *,
        title: str,
        reminder_text: str,
        scheduled_at: str,
        timezone: str,
    ) -> bool:
        arguments = ReminderInput(
            title=title,
            reminder_text=reminder_text,
            scheduled_at=scheduled_at,
            timezone=timezone,
        )
        scheduled_utc, scheduled_local = self._parse_schedule(arguments)
        current_time = self._now()
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=UTC)
        current = current_time.astimezone(UTC).isoformat()
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE reminders
                SET title = ?, reminder_text = ?, scheduled_at_utc = ?,
                    scheduled_at_local = ?, timezone = ?, updated_at = ?
                WHERE reminder_id = ?
                """,
                (
                    title,
                    reminder_text,
                    scheduled_utc.isoformat(),
                    scheduled_local.isoformat(),
                    timezone,
                    current,
                    reminder_id,
                ),
            )
            connection.commit()
        return cursor.rowcount == 1

    def delete(self, reminder_id: str) -> bool:
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                "DELETE FROM reminders WHERE reminder_id = ?", (reminder_id,)
            )
            connection.commit()
        return cursor.rowcount == 1
