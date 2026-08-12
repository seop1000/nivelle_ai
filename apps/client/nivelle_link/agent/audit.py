from __future__ import annotations

import hashlib
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .atomic_store import atomic_write_json, read_json
from .models import AgentToolRequest, AuditRecord


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def summarize_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Keep audit metadata useful without persisting user-controlled text.

    Keys such as ``query`` and ``title`` are not inherently secret, but their
    values are free-form and may contain credentials or private content.  All
    strings therefore use length plus a one-way digest; the audit UI already
    has a separate bounded target summary for human-readable correlation.
    """

    summary: dict[str, Any] = {}
    for key, value in arguments.items():
        if isinstance(value, str):
            text = str(value)
            summary[key] = {"redacted": True, "characters": len(text), "sha256": _hash(text)}
        elif isinstance(value, list):
            summary[key] = {"items": len(value)}
        elif isinstance(value, (bool, int, float)) or value is None:
            summary[key] = value
        else:
            summary[key] = {"type": type(value).__name__}
    return summary


class AuditLog:
    def __init__(self, path: Path, *, retention: int = 1_000) -> None:
        self.path = path
        self.retention = retention
        self._lock = threading.RLock()

    def append(
        self,
        request: AgentToolRequest,
        *,
        status: str,
        target_summary: str,
        started_at: datetime,
        completed_at: datetime,
        result_summary: str,
        error_code: str | None = None,
    ) -> AuditRecord:
        record = AuditRecord(
            audit_id=str(uuid.uuid4()),
            tool_call_id=request.tool_call_id,
            request_id=request.request_id,
            idempotency_key_hash=_hash(request.idempotency_key),
            client_id=request.target_client_id,
            session_id=request.target_session_id,
            tool_name=request.tool_name,
            tool_version=request.tool_version,
            risk_level=request.risk_level,
            status=status,
            target_summary=target_summary[:200],
            arguments_summary=summarize_arguments(request.arguments),
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=max(0, int((completed_at - started_at).total_seconds() * 1_000)),
            result_summary=result_summary[:500],
            error_code=error_code,
        )
        with self._lock:
            payload = read_json(self.path, [])
            if not isinstance(payload, list):
                payload = []
            payload.append(record.model_dump(mode="json"))
            atomic_write_json(self.path, payload[-self.retention :])
        return record

    def list_recent(self) -> list[AuditRecord]:
        with self._lock:
            payload = read_json(self.path, [])
        return [AuditRecord.model_validate(item) for item in payload]
