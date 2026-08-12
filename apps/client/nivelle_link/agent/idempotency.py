from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from .atomic_store import atomic_write_json, read_json
from .errors import IdempotencyError


class IdempotencyRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key_hash: str
    fingerprint: str
    business_fingerprint: str | None = None
    tool_name: str
    state: Literal["pending", "completed"]
    created_at: datetime
    updated_at: datetime
    result: dict[str, Any] | None = None


class IdempotencyCache:
    """Persistent, bounded replay guard for local side effects.

    A pending record is flushed before a side effect begins. After a crash, it is
    deliberately not retried because whether the first action occurred is uncertain.
    """

    def __init__(
        self,
        path: Path,
        *,
        max_entries: int = 1_000,
        retention: timedelta = timedelta(days=7),
    ) -> None:
        self.path = path
        self.max_entries = max_entries
        self.retention = retention
        self._lock = threading.RLock()

    @staticmethod
    def _hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def fingerprint(tool_name: str, arguments: dict[str, Any]) -> str:
        serialized = json.dumps(
            {"tool": tool_name, "arguments": arguments},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def _load(self) -> list[IdempotencyRecord]:
        payload = read_json(self.path, [])
        if not isinstance(payload, list):
            raise IdempotencyError("execution_failed", "Local idempotency storage is invalid.")
        return [IdempotencyRecord.model_validate(item) for item in payload]

    def _save(self, records: list[IdempotencyRecord]) -> None:
        atomic_write_json(
            self.path, [record.model_dump(mode="json") for record in records]
        )

    def _prune(
        self, records: list[IdempotencyRecord], current: datetime
    ) -> list[IdempotencyRecord]:
        cutoff = current - self.retention
        retained = [record for record in records if record.updated_at >= cutoff]
        return sorted(retained, key=lambda record: record.updated_at)[-self.max_entries :]

    def execute_once(
        self,
        *,
        idempotency_key: str,
        tool_name: str,
        arguments: dict[str, Any],
        operation: Callable[[], dict[str, Any]],
        reconcile_completed: bool = False,
        business_arguments: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> tuple[dict[str, Any], bool]:
        current = now or datetime.now(UTC)
        key_hash = self._hash(idempotency_key)
        fingerprint = self.fingerprint(tool_name, arguments)
        business_fingerprint = (
            self.fingerprint(
                tool_name,
                arguments if business_arguments is None else business_arguments,
            )
            if reconcile_completed
            else None
        )
        with self._lock:
            records = self._prune(self._load(), current)
            existing = next((item for item in records if item.key_hash == key_hash), None)
            if existing is not None:
                if existing.fingerprint != fingerprint or existing.tool_name != tool_name:
                    raise IdempotencyError(
                        "duplicate_request",
                        "The idempotency key was already used for a different request.",
                    )
                if existing.state == "pending":
                    raise IdempotencyError(
                        "duplicate_request",
                        "The earlier side effect has an uncertain outcome and will not be repeated.",
                    )
                if existing.result is None:
                    raise IdempotencyError(
                        "duplicate_request", "The earlier result cannot be replayed safely."
                    )
                return existing.result, True

            if reconcile_completed:
                business_match = next(
                    (
                        item
                        for item in reversed(records)
                        if item.tool_name == tool_name
                        and item.business_fingerprint == business_fingerprint
                    ),
                    None,
                )
                if business_match is not None:
                    if business_match.state == "pending":
                        raise IdempotencyError(
                            "duplicate_request",
                            "An equivalent side effect has an uncertain outcome and will not be repeated.",
                        )
                    if business_match.result is None:
                        raise IdempotencyError(
                            "duplicate_request",
                            "The equivalent result cannot be replayed safely.",
                        )
                    # Reserve the retry's key as an alias. Preserve the original
                    # timestamps so retries cannot extend the seven-day window.
                    records.append(
                        IdempotencyRecord(
                            key_hash=key_hash,
                            fingerprint=fingerprint,
                            business_fingerprint=business_fingerprint,
                            tool_name=tool_name,
                            state="completed",
                            created_at=business_match.created_at,
                            updated_at=business_match.updated_at,
                            result=business_match.result,
                        )
                    )
                    self._save(self._prune(records, current))
                    return business_match.result, True

            pending = IdempotencyRecord(
                key_hash=key_hash,
                fingerprint=fingerprint,
                business_fingerprint=business_fingerprint,
                tool_name=tool_name,
                state="pending",
                created_at=current,
                updated_at=current,
            )
            records.append(pending)
            self._save(self._prune(records, current))

            try:
                result = operation()
            except Exception:
                # Keep pending: repeating a side effect after an exception may be unsafe.
                raise
            pending.state = "completed"
            pending.result = result
            pending.updated_at = current
            self._save(self._prune(records, pending.updated_at))
            return result, False
