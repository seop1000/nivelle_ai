from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import aiosqlite
from nivelle_protocol.tools import (
    LEGAL_TOOL_TRANSITIONS,
    TERMINAL_TOOL_STATUSES,
    ApprovalMode,
    RiskLevel,
    ToolEventType,
    ToolStatus,
)

from .database import Database

PROPOSED = ToolStatus.PROPOSED.value
VALIDATED = ToolStatus.VALIDATED.value
AWAITING_APPROVAL = ToolStatus.AWAITING_APPROVAL.value
APPROVED = ToolStatus.APPROVED.value
QUEUED = ToolStatus.QUEUED.value
RUNNING = ToolStatus.RUNNING.value
COMPLETED = ToolStatus.COMPLETED.value
VALIDATION_FAILED = ToolStatus.VALIDATION_FAILED.value
DENIED = ToolStatus.DENIED.value
TIMED_OUT = ToolStatus.TIMED_OUT.value
CANCELLED = ToolStatus.CANCELLED.value
FAILED = ToolStatus.FAILED.value
CLIENT_DISCONNECTED = ToolStatus.CLIENT_DISCONNECTED.value

TERMINAL_STATUSES = frozenset(status.value for status in TERMINAL_TOOL_STATUSES)
NONTERMINAL_STATUSES = frozenset(
    {PROPOSED, VALIDATED, AWAITING_APPROVAL, APPROVED, QUEUED, RUNNING}
)
TOOL_STATUSES = TERMINAL_STATUSES | NONTERMINAL_STATUSES
EXECUTION_STATUSES = frozenset({QUEUED, RUNNING})

VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    current.value: frozenset(target.value for target in targets)
    for current, targets in LEGAL_TOOL_TRANSITIONS.items()
}

EVENT_TYPES: dict[str, str] = {
    PROPOSED: ToolEventType.PROPOSED.value,
    # The shared protocol represents successful validation as tool.request.
    VALIDATED: ToolEventType.REQUEST.value,
    AWAITING_APPROVAL: ToolEventType.APPROVAL_REQUIRED.value,
    APPROVED: ToolEventType.APPROVED.value,
    QUEUED: ToolEventType.QUEUED.value,
    RUNNING: ToolEventType.STARTED.value,
    COMPLETED: ToolEventType.COMPLETED.value,
    VALIDATION_FAILED: ToolEventType.VALIDATION_FAILED.value,
    DENIED: ToolEventType.DENIED.value,
    TIMED_OUT: ToolEventType.TIMED_OUT.value,
    CANCELLED: ToolEventType.CANCELLED.value,
    FAILED: ToolEventType.FAILED.value,
    CLIENT_DISCONNECTED: ToolEventType.CLIENT_DISCONNECTED.value,
}

_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(authorization|password|passwd|token|secret|api[_-]?key|private[_-]?key)"
    r"\s*[:=]\s*([^\s,;]+)"
)
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
    flags=re.DOTALL,
)
MAX_SUMMARY_CHARACTERS = 2_000


class ToolRepositoryError(RuntimeError):
    pass


class ToolCallNotFoundError(ToolRepositoryError):
    pass


class DuplicateToolCallError(ToolRepositoryError):
    pass


class DuplicateIdempotencyKeyError(ToolRepositoryError):
    pass


class InvalidToolTransitionError(ToolRepositoryError):
    pass


class ToolTargetMismatchError(ToolRepositoryError):
    pass


class ToolLimitExceededError(ToolRepositoryError):
    pass


class ConflictingToolResultError(ToolRepositoryError):
    pass


@dataclass(frozen=True, slots=True)
class ToolCallCreate:
    tool_call_id: str
    request_id: str
    idempotency_key: str
    conversation_id: str
    user_message_id: str
    assistant_message_id: str | None
    target_client_id: str
    target_session_id: str
    tool_name: str
    tool_version: str
    risk_level: str
    arguments_summary: str
    approval_mode: str


@dataclass(frozen=True, slots=True)
class ClientCapability:
    client_id: str
    session_id: str
    platform: str
    app_version: str
    protocol_version: str
    tool_name: str
    tool_version: str
    enabled: bool
    implementation_available: bool
    risk_level: str
    default_approval_required: bool
    default_timeout_ms: int
    maximum_timeout_ms: int
    maximum_result_size: int
    expires_at: str


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def safe_metadata_summary(value: str | None) -> str:
    """Bound and redact an already metadata-only audit summary.

    The repository deliberately has no raw argument/result fields. This last
    defensive pass prevents common credential forms from leaking through a
    caller-provided summary.
    """

    if value is None:
        return ""
    normalized = " ".join(str(value).split())
    normalized = _PRIVATE_KEY_BLOCK.sub("[REDACTED PRIVATE KEY]", normalized)
    normalized = _SECRET_ASSIGNMENT.sub(r"\1=[REDACTED]", normalized)
    if len(normalized) <= MAX_SUMMARY_CHARACTERS:
        return normalized
    return normalized[: MAX_SUMMARY_CHARACTERS - 1] + "…"


def server_result_metadata_summary(
    *,
    tool_name: str,
    status: str,
    error_code: str | None,
) -> str:
    """Build a terminal summary only from bounded server-owned metadata.

    ``ToolResult.safe_summary`` originates on the remote Link and may contain
    arbitrary file content or credentials despite its name. Terminal result
    rows therefore never accept that text. The durable summary is derived from
    the correlated tool call and the validated terminal state instead.
    """

    normalized_tool_name = _require_nonempty("tool_name", tool_name)
    if status not in TERMINAL_STATUSES:
        raise ValueError(f"invalid terminal result status: {status}")
    normalized_error_code = _require_nonempty("error_code", error_code or "none")
    return safe_metadata_summary(
        f"tool={normalized_tool_name}; status={status}; "
        f"error_code={normalized_error_code}"
    )


def _require_nonempty(label: str, value: str) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise ValueError(f"{label} must not be empty")
    return normalized


def _row(row: aiosqlite.Row) -> dict[str, Any]:
    return dict(row)


class ToolRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def get_tool_call(self, tool_call_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone(
            "SELECT * FROM tool_calls WHERE tool_call_id=?", (tool_call_id,)
        )
        return _row(row) if row is not None else None

    async def get_by_idempotency_key(
        self, idempotency_key: str
    ) -> dict[str, Any] | None:
        row = await self.db.fetchone(
            """
            SELECT call.* FROM tool_idempotency AS ledger
            JOIN tool_calls AS call ON call.tool_call_id=ledger.tool_call_id
            WHERE ledger.idempotency_key=?
            """,
            (idempotency_key,),
        )
        return _row(row) if row is not None else None

    async def list_events(self, tool_call_id: str) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            """
            SELECT * FROM tool_call_events
            WHERE tool_call_id=? ORDER BY sequence,event_id
            """,
            (tool_call_id,),
        )
        return [_row(row) for row in rows]

    async def create_tool_call(
        self,
        call: ToolCallCreate,
        *,
        max_calls_per_turn: int,
        idempotency_retention_days: int,
        created_at: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Insert a proposal and ledger record atomically.

        An exact replay returns the original row with ``created=False``. Any
        identifier collision with different immutable routing data is rejected.
        """

        if max_calls_per_turn < 1:
            raise ValueError("max_calls_per_turn must be positive")
        if idempotency_retention_days < 1:
            raise ValueError("idempotency_retention_days must be positive")
        self._validate_call(call)
        timestamp = created_at or utc_now()
        expires_at = (
            datetime.fromisoformat(timestamp) + timedelta(days=idempotency_retention_days)
        ).isoformat()
        arguments_summary = safe_metadata_summary(call.arguments_summary)
        async with aiosqlite.connect(self.db.path) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                existing_by_id = await self._fetch_call(db, call.tool_call_id)
                existing_by_key = await self._fetch_by_key(db, call.idempotency_key)
                if existing_by_id is not None or existing_by_key is not None:
                    replay = self._resolve_replay(call, existing_by_id, existing_by_key)
                    await db.rollback()
                    return replay, False
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM tool_calls WHERE request_id=?",
                    (call.request_id,),
                )
                count_row = await cursor.fetchone()
                await cursor.close()
                count = int(count_row[0]) if count_row else 0
                if count >= max_calls_per_turn:
                    raise ToolLimitExceededError(
                        f"calls-per-turn limit reached for request {call.request_id}"
                    )
                await db.execute(
                    """
                    INSERT INTO tool_calls(
                        tool_call_id,request_id,idempotency_key,conversation_id,
                        user_message_id,assistant_message_id,target_client_id,
                        target_session_id,tool_name,tool_version,risk_level,
                        arguments_summary,status,approval_mode,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        call.tool_call_id,
                        call.request_id,
                        call.idempotency_key,
                        call.conversation_id,
                        call.user_message_id,
                        call.assistant_message_id,
                        call.target_client_id,
                        call.target_session_id,
                        call.tool_name,
                        call.tool_version,
                        call.risk_level,
                        arguments_summary,
                        PROPOSED,
                        call.approval_mode,
                        timestamp,
                        timestamp,
                    ),
                )
                await db.execute(
                    """
                    INSERT INTO tool_idempotency(
                        idempotency_key,tool_call_id,request_id,target_client_id,
                        target_session_id,tool_name,outcome_status,created_at,
                        updated_at,expires_at
                    ) VALUES(?,?,?,?,?,?,NULL,?,?,?)
                    """,
                    (
                        call.idempotency_key,
                        call.tool_call_id,
                        call.request_id,
                        call.target_client_id,
                        call.target_session_id,
                        call.tool_name,
                        timestamp,
                        timestamp,
                        expires_at,
                    ),
                )
                await self._append_event(
                    db,
                    tool_call_id=call.tool_call_id,
                    request_id=call.request_id,
                    event_type=EVENT_TYPES[PROPOSED],
                    from_status=None,
                    to_status=PROPOSED,
                    target_client_id=call.target_client_id,
                    target_session_id=call.target_session_id,
                    safe_summary="",
                    error_code=None,
                    created_at=timestamp,
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        created = await self.get_tool_call(call.tool_call_id)
        if created is None:  # pragma: no cover - committed insert invariant
            raise ToolRepositoryError("created tool call could not be reloaded")
        return created, True

    async def transition(
        self,
        tool_call_id: str,
        to_status: str,
        *,
        target_client_id: str | None = None,
        target_session_id: str | None = None,
        safe_summary: str = "",
        error_code: str | None = None,
        occurred_at: str | None = None,
    ) -> bool:
        if to_status not in TOOL_STATUSES:
            raise ValueError(f"unknown tool status: {to_status}")
        self._validate_optional_target(target_client_id, target_session_id)
        timestamp = occurred_at or utc_now()
        async with aiosqlite.connect(self.db.path) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                call = await self._require_call(db, tool_call_id)
                self._assert_target(call, target_client_id, target_session_id)
                changed = await self._transition_locked(
                    db,
                    call,
                    to_status,
                    safe_summary=safe_summary,
                    error_code=error_code,
                    occurred_at=timestamp,
                )
                await db.commit()
                return changed
            except Exception:
                await db.rollback()
                raise

    async def queue_with_parallel_limit(
        self,
        tool_call_id: str,
        *,
        target_client_id: str,
        target_session_id: str,
        max_parallel_calls_per_client: int,
        occurred_at: str | None = None,
    ) -> bool:
        if max_parallel_calls_per_client < 1:
            raise ValueError("max_parallel_calls_per_client must be positive")
        timestamp = occurred_at or utc_now()
        async with aiosqlite.connect(self.db.path) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                call = await self._require_call(db, tool_call_id)
                self._assert_target(call, target_client_id, target_session_id)
                if str(call["status"]) == QUEUED:
                    await db.rollback()
                    return False
                cursor = await db.execute(
                    """
                    SELECT COUNT(*) FROM tool_calls
                    WHERE target_client_id=? AND status IN ('queued','running')
                      AND tool_call_id<>?
                    """,
                    (target_client_id, tool_call_id),
                )
                count_row = await cursor.fetchone()
                await cursor.close()
                count = int(count_row[0]) if count_row else 0
                if count >= max_parallel_calls_per_client:
                    raise ToolLimitExceededError(
                        f"parallel-call limit reached for client {target_client_id}"
                    )
                changed = await self._transition_locked(
                    db,
                    call,
                    QUEUED,
                    safe_summary="",
                    error_code=None,
                    occurred_at=timestamp,
                )
                await db.commit()
                return changed
            except Exception:
                await db.rollback()
                raise

    async def record_approval(
        self,
        tool_call_id: str,
        *,
        approved: bool,
        target_client_id: str,
        target_session_id: str,
        safe_summary: str = "",
        occurred_at: str | None = None,
    ) -> bool:
        destination = APPROVED if approved else DENIED
        timestamp = occurred_at or utc_now()
        async with aiosqlite.connect(self.db.path) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                call = await self._require_call(db, tool_call_id)
                self._assert_target(call, target_client_id, target_session_id)
                cursor = await db.execute(
                    """
                    SELECT 1 FROM tool_call_events
                    WHERE tool_call_id=? AND to_status=? LIMIT 1
                    """,
                    (tool_call_id, destination),
                )
                duplicate = await cursor.fetchone()
                await cursor.close()
                if duplicate is not None:
                    await db.rollback()
                    return False
                changed = await self._transition_locked(
                    db,
                    call,
                    destination,
                    safe_summary=safe_summary,
                    error_code=None if approved else "approval_denied",
                    occurred_at=timestamp,
                )
                await db.commit()
                return changed
            except Exception:
                await db.rollback()
                raise

    async def record_result(
        self,
        tool_call_id: str,
        *,
        status: str,
        target_client_id: str,
        target_session_id: str,
        error_code: str | None,
        duration_ms: int | None,
        occurred_at: str | None = None,
    ) -> bool:
        if status not in {COMPLETED, FAILED, CANCELLED, TIMED_OUT}:
            raise ValueError(f"invalid client result status: {status}")
        if duration_ms is not None and duration_ms < 0:
            raise ValueError("duration_ms must not be negative")
        timestamp = occurred_at or utc_now()
        async with aiosqlite.connect(self.db.path) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                call = await self._require_call(db, tool_call_id)
                self._assert_target(call, target_client_id, target_session_id)
                summary = server_result_metadata_summary(
                    tool_name=str(call["tool_name"]),
                    status=status,
                    error_code=error_code,
                )
                current = str(call["status"])
                if current in TERMINAL_STATUSES:
                    same = (
                        current == status
                        and call["result_summary"] == summary
                        and call["error_code"] == error_code
                        and call["duration_ms"] == duration_ms
                    )
                    if same:
                        await db.rollback()
                        return False
                    raise ConflictingToolResultError(
                        f"conflicting duplicate result for {tool_call_id}"
                    )
                if status not in VALID_TRANSITIONS.get(current, frozenset()):
                    raise InvalidToolTransitionError(f"{current} -> {status} is not allowed")
                await db.execute(
                    """
                    UPDATE tool_calls
                    SET status=?,completed_at=?,duration_ms=?,result_summary=?,
                        error_code=?,updated_at=?
                    WHERE tool_call_id=?
                    """,
                    (
                        status,
                        timestamp,
                        duration_ms,
                        summary,
                        error_code,
                        timestamp,
                        tool_call_id,
                    ),
                )
                await db.execute(
                    """
                    UPDATE tool_idempotency
                    SET outcome_status=?,updated_at=? WHERE tool_call_id=?
                    """,
                    (status, timestamp, tool_call_id),
                )
                await self._append_event(
                    db,
                    tool_call_id=tool_call_id,
                    request_id=str(call["request_id"]),
                    event_type=EVENT_TYPES[status],
                    from_status=current,
                    to_status=status,
                    target_client_id=str(call["target_client_id"]),
                    target_session_id=str(call["target_session_id"]),
                    safe_summary=summary or "",
                    error_code=error_code,
                    created_at=timestamp,
                )
                await db.commit()
                return True
            except Exception:
                await db.rollback()
                raise

    async def record_progress(
        self,
        tool_call_id: str,
        *,
        target_client_id: str,
        target_session_id: str,
        safe_summary: str,
        occurred_at: str | None = None,
    ) -> bool:
        """Persist metadata-only ``tool.progress`` for a running call."""

        return await self._record_same_state_event(
            tool_call_id,
            required_status=RUNNING,
            event_type=ToolEventType.PROGRESS.value,
            target_client_id=target_client_id,
            target_session_id=target_session_id,
            safe_summary=safe_summary,
            occurred_at=occurred_at,
        )

    async def replace_capabilities(
        self,
        capabilities: Sequence[ClientCapability],
        *,
        connected_at: str | None = None,
    ) -> None:
        if not capabilities:
            raise ValueError("at least one capability is required")
        first = capabilities[0]
        client_id = _require_nonempty("client_id", first.client_id)
        session_id = _require_nonempty("session_id", first.session_id)
        for capability in capabilities:
            self._validate_capability(capability, client_id, session_id)
        timestamp = connected_at or utc_now()
        async with aiosqlite.connect(self.db.path) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("BEGIN IMMEDIATE")
            try:
                # A new advertisement is the sole live capability set for this
                # client. Prior sessions and omitted tools become stale.
                await db.execute(
                    """
                    UPDATE client_capabilities
                    SET expires_at=?,disconnected_at=COALESCE(disconnected_at,?)
                    WHERE client_id=? AND disconnected_at IS NULL
                    """,
                    (timestamp, timestamp, client_id),
                )
                for capability in capabilities:
                    await db.execute(
                        """
                        INSERT INTO client_capabilities(
                            client_id,session_id,platform,app_version,protocol_version,
                            tool_name,tool_version,enabled,implementation_available,
                            risk_level,default_approval_required,default_timeout_ms,
                            maximum_timeout_ms,maximum_result_size,connected_at,
                            expires_at,disconnected_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)
                        ON CONFLICT(client_id,session_id,tool_name,tool_version)
                        DO UPDATE SET
                            platform=excluded.platform,
                            app_version=excluded.app_version,
                            protocol_version=excluded.protocol_version,
                            enabled=excluded.enabled,
                            implementation_available=excluded.implementation_available,
                            risk_level=excluded.risk_level,
                            default_approval_required=excluded.default_approval_required,
                            default_timeout_ms=excluded.default_timeout_ms,
                            maximum_timeout_ms=excluded.maximum_timeout_ms,
                            maximum_result_size=excluded.maximum_result_size,
                            connected_at=excluded.connected_at,
                            expires_at=excluded.expires_at,
                            disconnected_at=NULL
                        """,
                        (
                            capability.client_id,
                            capability.session_id,
                            capability.platform,
                            capability.app_version,
                            capability.protocol_version,
                            capability.tool_name,
                            capability.tool_version,
                            int(capability.enabled),
                            int(capability.implementation_available),
                            capability.risk_level,
                            int(capability.default_approval_required),
                            capability.default_timeout_ms,
                            capability.maximum_timeout_ms,
                            capability.maximum_result_size,
                            timestamp,
                            capability.expires_at,
                        ),
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    async def _record_same_state_event(
        self,
        tool_call_id: str,
        *,
        required_status: str,
        event_type: str,
        target_client_id: str,
        target_session_id: str,
        safe_summary: str,
        occurred_at: str | None,
    ) -> bool:
        timestamp = occurred_at or utc_now()
        async with aiosqlite.connect(self.db.path) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                call = await self._require_call(db, tool_call_id)
                self._assert_target(call, target_client_id, target_session_id)
                current = str(call["status"])
                if current != required_status:
                    raise InvalidToolTransitionError(
                        f"{event_type} requires {required_status}, found {current}"
                    )
                summary = safe_metadata_summary(safe_summary)
                await self._append_event(
                    db,
                    tool_call_id=tool_call_id,
                    request_id=str(call["request_id"]),
                    event_type=event_type,
                    from_status=current,
                    to_status=current,
                    target_client_id=str(call["target_client_id"]),
                    target_session_id=str(call["target_session_id"]),
                    safe_summary=summary,
                    error_code=None,
                    created_at=timestamp,
                )
                await db.execute(
                    "UPDATE tool_calls SET updated_at=? WHERE tool_call_id=?",
                    (timestamp, tool_call_id),
                )
                await db.commit()
                return True
            except Exception:
                await db.rollback()
                raise

    async def get_live_capability(
        self,
        *,
        client_id: str,
        session_id: str,
        tool_name: str,
        tool_version: str,
        at: str | None = None,
    ) -> dict[str, Any] | None:
        timestamp = at or utc_now()
        row = await self.db.fetchone(
            """
            SELECT * FROM client_capabilities
            WHERE client_id=? AND session_id=? AND tool_name=? AND tool_version=?
              AND enabled=1 AND implementation_available=1
              AND disconnected_at IS NULL AND expires_at>?
            """,
            (client_id, session_id, tool_name, tool_version, timestamp),
        )
        return _row(row) if row is not None else None

    async def disconnect_session(
        self,
        *,
        client_id: str,
        session_id: str,
        occurred_at: str | None = None,
    ) -> int:
        timestamp = occurred_at or utc_now()
        async with aiosqlite.connect(self.db.path) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute(
                    """
                    UPDATE client_capabilities
                    SET expires_at=?,disconnected_at=COALESCE(disconnected_at,?)
                    WHERE client_id=? AND session_id=? AND disconnected_at IS NULL
                    """,
                    (timestamp, timestamp, client_id, session_id),
                )
                cursor = await db.execute(
                    """
                    SELECT * FROM tool_calls
                    WHERE target_client_id=? AND target_session_id=?
                      AND status IN (
                        'proposed','validated','awaiting_approval','approved',
                        'queued','running'
                      )
                    ORDER BY created_at,tool_call_id
                    """,
                    (client_id, session_id),
                )
                calls = list(await cursor.fetchall())
                await cursor.close()
                for call in calls:
                    await self._transition_locked(
                        db,
                        call,
                        CLIENT_DISCONNECTED,
                        safe_summary="target session disconnected",
                        error_code="client_disconnected",
                        occurred_at=timestamp,
                    )
                await db.commit()
                return len(calls)
            except Exception:
                await db.rollback()
                raise

    async def prune_terminal_before(self, cutoff: str) -> tuple[int, int]:
        """Bound retained replay/audit state without touching live calls."""

        async with aiosqlite.connect(self.db.path) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("BEGIN IMMEDIATE")
            try:
                capability_cursor = await db.execute(
                    """
                    DELETE FROM client_capabilities
                    WHERE expires_at<? AND disconnected_at IS NOT NULL
                    """,
                    (cutoff,),
                )
                capabilities_deleted = max(capability_cursor.rowcount, 0)
                await capability_cursor.close()
                call_cursor = await db.execute(
                    """
                    DELETE FROM tool_calls
                    WHERE updated_at<? AND status IN (
                        'completed','validation_failed','denied','timed_out',
                        'cancelled','failed','client_disconnected'
                    )
                    """,
                    (cutoff,),
                )
                calls_deleted = max(call_cursor.rowcount, 0)
                await call_cursor.close()
                await db.commit()
                return calls_deleted, capabilities_deleted
            except Exception:
                await db.rollback()
                raise

    @staticmethod
    def _validate_call(call: ToolCallCreate) -> None:
        for label in (
            "tool_call_id",
            "request_id",
            "idempotency_key",
            "conversation_id",
            "user_message_id",
            "target_client_id",
            "target_session_id",
            "tool_name",
            "tool_version",
            "risk_level",
            "approval_mode",
        ):
            _require_nonempty(label, str(getattr(call, label)))
        try:
            ApprovalMode(call.approval_mode)
        except ValueError as exc:
            raise ValueError(f"unknown approval_mode: {call.approval_mode}") from exc
        try:
            RiskLevel(call.risk_level)
        except ValueError as exc:
            raise ValueError(f"unknown risk_level: {call.risk_level}") from exc

    @staticmethod
    def _validate_capability(
        capability: ClientCapability, expected_client_id: str, expected_session_id: str
    ) -> None:
        if capability.client_id != expected_client_id:
            raise ValueError("one advertisement cannot contain multiple client IDs")
        if capability.session_id != expected_session_id:
            raise ValueError("one advertisement cannot contain multiple session IDs")
        for label in (
            "platform",
            "app_version",
            "protocol_version",
            "tool_name",
            "tool_version",
            "risk_level",
            "expires_at",
        ):
            _require_nonempty(label, str(getattr(capability, label)))
        if capability.default_timeout_ms < 1:
            raise ValueError("default_timeout_ms must be positive")
        if capability.maximum_timeout_ms < capability.default_timeout_ms:
            raise ValueError("maximum_timeout_ms must not be below the default")
        if capability.maximum_result_size < 1:
            raise ValueError("maximum_result_size must be positive")

    @staticmethod
    def _validate_optional_target(client_id: str | None, session_id: str | None) -> None:
        if (client_id is None) != (session_id is None):
            raise ValueError("target client and session must be supplied together")

    @staticmethod
    async def _fetch_call(
        db: aiosqlite.Connection, tool_call_id: str
    ) -> aiosqlite.Row | None:
        cursor = await db.execute(
            "SELECT * FROM tool_calls WHERE tool_call_id=?", (tool_call_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row

    @staticmethod
    async def _fetch_by_key(
        db: aiosqlite.Connection, idempotency_key: str
    ) -> aiosqlite.Row | None:
        cursor = await db.execute(
            "SELECT * FROM tool_calls WHERE idempotency_key=?", (idempotency_key,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row

    @classmethod
    async def _require_call(
        cls, db: aiosqlite.Connection, tool_call_id: str
    ) -> aiosqlite.Row:
        row = await cls._fetch_call(db, tool_call_id)
        if row is None:
            raise ToolCallNotFoundError(tool_call_id)
        return row

    @staticmethod
    def _resolve_replay(
        requested: ToolCallCreate,
        existing_by_id: aiosqlite.Row | None,
        existing_by_key: aiosqlite.Row | None,
    ) -> dict[str, Any]:
        if existing_by_id is None:
            raise DuplicateIdempotencyKeyError(requested.idempotency_key)
        if existing_by_key is None:
            raise DuplicateToolCallError(requested.tool_call_id)
        if str(existing_by_id["tool_call_id"]) != str(existing_by_key["tool_call_id"]):
            raise DuplicateToolCallError("tool_call_id and idempotency_key map differently")
        immutable = {
            "tool_call_id": requested.tool_call_id,
            "request_id": requested.request_id,
            "idempotency_key": requested.idempotency_key,
            "conversation_id": requested.conversation_id,
            "user_message_id": requested.user_message_id,
            "assistant_message_id": requested.assistant_message_id,
            "target_client_id": requested.target_client_id,
            "target_session_id": requested.target_session_id,
            "tool_name": requested.tool_name,
            "tool_version": requested.tool_version,
            "risk_level": requested.risk_level,
            "arguments_summary": safe_metadata_summary(requested.arguments_summary),
            "approval_mode": requested.approval_mode,
        }
        if any(str(existing_by_id[key]) != str(value) for key, value in immutable.items()):
            raise DuplicateToolCallError("replayed tool call changed immutable routing data")
        return _row(existing_by_id)

    @staticmethod
    def _assert_target(
        call: aiosqlite.Row,
        client_id: str | None,
        session_id: str | None,
    ) -> None:
        if client_id is None and session_id is None:
            return
        if (
            str(call["target_client_id"]) != client_id
            or str(call["target_session_id"]) != session_id
        ):
            raise ToolTargetMismatchError(
                f"result target does not own tool call {call['tool_call_id']}"
            )

    async def _transition_locked(
        self,
        db: aiosqlite.Connection,
        call: aiosqlite.Row,
        to_status: str,
        *,
        safe_summary: str,
        error_code: str | None,
        occurred_at: str,
    ) -> bool:
        current = str(call["status"])
        if current == to_status:
            return False
        if to_status not in VALID_TRANSITIONS.get(current, frozenset()):
            raise InvalidToolTransitionError(f"{current} -> {to_status} is not allowed")
        approved_at = occurred_at if to_status == APPROVED else call["approved_at"]
        started_at = occurred_at if to_status == RUNNING else call["started_at"]
        completed_at = occurred_at if to_status in TERMINAL_STATUSES else call["completed_at"]
        summary = safe_metadata_summary(safe_summary)
        await db.execute(
            """
            UPDATE tool_calls
            SET status=?,approved_at=?,started_at=?,completed_at=?,error_code=?,updated_at=?
            WHERE tool_call_id=?
            """,
            (
                to_status,
                approved_at,
                started_at,
                completed_at,
                error_code,
                occurred_at,
                str(call["tool_call_id"]),
            ),
        )
        await db.execute(
            """
            UPDATE tool_idempotency
            SET outcome_status=?,updated_at=? WHERE tool_call_id=?
            """,
            (
                to_status if to_status in TERMINAL_STATUSES else None,
                occurred_at,
                str(call["tool_call_id"]),
            ),
        )
        await self._append_event(
            db,
            tool_call_id=str(call["tool_call_id"]),
            request_id=str(call["request_id"]),
            event_type=EVENT_TYPES[to_status],
            from_status=current,
            to_status=to_status,
            target_client_id=str(call["target_client_id"]),
            target_session_id=str(call["target_session_id"]),
            safe_summary=summary,
            error_code=error_code,
            created_at=occurred_at,
        )
        return True

    @staticmethod
    async def _append_event(
        db: aiosqlite.Connection,
        *,
        tool_call_id: str,
        request_id: str,
        event_type: str,
        from_status: str | None,
        to_status: str,
        target_client_id: str,
        target_session_id: str,
        safe_summary: str,
        error_code: str | None,
        created_at: str,
    ) -> None:
        cursor = await db.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM tool_call_events WHERE tool_call_id=?",
            (tool_call_id,),
        )
        sequence_row = await cursor.fetchone()
        await cursor.close()
        sequence = int(sequence_row[0]) if sequence_row else 1
        await db.execute(
            """
            INSERT INTO tool_call_events(
                event_id,tool_call_id,request_id,sequence,event_type,from_status,
                to_status,target_client_id,target_session_id,safe_summary,error_code,
                created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                str(uuid4()),
                tool_call_id,
                request_id,
                sequence,
                event_type,
                from_status,
                to_status,
                target_client_id,
                target_session_id,
                safe_summary,
                error_code,
                created_at,
            ),
        )
