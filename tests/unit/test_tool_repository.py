from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from nivelle_core.database import LATEST_SCHEMA_VERSION, Database
from nivelle_core.repositories import ConversationRepository
from nivelle_core.tool_repository import (
    APPROVED,
    AWAITING_APPROVAL,
    CLIENT_DISCONNECTED,
    COMPLETED,
    PROPOSED,
    QUEUED,
    RUNNING,
    VALIDATED,
    ClientCapability,
    ConflictingToolResultError,
    DuplicateIdempotencyKeyError,
    DuplicateToolCallError,
    InvalidToolTransitionError,
    ToolCallCreate,
    ToolRepository,
    ToolTargetMismatchError,
)

T0 = "2026-08-04T00:00:00+00:00"
T1 = "2026-08-04T00:01:00+00:00"
T2 = "2026-08-04T00:02:00+00:00"
T3 = "2026-08-04T00:03:00+00:00"


async def _seed_database(tmp_path: Path) -> tuple[Database, str, str]:
    database = Database(tmp_path / "nivelle.db")
    await database.initialize()
    conversations = ConversationRepository(database)
    conversation = await conversations.create("한글 대화는 그대로 보존합니다")
    message = await conversations.add_message(
        str(conversation["id"]),
        "user",
        "니벨, 이 UUID와 한글을 바꾸지 마세요.",
        message_id="11111111-1111-4111-8111-111111111111",
        client_message_id="22222222-2222-4222-8222-222222222222",
        request_id="33333333-3333-4333-8333-333333333333",
    )
    return database, str(conversation["id"]), str(message["id"])


def _call(
    conversation_id: str,
    user_message_id: str,
    *,
    suffix: str = "1",
    request_id: str = "request-1",
    session_id: str = "session-1",
    approval_mode: str = "allow_once",
) -> ToolCallCreate:
    return ToolCallCreate(
        tool_call_id=f"tool-call-{suffix}",
        request_id=request_id,
        idempotency_key=f"idem-{suffix}",
        conversation_id=conversation_id,
        user_message_id=user_message_id,
        assistant_message_id=None,
        target_client_id="client-1",
        target_session_id=session_id,
        tool_name="get_system_status",
        tool_version="1.0",
        risk_level="SAFE_STATUS",
        arguments_summary="metric=cpu token=must-not-leak",
        approval_mode=approval_mode,
    )


def _capability(
    *, session_id: str = "session-1", expires_at: str = T3
) -> ClientCapability:
    return ClientCapability(
        client_id="client-1",
        session_id=session_id,
        platform="windows",
        app_version="0.4.0",
        protocol_version="1.0",
        tool_name="get_system_status",
        tool_version="1.0",
        enabled=True,
        implementation_available=True,
        risk_level="SAFE_STATUS",
        default_approval_required=False,
        default_timeout_ms=1_000,
        maximum_timeout_ms=5_000,
        maximum_result_size=10_000,
        expires_at=expires_at,
    )


@pytest.mark.asyncio
async def test_v7_to_v8_migration_preserves_uuid_korean_and_history(tmp_path: Path) -> None:
    database, conversation_id, message_id = await _seed_database(tmp_path)
    connection = sqlite3.connect(database.path)
    try:
        connection.execute("DROP TABLE tool_call_events")
        connection.execute("DROP TABLE client_capabilities")
        connection.execute("DROP TABLE tool_idempotency")
        connection.execute("DROP TABLE tool_calls")
        connection.execute("DELETE FROM schema_versions WHERE version=8")
        connection.commit()
    finally:
        connection.close()

    migrated = Database(database.path)
    await migrated.initialize()

    assert LATEST_SCHEMA_VERSION == 8
    assert migrated.last_migration_backup is not None
    versions = await migrated.fetchall("SELECT version FROM schema_versions ORDER BY version")
    assert [int(row["version"]) for row in versions] == list(range(1, 9))
    message = await migrated.fetchone(
        "SELECT id,conversation_id,content FROM messages WHERE id=?", (message_id,)
    )
    assert message is not None
    assert dict(message) == {
        "id": message_id,
        "conversation_id": conversation_id,
        "content": "니벨, 이 UUID와 한글을 바꾸지 마세요.",
    }
    tables = await migrated.fetchall(
        """
        SELECT name FROM sqlite_master
        WHERE type='table' AND name LIKE 'tool_%' OR name='client_capabilities'
        """
    )
    assert {str(row["name"]) for row in tables} >= {
        "tool_calls",
        "tool_call_events",
        "client_capabilities",
        "tool_idempotency",
    }


@pytest.mark.asyncio
async def test_tool_identity_is_unique_exact_replay_is_safe_and_summaries_are_redacted(
    tmp_path: Path,
) -> None:
    database, conversation_id, message_id = await _seed_database(tmp_path)
    repository = ToolRepository(database)
    call = _call(conversation_id, message_id)

    created, was_created = await repository.create_tool_call(
        call,
        max_calls_per_turn=3,
        idempotency_retention_days=90,
        created_at=T0,
    )
    replayed, replay_created = await repository.create_tool_call(
        call,
        max_calls_per_turn=3,
        idempotency_retention_days=90,
        created_at=T1,
    )

    assert was_created is True
    assert replay_created is False
    assert replayed["tool_call_id"] == created["tool_call_id"]
    assert created["arguments_summary"] == "metric=cpu token=[REDACTED]"
    events = await repository.list_events(call.tool_call_id)
    assert [(event["sequence"], event["to_status"]) for event in events] == [
        (1, PROPOSED)
    ]
    columns = await database.fetchall("PRAGMA table_info(tool_calls)")
    column_names = {str(column["name"]) for column in columns}
    assert "arguments_summary" in column_names
    assert "result_summary" in column_names
    assert "arguments_json" not in column_names
    assert "result_json" not in column_names

    with pytest.raises(DuplicateToolCallError):
        await repository.create_tool_call(
            replace(call, idempotency_key="different-key"),
            max_calls_per_turn=3,
            idempotency_retention_days=90,
        )
    with pytest.raises(DuplicateIdempotencyKeyError):
        await repository.create_tool_call(
            replace(call, tool_call_id="different-call"),
            max_calls_per_turn=3,
            idempotency_retention_days=90,
        )


@pytest.mark.asyncio
async def test_monotonic_transitions_duplicate_approval_and_result_are_safe(
    tmp_path: Path,
) -> None:
    database, conversation_id, message_id = await _seed_database(tmp_path)
    repository = ToolRepository(database)
    call = _call(conversation_id, message_id)
    await repository.create_tool_call(
        call, max_calls_per_turn=3, idempotency_retention_days=90, created_at=T0
    )

    await repository.transition(call.tool_call_id, VALIDATED, occurred_at=T1)
    await repository.transition(call.tool_call_id, AWAITING_APPROVAL, occurred_at=T1)
    assert await repository.record_approval(
        call.tool_call_id,
        approved=True,
        target_client_id="client-1",
        target_session_id="session-1",
        occurred_at=T1,
    )
    assert not await repository.record_approval(
        call.tool_call_id,
        approved=True,
        target_client_id="client-1",
        target_session_id="session-1",
        occurred_at=T2,
    )
    with pytest.raises(ToolTargetMismatchError):
        await repository.record_approval(
            call.tool_call_id,
            approved=True,
            target_client_id="client-2",
            target_session_id="session-1",
        )
    await repository.queue_with_parallel_limit(
        call.tool_call_id,
        target_client_id="client-1",
        target_session_id="session-1",
        max_parallel_calls_per_client=2,
        occurred_at=T2,
    )
    await repository.transition(
        call.tool_call_id,
        RUNNING,
        target_client_id="client-1",
        target_session_id="session-1",
        occurred_at=T2,
    )
    with pytest.raises(ToolTargetMismatchError):
        await repository.record_result(
            call.tool_call_id,
            status=COMPLETED,
            target_client_id="client-1",
            target_session_id="wrong-session",
            error_code=None,
            duration_ms=1,
        )
    assert await repository.record_result(
        call.tool_call_id,
        status=COMPLETED,
        target_client_id="client-1",
        target_session_id="session-1",
        error_code=None,
        duration_ms=42,
        occurred_at=T3,
    )
    assert not await repository.record_result(
        call.tool_call_id,
        status=COMPLETED,
        target_client_id="client-1",
        target_session_id="session-1",
        error_code=None,
        duration_ms=42,
        occurred_at=T3,
    )
    with pytest.raises(ConflictingToolResultError):
        await repository.record_result(
            call.tool_call_id,
            status=COMPLETED,
            target_client_id="client-1",
            target_session_id="session-1",
            error_code=None,
            duration_ms=43,
        )
    with pytest.raises(InvalidToolTransitionError):
        await repository.transition(call.tool_call_id, RUNNING)

    persisted = await repository.get_tool_call(call.tool_call_id)
    assert persisted is not None
    assert persisted["status"] == COMPLETED
    assert persisted["result_summary"] == (
        "tool=get_system_status; status=completed; error_code=none"
    )
    events = await repository.list_events(call.tool_call_id)
    assert [event["to_status"] for event in events] == [
        PROPOSED,
        VALIDATED,
        AWAITING_APPROVAL,
        APPROVED,
        QUEUED,
        RUNNING,
        COMPLETED,
    ]
    assert [event["sequence"] for event in events] == list(range(1, 8))


@pytest.mark.asyncio
async def test_capabilities_are_exact_session_scoped_expire_and_disconnect_calls(
    tmp_path: Path,
) -> None:
    database, conversation_id, message_id = await _seed_database(tmp_path)
    repository = ToolRepository(database)
    await repository.replace_capabilities([_capability()], connected_at=T0)
    assert (
        await repository.get_live_capability(
            client_id="client-1",
            session_id="session-1",
            tool_name="get_system_status",
            tool_version="1.0",
            at=T1,
        )
        is not None
    )
    assert (
        await repository.get_live_capability(
            client_id="client-1",
            session_id="session-1",
            tool_name="get_system_status",
            tool_version="1.0",
            at="2026-08-04T00:04:00+00:00",
        )
        is None
    )
    assert (
        await repository.get_live_capability(
            client_id="client-1",
            session_id="another-session",
            tool_name="get_system_status",
            tool_version="1.0",
            at=T1,
        )
        is None
    )

    call = _call(conversation_id, message_id)
    await repository.create_tool_call(
        call, max_calls_per_turn=3, idempotency_retention_days=90, created_at=T0
    )
    assert await repository.disconnect_session(
        client_id="client-1", session_id="session-1", occurred_at=T1
    ) == 1
    persisted = await repository.get_tool_call(call.tool_call_id)
    assert persisted is not None
    assert persisted["status"] == CLIENT_DISCONNECTED
    assert (
        await repository.get_live_capability(
            client_id="client-1",
            session_id="session-1",
            tool_name="get_system_status",
            tool_version="1.0",
            at=T1,
        )
        is None
    )
