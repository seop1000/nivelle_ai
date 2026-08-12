from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from nivelle_link.agent.errors import AgentError
from nivelle_link.agent.idempotency import IdempotencyCache
from nivelle_link.agent.models import AgentToolRequest
from nivelle_link.agent.note import NoteWriter
from nivelle_link.agent.protocol_adapter import normalize_result_payload
from nivelle_link.agent.reminder import ReminderStore


def _request(
    tool_name: str,
    arguments: dict[str, object],
    *,
    conversation_id: str,
    idempotency_key: str | None = None,
) -> AgentToolRequest:
    return AgentToolRequest(
        tool_call_id=str(uuid4()),
        request_id=str(uuid4()),
        idempotency_key=idempotency_key or str(uuid4()),
        conversation_id=conversation_id,
        user_message_id=str(uuid4()),
        target_client_id=str(uuid4()),
        target_session_id=str(uuid4()),
        tool_name=tool_name,
        tool_version="1.0",
        arguments=arguments,
        risk_level="LOCAL_WRITE",
        created_at=datetime.now(UTC),
        timeout_ms=10_000,
        user_intent_summary=f"Test {tool_name}",
    )


def test_idempotency_business_reconciliation_is_exact_and_expires_after_seven_days(
    tmp_path: Path,
) -> None:
    cache = IdempotencyCache(tmp_path / "idempotency.json")
    started_at = datetime(2030, 1, 1, tzinfo=UTC)
    calls = 0

    def operation() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"write_id": calls}

    business_arguments = {
        "conversation_id": "conversation-one",
        "arguments": {"title": "same", "content": "same", "format": "txt"},
    }
    first, first_replayed = cache.execute_once(
        idempotency_key="first-key",
        tool_name="create_note",
        arguments={"title": "same", "content": "same"},
        business_arguments=business_arguments,
        reconcile_completed=True,
        operation=operation,
        now=started_at,
    )
    retry, retry_replayed = cache.execute_once(
        idempotency_key="retry-key",
        tool_name="create_note",
        arguments={"title": "same", "content": "same", "format": "txt"},
        business_arguments=business_arguments,
        reconcile_completed=True,
        operation=operation,
        now=started_at + timedelta(days=6),
    )

    assert first == retry == {"write_id": 1}
    assert first_replayed is False
    assert retry_replayed is True
    assert calls == 1

    expired, expired_replayed = cache.execute_once(
        idempotency_key="after-retention-key",
        tool_name="create_note",
        arguments={"title": "same", "content": "same", "format": "txt"},
        business_arguments=business_arguments,
        reconcile_completed=True,
        operation=operation,
        now=started_at + timedelta(days=8),
    )

    assert expired == {"write_id": 2}
    assert expired_replayed is False
    assert calls == 2


def test_note_retry_with_new_ids_reuses_result_but_other_business_actions_write(
    tmp_path: Path,
) -> None:
    notes_directory = tmp_path / "notes"
    writer = NoteWriter(notes_directory, IdempotencyCache(tmp_path / "idempotency.json"))
    conversation_id = str(uuid4())
    original = _request(
        "create_note",
        {"title": "handoff", "content": "one"},
        conversation_id=conversation_id,
    )
    retry = _request(
        "create_note",
        {"title": "handoff", "content": "one", "format": "txt"},
        conversation_id=conversation_id,
    )

    first, first_replayed = writer.create(original, original.arguments)
    repeated, repeated_replayed = writer.create(retry, retry.arguments)

    assert repeated == first
    assert first_replayed is False
    assert repeated_replayed is True
    normalized = normalize_result_payload(
        "create_note", repeated, replayed=repeated_replayed
    )
    assert normalized.model_dump()["already_executed"] is True
    assert len(list(notes_directory.glob("*.txt"))) == 1

    other_conversation = _request(
        "create_note",
        {"title": "handoff", "content": "one", "format": "txt"},
        conversation_id=str(uuid4()),
    )
    other_content = _request(
        "create_note",
        {"title": "handoff", "content": "two", "format": "txt"},
        conversation_id=conversation_id,
    )
    _, other_conversation_replayed = writer.create(
        other_conversation, other_conversation.arguments
    )
    _, other_content_replayed = writer.create(other_content, other_content.arguments)

    assert other_conversation_replayed is False
    assert other_content_replayed is False
    assert len(list(notes_directory.glob("*.txt"))) == 3


def test_reminder_retry_with_new_ids_reuses_one_row_for_seven_days(
    tmp_path: Path,
) -> None:
    clock = [datetime(2030, 1, 1, tzinfo=UTC)]
    store = ReminderStore(tmp_path / "reminders.db", now=lambda: clock[0])
    conversation_id = str(uuid4())
    arguments: dict[str, object] = {
        "title": "handoff",
        "reminder_text": "check the server",
        "scheduled_at": datetime(2032, 1, 1, tzinfo=UTC).isoformat(),
        "timezone": "Asia/Seoul",
    }
    original = _request(
        "set_reminder", arguments, conversation_id=conversation_id
    )
    retry = _request("set_reminder", arguments, conversation_id=conversation_id)

    first, first_replayed = store.create(original, original.arguments)
    clock[0] += timedelta(days=6)
    repeated, repeated_replayed = store.create(retry, retry.arguments)

    assert repeated["reminder_id"] == first["reminder_id"]
    assert repeated["replayed"] is True
    assert first_replayed is False
    assert repeated_replayed is True
    normalized = normalize_result_payload(
        "set_reminder", repeated, replayed=repeated_replayed
    )
    assert normalized.model_dump()["already_executed"] is True

    reused_key_with_other_arguments = _request(
        "set_reminder",
        arguments | {"reminder_text": "different"},
        conversation_id=conversation_id,
        idempotency_key=retry.idempotency_key,
    )
    with pytest.raises(AgentError, match="different reminder"):
        store.create(reused_key_with_other_arguments, reused_key_with_other_arguments.arguments)

    other_conversation = _request(
        "set_reminder", arguments, conversation_id=str(uuid4())
    )
    other_content = _request(
        "set_reminder",
        arguments | {"reminder_text": "different"},
        conversation_id=conversation_id,
    )
    _, other_conversation_replayed = store.create(
        other_conversation, other_conversation.arguments
    )
    _, other_content_replayed = store.create(other_content, other_content.arguments)

    assert other_conversation_replayed is False
    assert other_content_replayed is False

    clock[0] = datetime(2030, 1, 9, tzinfo=UTC)
    after_retention = _request(
        "set_reminder", arguments, conversation_id=conversation_id
    )
    _, after_retention_replayed = store.create(
        after_retention, after_retention.arguments
    )
    assert after_retention_replayed is False

    with closing(sqlite3.connect(tmp_path / "reminders.db")) as connection:
        reminder_count = connection.execute("SELECT COUNT(*) FROM reminders").fetchone()
        alias_count = connection.execute(
            "SELECT COUNT(*) FROM reminder_idempotency_aliases"
        ).fetchone()
    assert reminder_count == (4,)
    assert alias_count == (1,)
