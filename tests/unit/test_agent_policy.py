from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from nivelle_link.agent import (
    AgentPolicy,
    AgentToolRequest,
    ApprovalError,
    ApprovalManager,
    ApprovalMode,
    ApprovalSource,
    PolicyStore,
    RiskLevel,
)
from pydantic import ValidationError


def request(
    tool_name: str = "read_text_file",
    *,
    risk: RiskLevel = RiskLevel.LOCAL_READ,
    arguments: dict[str, object] | None = None,
    session_id: str | None = None,
) -> AgentToolRequest:
    return AgentToolRequest(
        tool_call_id=str(uuid4()),
        request_id=str(uuid4()),
        idempotency_key=str(uuid4()),
        conversation_id=str(uuid4()),
        user_message_id=str(uuid4()),
        target_client_id=str(uuid4()),
        target_session_id=session_id or str(uuid4()),
        tool_name=tool_name,
        tool_version="1.0",
        arguments=arguments or {"path_ref": "docs:dGVzdC50eHQ"},
        risk_level=risk.value,
        created_at=datetime.now(UTC),
        timeout_ms=5_000,
        user_intent_summary="test",
    )


def test_default_policy_fails_closed_but_declares_safe_status() -> None:
    policy = AgentPolicy()

    assert policy.agent_enabled is False
    assert policy.enabled_tools == {"get_system_status"}
    assert policy.applications == {}
    assert policy.filesystem_roots == {}
    assert policy.allow_network_paths is False
    assert policy.allow_hidden_files is False
    assert policy.allow_reparse_points is False


def test_policy_store_is_atomic_and_rejects_secret_fields(tmp_path: Path) -> None:
    store = PolicyStore(tmp_path / "policy.json")
    policy = AgentPolicy(agent_enabled=True)
    store.save(policy)

    assert store.load() == policy
    assert not list(tmp_path.glob("*.tmp"))
    assert "token" not in store.path.read_text(encoding="utf-8").casefold()
    with pytest.raises(ValidationError):
        AgentPolicy.model_validate({"token": "must-not-be-stored"})


@pytest.mark.parametrize(
    "source",
    [
        ApprovalSource.CHAT,
        ApprovalSource.PERSONA,
        ApprovalSource.MEMORY,
        ApprovalSource.TOOL_RESULT,
        ApprovalSource.SERVER,
    ],
)
def test_only_local_ui_can_grant_permission(tmp_path: Path, source: ApprovalSource) -> None:
    manager = ApprovalManager(tmp_path / "approvals.json")
    with pytest.raises(ApprovalError, match="explicit local"):
        manager.grant(
            request(),
            ApprovalMode.ALLOW_ONCE,
            source=source,
            policy=AgentPolicy(),
        )


def test_explicit_denial_never_creates_an_approval(tmp_path: Path) -> None:
    manager = ApprovalManager(tmp_path / "approvals.json")
    with pytest.raises(ApprovalError) as denied:
        manager.grant(
            request(),
            ApprovalMode.DENY,
            source=ApprovalSource.USER_UI,
            policy=AgentPolicy(),
        )
    assert denied.value.code == "approval_denied"
    assert not manager.list_active(AgentPolicy())


def test_allow_once_is_bound_to_exact_call_and_duplicate_replay(tmp_path: Path) -> None:
    manager = ApprovalManager(tmp_path / "approvals.json")
    policy = AgentPolicy()
    original = request()
    grant = manager.grant(
        original,
        ApprovalMode.ALLOW_ONCE,
        source=ApprovalSource.USER_UI,
        policy=policy,
    )

    assert manager.authorize(original, policy=policy, approval_id=grant.approval_id) is not None
    # A reconnect replay of the same correlated call may reach the idempotency cache.
    assert manager.authorize(original, policy=policy, approval_id=grant.approval_id) is not None
    changed = original.model_copy(update={"tool_call_id": str(uuid4())})
    with pytest.raises(ApprovalError):
        manager.authorize(changed, policy=policy, approval_id=grant.approval_id)


def test_session_expiry_revocation_and_policy_version_invalidation(tmp_path: Path) -> None:
    manager = ApprovalManager(tmp_path / "approvals.json")
    policy = AgentPolicy(policy_version="p1", session_approval_ttl_seconds=60)
    now = datetime(2030, 1, 1, tzinfo=UTC)
    original = request(session_id="session-a")
    grant = manager.grant(
        original,
        ApprovalMode.ALLOW_SESSION,
        source=ApprovalSource.USER_UI,
        policy=policy,
        now=now,
    )

    assert manager.authorize(
        original, policy=policy, approval_id=grant.approval_id, now=now + timedelta(seconds=59)
    )
    with pytest.raises(ApprovalError, match="expired"):
        manager.authorize(
            original,
            policy=policy,
            approval_id=grant.approval_id,
            now=now + timedelta(seconds=60),
        )

    second = manager.grant(
        original,
        ApprovalMode.ALLOW_SESSION,
        source=ApprovalSource.USER_UI,
        policy=policy,
        now=now,
    )
    assert manager.revoke(second.approval_id, now=now)
    with pytest.raises(ApprovalError):
        manager.authorize(original, policy=policy, approval_id=second.approval_id, now=now)

    third = manager.grant(
        original,
        ApprovalMode.ALLOW_SESSION,
        source=ApprovalSource.USER_UI,
        policy=policy,
        now=now,
    )
    with pytest.raises(ApprovalError):
        manager.authorize(
            original,
            policy=policy.model_copy(update={"policy_version": "p2"}),
            approval_id=third.approval_id,
            now=now,
        )

    fourth = manager.grant(
        original,
        ApprovalMode.ALLOW_SESSION,
        source=ApprovalSource.USER_UI,
        policy=policy,
        now=now,
    )
    with pytest.raises(ApprovalError):
        manager.authorize(
            original,
            policy=policy.model_copy(update={"allow_hidden_files": True}),
            approval_id=fourth.approval_id,
            now=now,
        )


def test_persistent_approval_is_exact_and_never_available_to_writes(tmp_path: Path) -> None:
    manager = ApprovalManager(tmp_path / "approvals.json")
    policy = AgentPolicy()
    write = request(
        "create_note",
        risk=RiskLevel.LOCAL_WRITE,
        arguments={"title": "one", "content": "body", "format": "txt"},
    )
    with pytest.raises(ApprovalError, match="one-time"):
        manager.grant(
            write,
            ApprovalMode.ALLOW_ALWAYS_EXACT,
            source=ApprovalSource.USER_UI,
            policy=policy,
        )
    with pytest.raises(ApprovalError, match="one-time"):
        manager.grant(
            write,
            ApprovalMode.ALLOW_SESSION,
            source=ApprovalSource.USER_UI,
            policy=policy,
        )

    application = request(
        "open_application",
        risk=RiskLevel.INTERACTIVE,
        arguments={"application_id": "editor"},
    )
    grant = manager.grant(
        application,
        ApprovalMode.ALLOW_ALWAYS_EXACT,
        source=ApprovalSource.USER_UI,
        policy=policy,
    )
    assert manager.authorize(application, policy=policy, approval_id=grant.approval_id)
    changed = application.model_copy(update={"arguments": {"application_id": "other"}})
    with pytest.raises(ApprovalError):
        manager.authorize(changed, policy=policy, approval_id=grant.approval_id)

    serialized = json.loads((tmp_path / "approvals.json").read_text(encoding="utf-8"))
    assert all("token" not in json.dumps(item).casefold() for item in serialized)
