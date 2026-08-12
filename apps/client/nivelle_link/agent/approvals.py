from __future__ import annotations

import hashlib
import json
import threading
import unicodedata
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .atomic_store import atomic_write_json, read_json
from .errors import ApprovalError
from .models import (
    AgentPolicy,
    AgentToolRequest,
    ApprovalGrant,
    ApprovalMode,
    ApprovalSource,
    RiskLevel,
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _canonical_value(value: Any) -> Any:
    if isinstance(value, str):
        return unicodedata.normalize("NFKC", value)
    if isinstance(value, dict):
        return {str(key): _canonical_value(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_canonical_value(item) for item in value]
    return value


def argument_scope_hash(arguments: dict[str, Any]) -> str:
    serialized = json.dumps(
        _canonical_value(arguments), ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def policy_fingerprint(policy: AgentPolicy) -> str:
    serialized = json.dumps(
        policy.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def exact_target_for(request: AgentToolRequest) -> str:
    arguments = request.arguments
    if request.tool_name == "open_application":
        return f"application:{arguments.get('application_id', '')}"
    if request.tool_name in {"open_folder", "read_text_file"}:
        target = arguments.get("path_ref") or arguments.get("path") or ""
        return f"path:{target}"
    if request.tool_name == "search_files":
        return f"root:{arguments.get('root_id', '')}"
    if request.tool_name == "get_active_window":
        return "foreground-window"
    if request.tool_name == "create_note":
        return f"note:{arguments.get('title', '')}"
    if request.tool_name == "set_reminder":
        return f"reminder:{arguments.get('title', '')}:{arguments.get('scheduled_at', '')}"
    return request.tool_name


class ApprovalManager:
    """Client-owned approval records; only explicit local UI decisions may grant."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()

    def _load(self) -> list[ApprovalGrant]:
        payload = read_json(self.path, [])
        if not isinstance(payload, list):
            raise ApprovalError("permission_denied", "Local approval storage is invalid.")
        return [ApprovalGrant.model_validate(item) for item in payload]

    def _save(self, grants: list[ApprovalGrant]) -> None:
        atomic_write_json(
            self.path, [grant.model_dump(mode="json") for grant in grants]
        )

    def grant(
        self,
        request: AgentToolRequest,
        mode: ApprovalMode,
        *,
        source: ApprovalSource,
        policy: AgentPolicy,
        now: datetime | None = None,
    ) -> ApprovalGrant:
        if source is not ApprovalSource.USER_UI:
            raise ApprovalError(
                "permission_denied",
                "Only an explicit local Nivelle Link UI action can grant permission.",
            )
        if mode is ApprovalMode.NOT_REQUIRED:
            raise ApprovalError(
                "permission_denied", "Not-required is a policy setting, not an approval grant."
            )
        if mode is ApprovalMode.DENY:
            raise ApprovalError("approval_denied", "The user denied this tool request.")
        if (
            request.risk_level == RiskLevel.LOCAL_WRITE.value
            and mode is not ApprovalMode.ALLOW_ONCE
        ):
            raise ApprovalError(
                "permission_denied", "Write tools require a new one-time approval for every call."
            )
        if mode is ApprovalMode.ALWAYS_EXACT_TARGET:
            if request.tool_name not in policy.persistent_approval_tools:
                raise ApprovalError(
                    "permission_denied",
                    "Persistent approval is not permitted for this tool.",
                )
            if request.risk_level == RiskLevel.LOCAL_WRITE.value:
                raise ApprovalError(
                    "permission_denied", "Persistent approval is never permitted for writes."
                )

        current = now or _utc_now()
        expires_at: datetime | None = None
        session_id: str | None = None
        remaining_uses: int | None = None
        if mode is ApprovalMode.ALLOW_ONCE:
            remaining_uses = 1
        elif mode is ApprovalMode.ALLOW_SESSION:
            session_id = request.target_session_id
            expires_at = current + timedelta(seconds=policy.session_approval_ttl_seconds)

        grant = ApprovalGrant(
            approval_id=str(uuid.uuid4()),
            tool_call_id=request.tool_call_id,
            request_id=request.request_id,
            idempotency_key_hash=hashlib.sha256(
                request.idempotency_key.encode("utf-8")
            ).hexdigest(),
            policy_fingerprint=policy_fingerprint(policy),
            client_id=request.target_client_id,
            session_id=session_id,
            tool_name=request.tool_name,
            tool_version=request.tool_version,
            exact_target=exact_target_for(request),
            argument_scope_hash=argument_scope_hash(request.arguments),
            policy_version=policy.policy_version,
            mode=mode,
            created_at=current,
            expires_at=expires_at,
            remaining_uses=remaining_uses,
        )
        with self._lock:
            grants = self._load()
            grants.append(grant)
            self._save(grants)
        return grant

    def authorize(
        self,
        request: AgentToolRequest,
        *,
        policy: AgentPolicy,
        approval_id: str | None = None,
        now: datetime | None = None,
    ) -> ApprovalGrant | None:
        current = now or _utc_now()
        scope_hash = argument_scope_hash(request.arguments)
        exact_target = exact_target_for(request)
        with self._lock:
            grants = self._load()
            changed = False
            selected: ApprovalGrant | None = None
            for grant in grants:
                if grant.revoked_at is not None:
                    continue
                if grant.policy_version != policy.policy_version:
                    continue
                if grant.policy_fingerprint != policy_fingerprint(policy):
                    continue
                if approval_id is not None and grant.approval_id != approval_id:
                    continue
                if (
                    grant.client_id != request.target_client_id
                    or grant.tool_name != request.tool_name
                    or grant.tool_version != request.tool_version
                    or grant.exact_target != exact_target
                    or grant.argument_scope_hash != scope_hash
                ):
                    continue
                if grant.mode is ApprovalMode.ALLOW_SESSION:
                    if grant.session_id != request.target_session_id:
                        continue
                    if grant.expires_at is None or current >= grant.expires_at:
                        if approval_id == grant.approval_id:
                            raise ApprovalError("approval_expired", "The session approval expired.")
                        continue
                if grant.mode is ApprovalMode.ALLOW_ONCE:
                    if grant.remaining_uses == 1:
                        grant.remaining_uses = 0
                    elif not (
                        grant.remaining_uses == 0
                        and grant.tool_call_id == request.tool_call_id
                        and grant.request_id == request.request_id
                        and grant.idempotency_key_hash
                        == hashlib.sha256(request.idempotency_key.encode("utf-8")).hexdigest()
                    ):
                        continue
                grant.last_used_at = current
                selected = grant
                changed = True
                break

            if selected is None:
                raise ApprovalError("permission_denied", "This tool call has no valid local approval.")
            if changed:
                self._save(grants)
            return selected

    def revoke(self, approval_id: str, *, now: datetime | None = None) -> bool:
        with self._lock:
            grants = self._load()
            for grant in grants:
                if grant.approval_id == approval_id and grant.revoked_at is None:
                    grant.revoked_at = now or _utc_now()
                    self._save(grants)
                    return True
        return False

    def list_active(
        self, policy: AgentPolicy, *, now: datetime | None = None
    ) -> list[ApprovalGrant]:
        current = now or _utc_now()
        with self._lock:
            return [
                grant
                for grant in self._load()
                if grant.revoked_at is None
                and grant.policy_version == policy.policy_version
                and (grant.expires_at is None or current < grant.expires_at)
                and (grant.remaining_uses is None or grant.remaining_uses > 0)
            ]
