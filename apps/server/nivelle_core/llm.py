from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast

import httpx
from nivelle_protocol.server_status import GenerationMetrics
from nivelle_protocol.settings import InferenceSettings

_LOGGER = logging.getLogger(__name__)
# b10231 caps direct counts at 2,000 and rejects expanded rule_count * repetition >= 2,000.
_LLAMA_CPP_MAX_REPETITION = 2_000


@dataclass(frozen=True)
class PromptMessage:
    role: str
    content: str


@dataclass(frozen=True)
class LLMToolProposal:
    tool_call_id: str
    name: str
    arguments: dict[str, Any]


MetricsCallback = Callable[[GenerationMetrics], None]


class LLMProvider(Protocol):
    def stream(self, messages: Sequence[PromptMessage]) -> AsyncIterator[str]: ...


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _nonnegative_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if result >= 0 else None


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _grammar_safe_json_schema(value: object) -> object:
    """Relax only string bounds that b10231 cannot encode as GBNF repetitions."""

    if isinstance(value, Mapping):
        result = {
            str(key): _grammar_safe_json_schema(item) for key, item in value.items()
        }
        for keyword in ("minLength", "maxLength"):
            bound = result.get(keyword)
            if (
                isinstance(bound, int)
                and not isinstance(bound, bool)
                and bound >= _LLAMA_CPP_MAX_REPETITION
            ):
                result.pop(keyword)
        return result
    if isinstance(value, list):
        return [_grammar_safe_json_schema(item) for item in value]
    return value


def _grammar_safe_tool_definition(tool: Mapping[str, object]) -> dict[str, object]:
    result = dict(tool)
    function = tool.get("function")
    if not isinstance(function, Mapping):
        return result
    safe_function = dict(function)
    parameters = function.get("parameters")
    if isinstance(parameters, Mapping):
        safe_function["parameters"] = _grammar_safe_json_schema(parameters)
    result["function"] = safe_function
    return result


def _log_message_roles(messages: Sequence[PromptMessage]) -> None:
    roles = [message.role for message in messages]
    conversational = roles[1:] if roles[:1] == ["system"] else roles
    expected = "user"
    valid = bool(conversational)
    for role in conversational:
        if role != expected:
            valid = False
            break
        expected = "assistant" if role == "user" else "user"
    log = _LOGGER.debug if valid else _LOGGER.warning
    log(
        "llama.cpp request roles=%s message_count=%d alternation_valid=%s",
        " -> ".join(roles),
        len(roles),
        valid,
    )


def generation_metrics_from_payload(payload: Mapping[str, Any]) -> GenerationMetrics:
    """Read only metrics actually supplied by an OpenAI/llama.cpp response."""

    usage = _mapping(payload.get("usage"))
    timings = _mapping(payload.get("timings"))
    prompt_tokens = _nonnegative_int(
        usage.get("prompt_tokens", usage.get("input_tokens"))
    )
    if prompt_tokens is None:
        prompt_tokens = _nonnegative_int(timings.get("prompt_n"))
    completion_tokens = _nonnegative_int(
        usage.get("completion_tokens", usage.get("output_tokens"))
    )
    if completion_tokens is None:
        completion_tokens = _nonnegative_int(timings.get("predicted_n"))
    total_tokens = _nonnegative_int(usage.get("total_tokens"))
    if total_tokens is None and prompt_tokens is not None and completion_tokens is not None:
        total_tokens = prompt_tokens + completion_tokens

    tokens_per_second = _nonnegative_float(timings.get("predicted_per_second"))
    if tokens_per_second is None:
        tokens_per_second = _nonnegative_float(payload.get("tokens_per_second"))

    finish_reason: str | None = None
    choices = payload.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], Mapping):
        reason = choices[0].get("finish_reason")
        if isinstance(reason, str) and reason:
            finish_reason = reason
    model_value = payload.get("model")
    model = model_value if isinstance(model_value, str) and model_value else None

    return GenerationMetrics(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        tokens_per_second=tokens_per_second,
        finish_reason=finish_reason,
        model=model,
    )


def _merge_metrics(
    current: GenerationMetrics | None, update: GenerationMetrics
) -> GenerationMetrics:
    if current is None:
        return update
    values: dict[str, object] = {}
    for field in GenerationMetrics.model_fields:
        new_value = getattr(update, field)
        old_value = getattr(current, field)
        values[field] = new_value if new_value is not None else old_value
    return GenerationMetrics.model_validate(values)


def _finish_metrics(
    reported: GenerationMetrics | None,
    *,
    started: float,
    first_token_at: float | None,
    finished: float,
) -> GenerationMetrics:
    base = reported or GenerationMetrics()
    first_latency_ms = (
        max((first_token_at - started) * 1000, 0.0)
        if first_token_at is not None
        else None
    )
    total_latency_ms = max((finished - started) * 1000, 0.0)
    tokens_per_second = base.tokens_per_second
    if (
        tokens_per_second is None
        and base.completion_tokens is not None
        and first_latency_ms is not None
    ):
        generation_seconds = (total_latency_ms - first_latency_ms) / 1000
        if generation_seconds > 0:
            tokens_per_second = base.completion_tokens / generation_seconds
    return base.model_copy(
        update={
            "tokens_per_second": tokens_per_second,
            "first_token_latency_ms": first_latency_ms,
            "total_latency_ms": total_latency_ms,
            "interrupted": False,
        }
    )


class MockLLMProvider:
    async def plan_tools(
        self,
        messages: Sequence[PromptMessage],
        tools: Sequence[Mapping[str, object]],
        *,
        max_calls: int,
    ) -> list[LLMToolProposal]:
        del messages, tools, max_calls
        return []

    async def stream(self, messages: Sequence[PromptMessage]) -> AsyncIterator[str]:
        async for chunk in self.stream_with_metrics(messages):
            yield chunk

    async def stream_with_metrics(
        self,
        messages: Sequence[PromptMessage],
        *,
        on_metrics: MetricsCallback | None = None,
    ) -> AsyncIterator[str]:
        started = time.perf_counter()
        first_token_at: float | None = None
        user = next((item.content for item in reversed(messages) if item.role == "user"), "")
        response = f"Mock LLM 응답입니다. 입력하신 내용: {user}"
        for word in response.split(" "):
            await asyncio.sleep(0)
            if first_token_at is None:
                first_token_at = time.perf_counter()
            yield word + " "
        if on_metrics is not None:
            on_metrics(
                _finish_metrics(
                    None,
                    started=started,
                    first_token_at=first_token_at,
                    finished=time.perf_counter(),
                )
            )


class LlamaCppServerProvider:
    def __init__(
        self,
        base_url: str,
        inference: InferenceSettings,
        *,
        model_id: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.inference = inference
        self.model_id = model_id
        self.transport = transport

    def request_payload(self, messages: Sequence[PromptMessage]) -> dict[str, object]:
        """Build the OpenAI-compatible request from the current saved settings."""

        _log_message_roles(messages)
        payload: dict[str, object] = {
            "messages": [item.__dict__ for item in messages],
            "stream": self.inference.streaming,
            "temperature": self.inference.temperature,
            "top_p": self.inference.top_p,
            "top_k": self.inference.top_k,
            "repeat_penalty": self.inference.repeat_penalty,
            "max_tokens": self.inference.max_output_tokens,
            "seed": self.inference.seed,
        }
        if self.inference.streaming:
            payload["stream_options"] = {"include_usage": True}
        if self.model_id is not None:
            payload["model"] = self.model_id
        return payload

    async def stream(self, messages: Sequence[PromptMessage]) -> AsyncIterator[str]:
        async for chunk in self.stream_with_metrics(messages):
            yield chunk

    async def plan_tools(
        self,
        messages: Sequence[PromptMessage],
        tools: Sequence[Mapping[str, object]],
        *,
        max_calls: int,
    ) -> list[LLMToolProposal]:
        """Request native OpenAI-compatible tool proposals without executing them."""

        if max_calls < 1 or not tools:
            return []
        payload = self.request_payload(messages)
        payload["stream"] = False
        payload.pop("stream_options", None)
        payload["tools"] = [_grammar_safe_tool_definition(tool) for tool in tools]
        payload["tool_choice"] = "auto"
        async with httpx.AsyncClient(
            timeout=self.inference.request_timeout,
            transport=self.transport,
        ) as client:
            response = await client.post(
                f"{self.base_url}/v1/chat/completions",
                json=payload,
            )
            response.raise_for_status()
            body = response.json()
        if not isinstance(body, Mapping):
            raise ValueError("llama.cpp returned a non-object tool-planning response")
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ValueError("llama.cpp tool-planning response has no choices")
        first_choice = _mapping(choices[0])
        message = _mapping(first_choice.get("message"))
        raw_calls = message.get("tool_calls")
        if raw_calls in (None, []):
            return []
        if not isinstance(raw_calls, list):
            raise ValueError("llama.cpp tool_calls must be an array")
        if len(raw_calls) > max_calls:
            raise ValueError("llama.cpp proposed too many tool calls")
        proposals: list[LLMToolProposal] = []
        for raw_call in raw_calls:
            call = _mapping(raw_call)
            if call.get("type") != "function":
                raise ValueError("only function tool calls are supported")
            call_id = call.get("id")
            function = _mapping(call.get("function"))
            name = function.get("name")
            raw_arguments = function.get("arguments")
            if not isinstance(call_id, str) or not call_id:
                raise ValueError("tool call has no id")
            if not isinstance(name, str) or not name:
                raise ValueError("tool call has no function name")
            if not isinstance(raw_arguments, str):
                raise ValueError("tool call arguments must be a JSON string")
            arguments = json.loads(raw_arguments)
            if not isinstance(arguments, dict):
                raise ValueError("tool call arguments must decode to an object")
            proposals.append(LLMToolProposal(call_id, name, arguments))
        return proposals

    async def stream_with_metrics(
        self,
        messages: Sequence[PromptMessage],
        *,
        on_metrics: MetricsCallback | None = None,
    ) -> AsyncIterator[str]:
        payload = self.request_payload(messages)
        started = time.perf_counter()
        first_token_at: float | None = None
        reported_metrics: GenerationMetrics | None = None
        async with httpx.AsyncClient(
            timeout=self.inference.request_timeout, transport=self.transport
        ) as client:
            if not self.inference.streaming:
                response = await client.post(
                    f"{self.base_url}/v1/chat/completions", json=payload
                )
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, Mapping):
                    raise ValueError("llama.cpp returned a non-object completion response")
                reported_metrics = generation_metrics_from_payload(data)
                choices = data.get("choices")
                if not isinstance(choices, list) or not choices:
                    raise ValueError("llama.cpp completion response has no choices")
                first_choice = _mapping(choices[0])
                message = _mapping(first_choice.get("message"))
                content = message.get("content")
                if content:
                    first_token_at = time.perf_counter()
                    yield str(content)
                if on_metrics is not None:
                    on_metrics(
                        _finish_metrics(
                            reported_metrics,
                            started=started,
                            first_token_at=first_token_at,
                            finished=time.perf_counter(),
                        )
                    )
                return

            async with client.stream(
                "POST", f"{self.base_url}/v1/chat/completions", json=payload
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data: ") or line == "data: [DONE]":
                        continue
                    data = json.loads(line[6:])
                    if not isinstance(data, Mapping):
                        raise ValueError("llama.cpp returned a non-object stream event")
                    reported_metrics = _merge_metrics(
                        reported_metrics, generation_metrics_from_payload(data)
                    )
                    choices = data.get("choices")
                    if not isinstance(choices, list) or not choices:
                        # OpenAI-compatible usage-only terminal chunks have no choice.
                        continue
                    first_choice = _mapping(choices[0])
                    delta = _mapping(first_choice.get("delta")).get("content")
                    if delta:
                        if first_token_at is None:
                            first_token_at = time.perf_counter()
                        yield str(delta)
        if on_metrics is not None:
            on_metrics(
                _finish_metrics(
                    reported_metrics,
                    started=started,
                    first_token_at=first_token_at,
                    finished=time.perf_counter(),
                )
            )


def provider_stream(
    provider: LLMProvider,
    messages: Sequence[PromptMessage],
    *,
    on_metrics: MetricsCallback | None = None,
) -> AsyncIterator[str]:
    """Use metrics when supported while retaining simple test/custom providers."""

    monitored = getattr(provider, "stream_with_metrics", None)
    if callable(monitored):
        return cast(AsyncIterator[str], monitored(messages, on_metrics=on_metrics))
    return provider.stream(messages)
