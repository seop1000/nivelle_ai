from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
for source_root in (ROOT / "apps" / "server", ROOT / "packages"):
    source = str(source_root)
    if source not in sys.path:
        sys.path.insert(0, source)

from nivelle_core.llm import PromptMessage  # noqa: E402
from nivelle_core.model_runtime import (  # noqa: E402
    FakeModelProvider,
    MalformedModelResponseError,
    ModelRequest,
    ModelRouter,
    ModelState,
    ProviderConnectionError,
    ProviderInternalError,
    ProviderStreamEvent,
)


def request(request_id: str) -> ModelRequest:
    return ModelRequest(
        messages=(PromptMessage("user", "simulation"),),
        request_id=request_id,
        conversation_id="simulation",
    )


async def primary_success() -> None:
    response = await ModelRouter(FakeModelProvider("primary", "a")).generate(
        request("primary-success")
    )
    assert response.provider_id == "primary"


async def primary_timeout_fallback_success() -> None:
    primary = FakeModelProvider("primary", "a", stream_error=httpx.ReadTimeout("slow"))
    fallback = FakeModelProvider("fallback", "b")
    response = await ModelRouter(primary, fallback).generate(request("timeout-fallback"))
    assert response.fallback_used


async def primary_connection_fallback_success() -> None:
    primary = FakeModelProvider("primary", "a", stream_error=ConnectionError("offline"))
    fallback = FakeModelProvider("fallback", "b")
    response = await ModelRouter(primary, fallback).generate(request("connection-fallback"))
    assert response.provider_id == "fallback"


async def primary_and_fallback_failure() -> None:
    router = ModelRouter(
        FakeModelProvider("primary", "a", stream_error=RuntimeError("failed")),
        FakeModelProvider("fallback", "b", stream_error=RuntimeError("failed")),
    )
    try:
        await router.generate(request("both-fail"))
    except ProviderInternalError:
        return
    raise AssertionError("both failed providers were reported as success")


async def streaming_normal_completion() -> None:
    events = [
        event
        async for event in ModelRouter(FakeModelProvider("primary", "a")).stream(
            request("stream-success")
        )
    ]
    assert events[-1].type == "final"


async def streaming_interruption() -> None:
    primary = FakeModelProvider(
        "primary",
        "a",
        events=[ProviderStreamEvent(delta="partial")],
        stream_error=ConnectionError("disconnected"),
        error_after_events=1,
    )
    try:
        _ = [event async for event in ModelRouter(primary).stream(request("stream-failed"))]
    except ProviderConnectionError:
        return
    raise AssertionError("interrupted stream was reported as complete")


async def cancellation() -> None:
    primary = FakeModelProvider("primary", "a", delay_seconds=30)
    router = ModelRouter(primary)
    model_request = request("cancel")

    async def consume() -> None:
        _ = [event async for event in router.stream(model_request)]

    task = asyncio.create_task(consume())
    await asyncio.wait_for(primary.started.wait(), timeout=1)
    assert await router.cancel(model_request.request_id)
    try:
        await task
    except asyncio.CancelledError:
        return
    raise AssertionError("cancelled request continued")


async def duplicate_final_blocked() -> None:
    primary = FakeModelProvider(
        "primary",
        "a",
        events=[
            ProviderStreamEvent(delta="answer"),
            ProviderStreamEvent(final=True),
            ProviderStreamEvent(final=True),
        ],
    )
    events = [event async for event in ModelRouter(primary).stream(request("duplicate-final"))]
    assert sum(event.type == "final" for event in events) == 1


async def malformed_response() -> None:
    primary = FakeModelProvider("primary", "a", events=[object()])
    try:
        _ = [event async for event in ModelRouter(primary).stream(request("malformed"))]
    except MalformedModelResponseError:
        return
    raise AssertionError("malformed response was accepted")


async def gateway_online_model_unavailable() -> None:
    primary = FakeModelProvider("primary", "a", health_state=ModelState.FAILED)
    snapshot = await ModelRouter(primary).health()
    assert snapshot.gateway_state == "online"
    assert snapshot.primary.state is ModelState.FAILED


SCENARIOS: dict[str, Callable[[], Awaitable[None]]] = {
    "primary_success": primary_success,
    "primary_timeout_fallback_success": primary_timeout_fallback_success,
    "primary_connection_fallback_success": primary_connection_fallback_success,
    "primary_failure_fallback_failure": primary_and_fallback_failure,
    "streaming_normal_completion": streaming_normal_completion,
    "stream_interruption": streaming_interruption,
    "cancellation": cancellation,
    "duplicate_final_blocked": duplicate_final_blocked,
    "malformed_response": malformed_response,
    "gateway_online_model_unavailable": gateway_online_model_unavailable,
}


async def run() -> int:
    failures = 0
    for name, scenario in SCENARIOS.items():
        try:
            await scenario()
        except Exception as error:
            failures += 1
            print(f"[FAIL] {name}: {type(error).__name__}: {error}")
        else:
            print(f"[PASS] {name}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
