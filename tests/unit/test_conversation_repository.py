from pathlib import Path

import aiosqlite
import pytest
from nivelle_core.database import Database
from nivelle_core.repositories import ConversationRepository


@pytest.mark.asyncio
async def test_terminal_message_state_cannot_regress_after_completion(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "nivelle.db")
    await database.initialize()
    repository = ConversationRepository(database)
    conversation = await repository.create("단조 상태 전이")
    _, assistant = await repository.allocate_turn(
        conversation["id"],
        "단조 상태 전이",
        user_metadata={"request_id": "request-1"},
        assistant_metadata={"in_reply_to_client_message_id": "client-1"},
        client_message_id="client-1",
    )

    completed = await repository.update_message(
        str(assistant["id"]),
        content="완료된 답변",
        state="completed",
        expected_state="generating",
    )
    late_cancellation = await repository.update_message(
        str(assistant["id"]),
        content="부분 답변",
        state="interrupted",
        expected_state="generating",
    )

    assert completed is not None and completed["state"] == "completed"
    assert late_cancellation is not None
    assert late_cancellation["state"] == "completed"
    assert late_cancellation["content"] == "완료된 답변"


@pytest.mark.asyncio
async def test_turn_allocation_persists_user_and_assistant_together(tmp_path: Path) -> None:
    database = Database(tmp_path / "nivelle.db")
    await database.initialize()
    repository = ConversationRepository(database)
    conversation = await repository.create("원자적 배정")

    user, assistant = await repository.allocate_turn(
        conversation["id"],
        "원자적 배정",
        user_metadata={"client_message_id": "client-atomic"},
        assistant_metadata={"in_reply_to_client_message_id": "client-atomic"},
        client_message_id="client-atomic",
        request_id="request-atomic",
    )
    messages = await repository.messages(conversation["id"])

    assert [message["id"] for message in messages] == [user["id"], assistant["id"]]
    assert [message["state"] for message in messages] == ["completed", "generating"]
    assert messages[0]["request_id"] == "request-atomic"
    assert messages[1]["request_id"] is None


@pytest.mark.asyncio
async def test_request_id_is_durable_and_unique(tmp_path: Path) -> None:
    database = Database(tmp_path / "nivelle.db")
    await database.initialize()
    repository = ConversationRepository(database)
    conversation = await repository.create("요청 ID")
    await repository.allocate_turn(
        conversation["id"],
        "첫 요청",
        user_metadata={"request_id": "request-unique"},
        assistant_metadata={"request_id": "request-unique"},
        client_message_id="client-one",
        request_id="request-unique",
    )

    with pytest.raises(aiosqlite.IntegrityError):
        await repository.allocate_turn(
            conversation["id"],
            "중복 요청",
            user_metadata={"request_id": "request-unique"},
            assistant_metadata={"request_id": "request-unique"},
            client_message_id="client-two",
            request_id="request-unique",
        )

    persisted = await repository.find_user_by_request_id("request-unique")
    assert persisted is not None
    assert persisted["content"] == "첫 요청"
    assert len(await repository.messages(conversation["id"])) == 2


@pytest.mark.asyncio
async def test_retry_target_is_unique_and_failed_allocation_leaves_no_partial_turn(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "nivelle.db")
    await database.initialize()
    repository = ConversationRepository(database)
    conversation = await repository.create("재시도 관계")
    await repository.allocate_turn(
        conversation["id"],
        "첫 재시도",
        user_metadata={"retry_of_client_message_id": "original"},
        assistant_metadata={"in_reply_to_client_message_id": "retry-one"},
        client_message_id="retry-one",
        retry_of_client_message_id="original",
    )

    with pytest.raises(aiosqlite.IntegrityError):
        await repository.allocate_turn(
            conversation["id"],
            "중복 재시도",
            user_metadata={"retry_of_client_message_id": "original"},
            assistant_metadata={"in_reply_to_client_message_id": "retry-two"},
            client_message_id="retry-two",
            retry_of_client_message_id="original",
        )

    messages = await repository.messages(conversation["id"])
    assert [message["client_message_id"] for message in messages] == [
        "retry-one",
        None,
    ]
