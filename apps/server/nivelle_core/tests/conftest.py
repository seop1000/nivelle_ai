"""Focused fixtures for the Core v2 model-runtime recovery tests."""

from collections.abc import Callable

import pytest
from nivelle_core.llm import PromptMessage
from nivelle_core.model_runtime import ModelRequest


@pytest.fixture
def model_request() -> Callable[..., ModelRequest]:
    def build(
        content: str = "hello",
        *,
        request_id: str = "request-1",
        conversation_id: str = "conversation-1",
    ) -> ModelRequest:
        return ModelRequest(
            messages=(PromptMessage("user", content),),
            request_id=request_id,
            conversation_id=conversation_id,
        )

    return build
