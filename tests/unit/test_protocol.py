import tomllib
from pathlib import Path

import pytest
from nivelle_core import __version__ as server_version
from nivelle_link import __version__ as client_version
from nivelle_protocol.chat import (
    AssistantCompletedPayload,
    AssistantContextPayload,
    ChatContextPayload,
    ChatRequest,
    RetrievalContext,
    RuntimeConnectionContext,
)
from nivelle_protocol.memory import MemoryContextItem
from nivelle_protocol.settings import ModelsSettings
from nivelle_protocol.version import APP_VERSION
from pydantic import ValidationError


def test_chat_rejects_empty_content() -> None:
    with pytest.raises(ValidationError):
        ChatRequest(request_id="4c6007bc-f112-41ae-8baa-3bb7c42c2d66", content="")


def test_chat_context_validates_memory_evidence() -> None:
    payload = AssistantContextPayload(
        conversation_id="4c6007bc-f112-41ae-8baa-3bb7c42c2d66",
        user_message_id="919d58dc-2d3a-4e8c-aafd-436babb113bb",
        assistant_message_id="79e31c74-8171-4e58-b9ec-f756030a8c85",
        client_message_id="c972e7f6-cc9f-4f70-b196-6f1b164bd4ac",
        query="답변 언어는?",
        retrieval=RetrievalContext(
            backend="sqlite_hybrid", top_k=5, candidate_count=2
        ),
        memories=[
            MemoryContextItem(
                id="memory-1",
                content="답변은 한국어로 작성한다",
                category="instruction",
                priority=80,
                relevance_score=0.75,
            )
        ],
    )

    assert payload.memories[0].included is True
    assert payload.memories[0].memory_id == "memory-1"
    legacy = ChatContextPayload(
        conversation_id=payload.conversation_id, memories=payload.memories
    )
    assert legacy.memories[0].memory_id == "memory-1"
    with pytest.raises(ValidationError):
        MemoryContextItem(
            id="memory-2",
            content="범위를 벗어난 점수",
            category="other",
            priority=50,
            relevance_score=1.01,
        )


def test_chat_request_defaults_stable_client_message_id_and_validates_runtime() -> None:
    request_id = "4c6007bc-f112-41ae-8baa-3bb7c42c2d66"
    request = ChatRequest(
        request_id=request_id,
        content="현재 연결 서버는?",
        runtime_context=RuntimeConnectionContext(
            profile_id=" primary ",
            connection_type="local",
            host="192.0.2.10",
            port=8765,
            tls=False,
            client_version="0.3.1",
            latency_ms=62.49,
        ),
    )

    assert str(request.client_message_id) == request_id
    assert request.runtime_context is not None
    assert request.runtime_context.profile_id == "primary"
    with pytest.raises(ValidationError):
        RuntimeConnectionContext(
            profile_id="primary",
            connection_type="local",
            host="server.local",
            port=8765,
            tls=False,
            authorization="secret",
        )


def test_assistant_completed_payload_requires_one_canonical_message_id() -> None:
    assistant_id = "79e31c74-8171-4e58-b9ec-f756030a8c85"
    common = {
        "conversation_id": "4c6007bc-f112-41ae-8baa-3bb7c42c2d66",
        "client_message_id": "c972e7f6-cc9f-4f70-b196-6f1b164bd4ac",
        "message_id": assistant_id,
        "assistant_message_id": assistant_id,
        "message": {
            "id": assistant_id,
            "role": "assistant",
            "content": "완료",
            "state": "completed",
        },
    }

    assert str(AssistantCompletedPayload.model_validate(common).message.id) == assistant_id
    mismatched = dict(common)
    mismatched["assistant_message_id"] = "919d58dc-2d3a-4e8c-aafd-436babb113bb"
    with pytest.raises(ValidationError):
        AssistantCompletedPayload.model_validate(mismatched)


def test_managed_requires_binary() -> None:
    with pytest.raises(ValidationError):
        ModelsSettings(mode="managed")


def test_application_version_sources_are_in_sync() -> None:
    root = Path(__file__).parents[2]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))

    assert (root / "VERSION").read_text(encoding="ascii").strip() == APP_VERSION
    assert "version" in project["project"]["dynamic"]
    assert project["tool"]["hatch"]["version"]["path"] == "VERSION"
    assert client_version == APP_VERSION
    assert server_version == APP_VERSION
