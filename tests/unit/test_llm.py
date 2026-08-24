import json
import logging
from collections.abc import AsyncIterator, Sequence

import httpx
import pytest
from nivelle_core.llm import (
    LlamaCppServerProvider,
    MockLLMProvider,
    PromptMessage,
    generation_metrics_from_payload,
    provider_stream,
)
from nivelle_protocol.server_status import GenerationMetrics
from nivelle_protocol.settings import InferenceSettings
from nivelle_protocol.tools import TOOL_REGISTRY
from pydantic import ValidationError


async def test_mock_streams_user_message() -> None:
    text = "".join(
        [chunk async for chunk in MockLLMProvider().stream([PromptMessage("user", "안녕")])]
    )
    assert "안녕" in text


def test_llama_request_uses_saved_inference_settings() -> None:
    settings = InferenceSettings(
        temperature=0.25,
        top_p=0.75,
        top_k=12,
        repeat_penalty=1.05,
        max_output_tokens=321,
        seed=42,
        streaming=False,
    )
    provider = LlamaCppServerProvider("http://127.0.0.1:8080/", settings)

    payload = provider.request_payload([PromptMessage("user", "hello")])

    assert payload == {
        "messages": [{"role": "user", "content": "hello"}],
        "stream": False,
        "temperature": 0.25,
        "top_p": 0.75,
        "top_k": 12,
        "repeat_penalty": 1.05,
        "max_tokens": 321,
        "seed": 42,
    }


def test_streaming_request_asks_backend_for_real_usage() -> None:
    settings = InferenceSettings(streaming=True)
    provider = LlamaCppServerProvider("http://127.0.0.1:8080", settings)

    payload = provider.request_payload([PromptMessage("user", "hello")])

    assert payload["stream_options"] == {"include_usage": True}


def test_llama_request_logs_role_sequence_without_message_content(caplog) -> None:
    provider = LlamaCppServerProvider(
        "http://127.0.0.1:8080", InferenceSettings(streaming=False)
    )

    with caplog.at_level(logging.DEBUG, logger="nivelle_core.llm"):
        provider.request_payload(
            [
                PromptMessage("system", "secret-system-content"),
                PromptMessage("user", "secret-user-content"),
            ]
        )
        provider.request_payload(
            [
                PromptMessage("system", "other-secret-system-content"),
                PromptMessage("user", "first-secret-user-content"),
                PromptMessage("user", "second-secret-user-content"),
            ]
        )

    assert "roles=system -> user" in caplog.text
    assert "message_count=2" in caplog.text
    assert "alternation_valid=True" in caplog.text
    assert "roles=system -> user -> user" in caplog.text
    assert "alternation_valid=False" in caplog.text
    assert "secret-system-content" not in caplog.text
    assert "secret-user-content" not in caplog.text
    assert "other-secret-system-content" not in caplog.text
    assert "first-secret-user-content" not in caplog.text
    assert "second-secret-user-content" not in caplog.text


def test_llama_metrics_parse_usage_and_native_timings_without_estimating() -> None:
    metrics = generation_metrics_from_payload(
        {
            "model": "qwen3.5-9b-q4_k_m",
            "usage": {
                "prompt_tokens": 21,
                "completion_tokens": 9,
                "total_tokens": 30,
            },
            "timings": {"predicted_per_second": 17.25},
            "choices": [{"finish_reason": "stop"}],
        }
    )

    assert metrics.prompt_tokens == 21
    assert metrics.completion_tokens == 9
    assert metrics.total_tokens == 30
    assert metrics.tokens_per_second == 17.25
    assert metrics.finish_reason == "stop"
    assert metrics.model == "qwen3.5-9b-q4_k_m"


def test_missing_backend_usage_remains_null() -> None:
    metrics = generation_metrics_from_payload({"choices": [{"finish_reason": "length"}]})

    assert metrics.prompt_tokens is None
    assert metrics.completion_tokens is None
    assert metrics.total_tokens is None
    assert metrics.tokens_per_second is None
    assert metrics.finish_reason == "length"


async def test_stream_captures_usage_only_terminal_chunk_and_measured_latency() -> None:
    body = b"".join(
        [
            b'data: {"model":"qwen","choices":[{"delta":{"content":"hello"},',
            b'"finish_reason":null}]}\n\n',
            b'data: {"model":"qwen","choices":[],"usage":{"prompt_tokens":4,',
            b'"completion_tokens":2,"total_tokens":6},',
            b'"timings":{"predicted_per_second":12.5}}\n\n',
            b"data: [DONE]\n\n",
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "http://llama.local/v1/chat/completions"
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    provider = LlamaCppServerProvider(
        "http://llama.local",
        InferenceSettings(streaming=True),
        transport=httpx.MockTransport(handler),
    )
    captured: list[GenerationMetrics] = []

    text = "".join(
        [
            chunk
            async for chunk in provider_stream(
                provider,
                [PromptMessage("user", "hi")],
                on_metrics=captured.append,
            )
        ]
    )

    assert text == "hello"
    assert len(captured) == 1
    metrics = captured[0]
    assert metrics.prompt_tokens == 4
    assert metrics.completion_tokens == 2
    assert metrics.total_tokens == 6
    assert metrics.tokens_per_second == 12.5
    assert metrics.first_token_latency_ms is not None
    assert metrics.total_latency_ms is not None
    assert metrics.total_latency_ms >= metrics.first_token_latency_ms


async def test_provider_stream_keeps_legacy_custom_provider_compatible() -> None:
    class LegacyProvider:
        async def stream(self, messages: Sequence[PromptMessage]) -> AsyncIterator[str]:
            del messages
            yield "legacy"

    captured: list[GenerationMetrics] = []
    text = "".join(
        [
            chunk
            async for chunk in provider_stream(
                LegacyProvider(),
                [PromptMessage("user", "hi")],
                on_metrics=captured.append,
            )
        ]
    )

    assert text == "legacy"
    assert captured == []


async def test_native_tool_planning_returns_only_structured_function_calls() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["stream"] is False
        assert payload["tool_choice"] == "auto"
        assert payload["tools"][0]["function"]["name"] == "get_system_status"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "get_system_status",
                                        "arguments": "{}",
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
        )

    provider = LlamaCppServerProvider(
        "http://llama.local",
        InferenceSettings(streaming=True),
        transport=httpx.MockTransport(handler),
    )
    proposals = await provider.plan_tools(
        [PromptMessage("user", "PC 상태를 알려줘")],
        [
            {
                "type": "function",
                "function": {
                    "name": "get_system_status",
                    "description": "safe status",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        max_calls=3,
    )

    assert len(proposals) == 1
    assert proposals[0].tool_call_id == "call-1"
    assert proposals[0].name == "get_system_status"
    assert proposals[0].arguments == {}


async def test_native_tool_planning_relaxes_only_grammar_unsafe_string_bounds() -> None:
    definition = TOOL_REGISTRY.get("create_note")
    advertised = [item.model_tool_definition() for item in TOOL_REGISTRY.definitions]
    advertised.append(
        {
            "type": "function",
            "function": {
                "name": "repetition_boundary",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "safe": {"type": "string", "maxLength": 1_999},
                        "at_limit": {"type": "string", "maxLength": 2_000},
                    },
                },
            },
        }
    )
    original_parameters = definition.arguments_json_schema()
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"tool_calls": []}}]})

    provider = LlamaCppServerProvider(
        "http://llama.local",
        InferenceSettings(),
        transport=httpx.MockTransport(handler),
    )

    assert await provider.plan_tools(
        [PromptMessage("user", "긴 메모를 만들어줘")],
        advertised,
        max_calls=1,
    ) == []

    sent_functions = {
        item["function"]["name"]: item["function"] for item in captured["tools"]
    }
    create_note = sent_functions["create_note"]["parameters"]["properties"]
    open_folder = sent_functions["open_folder"]["parameters"]["properties"]
    set_reminder = sent_functions["set_reminder"]["parameters"]["properties"]
    boundary = sent_functions["repetition_boundary"]["parameters"]["properties"]
    assert create_note["title"]["maxLength"] == 200
    assert "maxLength" not in create_note["content"]
    assert "maxLength" not in open_folder["path"]
    assert "maxLength" not in set_reminder["reminder_text"]
    assert boundary["safe"]["maxLength"] == 1_999
    assert "maxLength" not in boundary["at_limit"]
    assert original_parameters["properties"]["content"]["maxLength"] == 100_000
    definition.argument_schema.model_validate(
        {"title": "긴 메모", "content": "가" * 100_000, "format": "txt"}
    )
    with pytest.raises(ValidationError, match="string_too_long"):
        definition.argument_schema.model_validate(
            {"title": "긴 메모", "content": "가" * 100_001, "format": "txt"}
        )


async def test_native_tool_planning_rejects_malformed_arguments_without_execution() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "call-bad",
                                    "type": "function",
                                    "function": {
                                        "name": "open_application",
                                        "arguments": "not-json",
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
        )

    provider = LlamaCppServerProvider(
        "http://llama.local",
        InferenceSettings(),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(json.JSONDecodeError):
        await provider.plan_tools(
            [PromptMessage("user", "열어줘")],
            [{"type": "function", "function": {"name": "open_application"}}],
            max_calls=1,
        )
