from nivelle_protocol.server_status import GenerationMetrics, ServerStatus
from nivelle_protocol.version import APP_VERSION, PROTOCOL_VERSION, runtime_identity


def test_generation_metrics_accepts_legacy_names_but_serializes_wire_names() -> None:
    metrics = GenerationMetrics(input_tokens=8, output_tokens=3, total_tokens=11)

    assert metrics.input_tokens == 8
    assert metrics.output_tokens == 3
    assert metrics.model_dump() == {
        "prompt_tokens": 8,
        "completion_tokens": 3,
        "total_tokens": 11,
        "tokens_per_second": None,
        "first_token_latency_ms": None,
        "total_latency_ms": None,
        "finish_reason": None,
        "interrupted": False,
        "model": None,
        "request_id": None,
    }


def test_server_status_has_separate_runtime_backend_memory_and_embedding_state() -> None:
    status = ServerStatus(
        pairing_required=False,
        uptime_seconds=12.5,
        runtime=runtime_identity(
            "nivelle-core",
            environ={},
            executable="Nivelle-Core.exe",
            frozen=True,
        ),
        llama_server={"state": "ready", "available": True},
        memory_database={"state": "ready", "backend": "sqlite_hybrid", "active_count": 10},
        embedding_model={"state": "unavailable", "provider": None, "reason": "not_configured"},
        last_request_metrics={"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
    )

    assert status.app_version == APP_VERSION
    assert status.protocol_version == PROTOCOL_VERSION
    assert status.runtime is not None
    assert status.runtime.component == "nivelle-core"
    assert status.llama_server is not None and status.llama_server.state == "ready"
    assert status.memory_database is not None
    assert status.memory_database.backend == "sqlite_hybrid"
    assert status.embedding_model is not None
    assert status.embedding_model.state == "unavailable"
    assert status.last_request_metrics is not None
    assert status.last_request_metrics.total_tokens == 6
