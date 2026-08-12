"""Executable reproductions for the Nivelle Core v2 model runtime."""

import asyncio
from collections.abc import Callable

import httpx
import pytest
from nivelle_core.model_runtime import (
    FakeModelProvider,
    InvalidModelRequestError,
    MalformedModelResponseError,
    ModelRequest,
    ModelRouter,
    ModelState,
    ProviderConnectionError,
    ProviderInternalError,
    ProviderStreamEvent,
    ProviderTimeoutError,
    RequestStatus,
)
from nivelle_protocol.settings import ModelEntry, ModelsSettings


def test_model_choices_are_configuration_data() -> None:
    settings = ModelsSettings(
        mode="external",
        fallback_enabled=True,
        models=[
            ModelEntry(
                id="primary",
                name="Qwen3.5-27B",
                endpoint="http://primary.local:8080/",
                role="primary",
            ),
            ModelEntry(
                id="fallback",
                name="Qwen3.5-9B",
                endpoint="http://fallback.local:8080",
                role="fallback",
            ),
        ],
    )

    assert [model.name for model in settings.models] == ["Qwen3.5-27B", "Qwen3.5-9B"]
    assert settings.models[0].role == "primary"
    assert settings.models[1].role == "fallback"
    assert settings.models[0].endpoint == "http://primary.local:8080"


async def test_primary_success_records_request_lifecycle(
    model_request: Callable[..., ModelRequest],
) -> None:
    primary = FakeModelProvider(
        "fake-primary",
        "primary-model",
        events=[ProviderStreamEvent(delta="primary"), ProviderStreamEvent(final=True)],
    )
    router = ModelRouter(primary)

    response = await router.generate(model_request())
    lifecycle = router.lifecycle(response.request_id)

    assert response.content == "primary"
    assert response.provider_id == "fake-primary"
    assert response.model_id == "primary-model"
    assert response.fallback_used is False
    assert response.latency_ms >= 0
    assert response.completed_at >= response.started_at
    assert lifecycle is not None
    assert lifecycle.status is RequestStatus.COMPLETED
    assert lifecycle.conversation_id == "conversation-1"


@pytest.mark.parametrize(
    ("error", "expected_type"),
    [
        (httpx.ReadTimeout("slow primary"), ProviderTimeoutError),
        (httpx.ConnectError("primary offline"), ProviderConnectionError),
    ],
)
async def test_retryable_primary_error_uses_fallback(
    model_request: Callable[..., ModelRequest],
    error: Exception,
    expected_type: type[Exception],
) -> None:
    primary = FakeModelProvider(
        "fake-primary",
        "primary-model",
        stream_error=error,
    )
    fallback = FakeModelProvider(
        "fake-fallback",
        "fallback-model",
        events=[ProviderStreamEvent(delta="fallback"), ProviderStreamEvent(final=True)],
    )
    router = ModelRouter(primary, fallback)

    response = await router.generate(model_request())

    assert response.content == "fallback"
    assert response.fallback_used is True
    assert response.provider_id == "fake-fallback"
    assert response.attempts[0].error_type == expected_type.error_type
    assert primary.call_count == 1
    assert fallback.call_count == 1


async def test_primary_and_fallback_failure_is_reported(
    model_request: Callable[..., ModelRequest],
) -> None:
    primary = FakeModelProvider("primary", "model-a", stream_error=RuntimeError("a"))
    fallback = FakeModelProvider("fallback", "model-b", stream_error=RuntimeError("b"))
    router = ModelRouter(primary, fallback)
    request = model_request()

    with pytest.raises(ProviderInternalError):
        await router.generate(request)

    lifecycle = router.lifecycle(request.request_id)
    assert lifecycle is not None
    assert lifecycle.status is RequestStatus.FAILED
    assert [attempt.provider_id for attempt in lifecycle.attempts] == ["primary", "fallback"]


async def test_stream_emits_one_authoritative_final_event(
    model_request: Callable[..., ModelRequest],
) -> None:
    primary = FakeModelProvider(
        "primary",
        "model-a",
        events=[
            ProviderStreamEvent(delta="one"),
            ProviderStreamEvent(delta=" two"),
            ProviderStreamEvent(final=True),
        ],
    )

    events = [event async for event in ModelRouter(primary).stream(model_request())]

    assert [event.type for event in events] == ["delta", "delta", "final"]
    assert "".join(event.delta for event in events) == "one two"
    assert events[-1].response is not None
    assert events[-1].response.content == "one two"


async def test_partial_stream_interruption_never_mixes_in_fallback(
    model_request: Callable[..., ModelRequest],
) -> None:
    primary = FakeModelProvider(
        "primary",
        "model-a",
        events=[ProviderStreamEvent(delta="partial")],
        stream_error=ConnectionError("stream disconnected"),
        error_after_events=1,
    )
    fallback = FakeModelProvider("fallback", "model-b")
    router = ModelRouter(primary, fallback)

    with pytest.raises(ProviderConnectionError):
        _ = [event async for event in router.stream(model_request())]

    assert fallback.call_count == 0


async def test_request_cancellation_reaches_active_provider(
    model_request: Callable[..., ModelRequest],
) -> None:
    primary = FakeModelProvider(
        "primary",
        "model-a",
        delay_seconds=30,
    )
    router = ModelRouter(primary)
    request = model_request()

    async def consume() -> None:
        _ = [event async for event in router.stream(request)]

    task = asyncio.create_task(consume())
    await asyncio.wait_for(primary.started.wait(), timeout=1)
    assert await router.cancel(request.request_id) is True
    with pytest.raises(asyncio.CancelledError):
        await task

    lifecycle = router.lifecycle(request.request_id)
    assert lifecycle is not None
    assert lifecycle.status is RequestStatus.CANCELLED
    assert primary.cancel_count >= 1


async def test_duplicate_provider_final_is_blocked(
    model_request: Callable[..., ModelRequest],
) -> None:
    primary = FakeModelProvider(
        "primary",
        "model-a",
        events=[
            ProviderStreamEvent(delta="answer"),
            ProviderStreamEvent(final=True),
            ProviderStreamEvent(final=True),
        ],
    )

    events = [event async for event in ModelRouter(primary).stream(model_request())]

    assert sum(event.type == "final" for event in events) == 1


async def test_malformed_provider_event_is_classified(
    model_request: Callable[..., ModelRequest],
) -> None:
    primary = FakeModelProvider("primary", "model-a", events=[object()])

    with pytest.raises(MalformedModelResponseError):
        _ = [event async for event in ModelRouter(primary).stream(model_request())]


async def test_gateway_remains_online_when_models_are_unavailable() -> None:
    primary = FakeModelProvider(
        "primary",
        "model-a",
        health_state=ModelState.FAILED,
    )
    fallback = FakeModelProvider(
        "fallback",
        "model-b",
        health_state=ModelState.READY,
    )

    snapshot = await ModelRouter(primary, fallback).health()

    assert snapshot.gateway_state == "online"
    assert snapshot.primary.state is ModelState.FAILED
    assert snapshot.fallback is not None
    assert snapshot.fallback.state is ModelState.READY


async def test_invalid_request_does_not_fallback() -> None:
    primary = FakeModelProvider("primary", "model-a")
    fallback = FakeModelProvider("fallback", "model-b")
    router = ModelRouter(primary, fallback)
    invalid = ModelRequest(messages=(), request_id="invalid")

    with pytest.raises(InvalidModelRequestError):
        await router.generate(invalid)

    assert primary.call_count == 0
    assert fallback.call_count == 0
