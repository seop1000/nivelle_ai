from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import Enum
from typing import Protocol, cast
from uuid import uuid4

import httpx

from .backend_status import probe_openai_backend
from .llm import LLMProvider, MetricsCallback, PromptMessage, provider_stream

logger = logging.getLogger(__name__)


class ModelState(str, Enum):
    UNAVAILABLE = "unavailable"
    STARTING = "starting"
    READY = "ready"
    BUSY = "busy"
    DEGRADED = "degraded"
    FAILED = "failed"


class RequestStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class ModelRuntimeError(RuntimeError):
    retryable = False
    error_type = "model_runtime_error"


class ProviderConnectionError(ModelRuntimeError):
    retryable = True
    error_type = "connection_error"


class ProviderTimeoutError(ModelRuntimeError):
    retryable = True
    error_type = "timeout"


class ModelUnavailableError(ModelRuntimeError):
    retryable = True
    error_type = "model_unavailable"


class InvalidModelRequestError(ModelRuntimeError):
    error_type = "invalid_request"


class ModelCancelledError(ModelRuntimeError):
    error_type = "cancelled"


class ProviderInternalError(ModelRuntimeError):
    retryable = True
    error_type = "provider_internal_error"


class MalformedModelResponseError(ModelRuntimeError):
    retryable = True
    error_type = "malformed_response"


@dataclass(frozen=True)
class ModelCapabilities:
    text: bool = True
    vision: bool = False
    tool_calling: bool = False
    streaming: bool = True
    structured_output: bool = False
    context_length: int | None = None


@dataclass(frozen=True)
class ProviderHealth:
    provider_id: str
    model_id: str
    state: ModelState
    endpoint: str | None = None
    error_type: str | None = None

    @property
    def available(self) -> bool:
        return self.state in {ModelState.READY, ModelState.DEGRADED}


@dataclass(frozen=True)
class ModelRequest:
    messages: tuple[PromptMessage, ...]
    request_id: str = field(default_factory=lambda: str(uuid4()))
    conversation_id: str | None = None


@dataclass(frozen=True)
class ProviderStreamEvent:
    delta: str = ""
    final: bool = False


@dataclass(frozen=True)
class ProviderAttempt:
    provider_id: str
    model_id: str
    error_type: str | None = None


@dataclass(frozen=True)
class ModelResponse:
    content: str
    request_id: str
    conversation_id: str | None
    provider_id: str
    model_id: str
    started_at: datetime
    completed_at: datetime
    latency_ms: float
    status: RequestStatus
    fallback_used: bool
    attempts: tuple[ProviderAttempt, ...]


@dataclass(frozen=True)
class ModelStreamEvent:
    type: str
    request_id: str
    sequence: int
    delta: str = ""
    response: ModelResponse | None = None


@dataclass
class ModelRequestRecord:
    request_id: str
    conversation_id: str | None
    started_at: datetime
    status: RequestStatus = RequestStatus.PENDING
    completed_at: datetime | None = None
    latency_ms: float | None = None
    provider_id: str | None = None
    model_id: str | None = None
    fallback_used: bool = False
    error_type: str | None = None
    attempts: list[ProviderAttempt] = field(default_factory=list)


@dataclass(frozen=True)
class ModelRuntimeSnapshot:
    gateway_state: str
    primary: ProviderHealth
    fallback: ProviderHealth | None


class ModelProvider(Protocol):
    provider_id: str
    model_id: str
    endpoint: str | None
    timeout: float
    capabilities: ModelCapabilities

    async def health(self) -> ProviderHealth: ...

    async def generate(self, request: ModelRequest) -> str: ...

    def stream(self, request: ModelRequest) -> AsyncIterator[ProviderStreamEvent]: ...

    async def cancel(self, request_id: str) -> None: ...


HealthCheck = Callable[[], Awaitable[ProviderHealth]]


class StreamingLLMModelProvider:
    """Adapt the existing text-only LLM interface to the Core v2 provider contract."""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        provider_id: str,
        model_id: str,
        endpoint: str | None,
        timeout: float,
        health_check: HealthCheck | None = None,
        on_metrics: MetricsCallback | None = None,
        capabilities: ModelCapabilities | None = None,
    ) -> None:
        self.provider = provider
        self.provider_id = provider_id
        self.model_id = model_id
        self.endpoint = endpoint
        self.timeout = timeout
        self._health_check = health_check
        self._on_metrics = on_metrics
        self.capabilities = capabilities or ModelCapabilities()
        self._active_tasks: dict[str, asyncio.Task[object]] = {}

    async def health(self) -> ProviderHealth:
        if self._health_check is not None:
            return await self._health_check()
        return ProviderHealth(
            provider_id=self.provider_id,
            model_id=self.model_id,
            state=ModelState.READY,
            endpoint=self.endpoint,
        )

    async def generate(self, request: ModelRequest) -> str:
        chunks: list[str] = []
        async for event in self.stream(request):
            if event.delta:
                chunks.append(event.delta)
        return "".join(chunks)

    async def stream(self, request: ModelRequest) -> AsyncIterator[ProviderStreamEvent]:
        task = asyncio.current_task()
        if task is not None:
            self._active_tasks[request.request_id] = cast(asyncio.Task[object], task)
        try:
            async for chunk in provider_stream(
                self.provider,
                request.messages,
                on_metrics=self._on_metrics,
            ):
                yield ProviderStreamEvent(delta=chunk)
            yield ProviderStreamEvent(final=True)
        finally:
            self._active_tasks.pop(request.request_id, None)

    async def cancel(self, request_id: str) -> None:
        task = self._active_tasks.get(request_id)
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()


class OpenAICompatibleModelProvider(StreamingLLMModelProvider):
    """Provider adapter for an OpenAI-compatible HTTP backend."""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        provider_id: str,
        model_id: str,
        endpoint: str,
        timeout: float,
        on_metrics: MetricsCallback | None = None,
        health_transport: httpx.AsyncBaseTransport | None = None,
        capabilities: ModelCapabilities | None = None,
    ) -> None:
        async def check_health() -> ProviderHealth:
            result = await probe_openai_backend(
                endpoint,
                request_timeout=min(timeout, 5.0),
                transport=health_transport,
            )
            state = {
                "ready": ModelState.READY,
                "unreachable": ModelState.UNAVAILABLE,
            }.get(str(result.get("state")), ModelState.FAILED)
            error_type = (
                "connection_error"
                if state is ModelState.UNAVAILABLE
                else "provider_health_error"
                if state is ModelState.FAILED
                else None
            )
            return ProviderHealth(
                provider_id=provider_id,
                model_id=model_id,
                state=state,
                endpoint=endpoint.rstrip("/"),
                error_type=error_type,
            )

        super().__init__(
            provider,
            provider_id=provider_id,
            model_id=model_id,
            endpoint=endpoint.rstrip("/"),
            timeout=timeout,
            health_check=check_health,
            on_metrics=on_metrics,
            capabilities=capabilities,
        )


class FakeModelProvider:
    """Deterministic provider used by tests and the standalone simulation harness."""

    def __init__(
        self,
        provider_id: str,
        model_id: str,
        *,
        events: Sequence[object] | None = None,
        health_state: ModelState = ModelState.READY,
        stream_error: BaseException | None = None,
        error_after_events: int = 0,
        delay_seconds: float = 0,
    ) -> None:
        self.provider_id = provider_id
        self.model_id = model_id
        self.endpoint = f"fake://{provider_id}/{model_id}"
        self.timeout = 1.0
        self.capabilities = ModelCapabilities()
        self.events = list(
            events
            if events is not None
            else [ProviderStreamEvent(delta="fake response"), ProviderStreamEvent(final=True)]
        )
        self.health_state = health_state
        self.stream_error = stream_error
        self.error_after_events = error_after_events
        self.delay_seconds = delay_seconds
        self.call_count = 0
        self.cancel_count = 0
        self.started = asyncio.Event()
        self._cancelled: set[str] = set()

    async def health(self) -> ProviderHealth:
        return ProviderHealth(
            provider_id=self.provider_id,
            model_id=self.model_id,
            state=self.health_state,
            endpoint=self.endpoint,
        )

    async def generate(self, request: ModelRequest) -> str:
        chunks: list[str] = []
        async for event in self.stream(request):
            if isinstance(event, ProviderStreamEvent) and event.delta:
                chunks.append(event.delta)
        return "".join(chunks)

    async def stream(self, request: ModelRequest) -> AsyncIterator[ProviderStreamEvent]:
        self.call_count += 1
        self.started.set()
        for index, event in enumerate(self.events):
            if self.stream_error is not None and index == self.error_after_events:
                raise self.stream_error
            if self.delay_seconds:
                await asyncio.sleep(self.delay_seconds)
            if request.request_id in self._cancelled:
                raise ModelCancelledError(f"request {request.request_id} was cancelled")
            yield cast(ProviderStreamEvent, event)
        if self.stream_error is not None and self.error_after_events >= len(self.events):
            raise self.stream_error

    async def cancel(self, request_id: str) -> None:
        self.cancel_count += 1
        self._cancelled.add(request_id)


def classify_provider_error(error: BaseException) -> ModelRuntimeError:
    if isinstance(error, ModelRuntimeError):
        return error
    if isinstance(error, (httpx.TimeoutException, TimeoutError, asyncio.TimeoutError)):
        return ProviderTimeoutError(str(error) or "model provider timed out")
    if isinstance(error, (httpx.ConnectError, httpx.NetworkError, ConnectionError, OSError)):
        return ProviderConnectionError(str(error) or "model provider connection failed")
    if isinstance(error, (ValueError, TypeError)):
        return MalformedModelResponseError(str(error) or "model provider response is malformed")
    return ProviderInternalError(str(error) or type(error).__name__)


class ModelRouter:
    """Route one request through primary and, when safe, one fallback provider."""

    def __init__(self, primary: ModelProvider, fallback: ModelProvider | None = None) -> None:
        self.primary = primary
        self.fallback = fallback
        self._records: dict[str, ModelRequestRecord] = {}
        self._active: dict[str, tuple[ModelProvider, asyncio.Task[object] | None]] = {}

    async def health(self) -> ModelRuntimeSnapshot:
        primary_health = await self.primary.health()
        fallback_health = await self.fallback.health() if self.fallback is not None else None
        return ModelRuntimeSnapshot(
            gateway_state="online",
            primary=primary_health,
            fallback=fallback_health,
        )

    def lifecycle(self, request_id: str) -> ModelRequestRecord | None:
        record = self._records.get(request_id)
        return replace(record, attempts=list(record.attempts)) if record is not None else None

    async def generate(self, request: ModelRequest) -> ModelResponse:
        response: ModelResponse | None = None
        async for event in self.stream(request):
            if event.type == "final":
                response = event.response
        if response is None:
            raise ProviderInternalError("model stream completed without a final response")
        return response

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        self._validate_request(request)
        if request.request_id in self._records:
            raise InvalidModelRequestError(f"duplicate request_id: {request.request_id}")

        record = ModelRequestRecord(
            request_id=request.request_id,
            conversation_id=request.conversation_id,
            started_at=datetime.now(UTC),
            status=RequestStatus.RUNNING,
        )
        self._records[request.request_id] = record
        started = time.perf_counter()
        self._log(record, "request_started")
        providers = [self.primary]
        if self.fallback is not None:
            providers.append(self.fallback)
        chunks: list[str] = []
        sequence = 0

        for index, provider in enumerate(providers):
            record.provider_id = provider.provider_id
            record.model_id = provider.model_id
            record.fallback_used = index > 0
            attempt = ProviderAttempt(provider.provider_id, provider.model_id)
            record.attempts.append(attempt)
            emitted_by_provider = False
            final_seen = False
            task = asyncio.current_task()
            self._active[request.request_id] = (
                provider,
                cast(asyncio.Task[object] | None, task),
            )
            try:
                health = await provider.health()
                if not health.available:
                    raise ModelUnavailableError(
                        f"provider {provider.provider_id}/{provider.model_id} is {health.state.value}"
                    )
                async for provider_event in provider.stream(request):
                    if not isinstance(provider_event, ProviderStreamEvent):
                        raise MalformedModelResponseError(
                            f"provider emitted {type(provider_event).__name__}, expected ProviderStreamEvent"
                        )
                    if provider_event.final:
                        if final_seen:
                            continue
                        final_seen = True
                        continue
                    if final_seen:
                        raise MalformedModelResponseError(
                            "provider emitted content after its final event"
                        )
                    if not provider_event.delta:
                        continue
                    emitted_by_provider = True
                    chunks.append(provider_event.delta)
                    sequence += 1
                    yield ModelStreamEvent(
                        type="delta",
                        request_id=request.request_id,
                        sequence=sequence,
                        delta=provider_event.delta,
                    )
                if not final_seen:
                    raise ProviderConnectionError(
                        "provider stream ended before an authoritative final event"
                    )
            except asyncio.CancelledError:
                await provider.cancel(request.request_id)
                self._finish_failed(
                    record,
                    RequestStatus.CANCELLED,
                    ModelCancelledError.error_type,
                    started,
                )
                self._log(record, "request_cancelled")
                raise
            except Exception as provider_error:
                classified = classify_provider_error(provider_error)
                record.attempts[-1] = ProviderAttempt(
                    provider.provider_id,
                    provider.model_id,
                    classified.error_type,
                )
                has_fallback = index + 1 < len(providers)
                can_fallback = classified.retryable and not emitted_by_provider and has_fallback
                self._log(record, "provider_failed", classified.error_type)
                if can_fallback:
                    continue
                self._finish_failed(record, RequestStatus.FAILED, classified.error_type, started)
                raise classified from provider_error
            finally:
                self._active.pop(request.request_id, None)

            response = self._finish_success(record, "".join(chunks), started)
            sequence += 1
            self._log(record, "request_completed")
            yield ModelStreamEvent(
                type="final",
                request_id=request.request_id,
                sequence=sequence,
                response=response,
            )
            return

        final_error = ProviderInternalError("no model provider could serve the request")
        self._finish_failed(record, RequestStatus.FAILED, final_error.error_type, started)
        raise final_error

    async def cancel(self, request_id: str) -> bool:
        active = self._active.get(request_id)
        if active is None:
            return False
        provider, task = active
        await provider.cancel(request_id)
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
        return True

    @staticmethod
    def _validate_request(request: ModelRequest) -> None:
        if not request.request_id.strip():
            raise InvalidModelRequestError("request_id must not be empty")
        if not request.messages:
            raise InvalidModelRequestError("at least one prompt message is required")
        if any(not message.role.strip() for message in request.messages):
            raise InvalidModelRequestError("prompt message roles must not be empty")

    @staticmethod
    def _finish_success(
        record: ModelRequestRecord,
        content: str,
        started: float,
    ) -> ModelResponse:
        completed_at = datetime.now(UTC)
        latency_ms = max((time.perf_counter() - started) * 1000, 0.0)
        record.status = RequestStatus.COMPLETED
        record.completed_at = completed_at
        record.latency_ms = latency_ms
        if record.provider_id is None or record.model_id is None:
            raise ProviderInternalError("completed request has no provider metadata")
        return ModelResponse(
            content=content,
            request_id=record.request_id,
            conversation_id=record.conversation_id,
            provider_id=record.provider_id,
            model_id=record.model_id,
            started_at=record.started_at,
            completed_at=completed_at,
            latency_ms=latency_ms,
            status=RequestStatus.COMPLETED,
            fallback_used=record.fallback_used,
            attempts=tuple(record.attempts),
        )

    @staticmethod
    def _finish_failed(
        record: ModelRequestRecord,
        status: RequestStatus,
        error_type: str,
        started: float,
    ) -> None:
        record.status = status
        record.error_type = error_type
        record.completed_at = datetime.now(UTC)
        record.latency_ms = max((time.perf_counter() - started) * 1000, 0.0)

    @staticmethod
    def _log(
        record: ModelRequestRecord,
        event: str,
        error_type: str | None = None,
    ) -> None:
        logger.info(
            "model_runtime_event",
            extra={
                "component": "model_runtime",
                "event": event,
                "request_id": record.request_id,
                "conversation_id": record.conversation_id,
                "provider_id": record.provider_id,
                "model_id": record.model_id,
                "latency_ms": record.latency_ms,
                "error_type": error_type,
            },
        )


__all__ = [
    "FakeModelProvider",
    "InvalidModelRequestError",
    "MalformedModelResponseError",
    "ModelCancelledError",
    "ModelCapabilities",
    "ModelProvider",
    "ModelRequest",
    "ModelResponse",
    "ModelRouter",
    "ModelRuntimeError",
    "ModelRuntimeSnapshot",
    "ModelState",
    "ModelStreamEvent",
    "ModelUnavailableError",
    "OpenAICompatibleModelProvider",
    "ProviderConnectionError",
    "ProviderHealth",
    "ProviderInternalError",
    "ProviderStreamEvent",
    "ProviderTimeoutError",
    "RequestStatus",
    "StreamingLLMModelProvider",
    "classify_provider_error",
]
