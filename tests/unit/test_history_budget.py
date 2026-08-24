import json
from pathlib import Path

import pytest
from nivelle_core.database import Database
from nivelle_core.llm import PromptMessage
from nivelle_core.persona import PromptContextBuilder
from nivelle_core.repositories import ConversationRepository


@pytest.mark.asyncio
async def test_completed_history_uses_index_and_returns_only_complete_turns(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "nivelle.db")
    await database.initialize()
    repository = ConversationRepository(database)
    conversation = await repository.create("history limit")

    for index in range(25):
            await database.execute(
                """
                INSERT INTO messages(
                    id,conversation_id,role,content,created_at,state,
                    prompt_tokens,completion_tokens,metadata_json
                ) VALUES(?,?,?,?,?,?,NULL,NULL,?)
                """,
            (
                f"message-{index:02}",
                conversation["id"],
                "user" if index % 2 == 0 else "assistant",
                f"history-{index:02}",
                f"2026-01-01T00:00:{index:02}+00:00",
                "completed",
                json.dumps({}),
            ),
        )

    history = await repository.completed_messages(conversation["id"])
    indexes = await database.fetchall("PRAGMA index_list('messages')")

    assert history is not None
    assert [message["content"] for message in history] == [
        f"history-{index:02}" for index in range(6, 24)
    ]
    assert [message["role"] for message in history] == [
        "user" if index % 2 == 0 else "assistant" for index in range(6, 24)
    ]
    assert "idx_messages_conversation_history" in {row["name"] for row in indexes}


def test_prompt_history_respects_context_character_budget(tmp_path: Path) -> None:
    builder = PromptContextBuilder(tmp_path / "persona")
    request = "현재 질문"
    system_message = builder.build([], request)[0]
    recent = [PromptMessage("user", "최근 질문"), PromptMessage("assistant", "최근 답변")]
    history = [
        PromptMessage("user", "이전 질문" * 20),
        PromptMessage("assistant", "이전 답변" * 20),
        *recent,
    ]
    max_output_tokens = 32
    recent_characters = sum(len(message.content) for message in recent)
    context_size = (
        len(system_message.content)
        + len(request)
        + recent_characters
        + max_output_tokens
    )

    prompt = builder.build(
        history,
        request,
        context_size=context_size,
        max_output_tokens=max_output_tokens,
    )

    assert prompt[1:-1] == recent
    assert sum(len(message.content) for message in prompt) <= context_size - max_output_tokens
