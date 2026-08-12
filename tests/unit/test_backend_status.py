import httpx
from nivelle_core.backend_status import probe_openai_backend


async def test_backend_probe_reports_ready() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == "http://llama.local/health":
            return httpx.Response(200, json={"status": "ok"})
        assert request.url == "http://llama.local/v1/models"
        return httpx.Response(200, json={"data": [{"id": "Qwen3.5-9B-Q4_K_M"}]})

    result = await probe_openai_backend(
        "http://llama.local/", transport=httpx.MockTransport(handler)
    )

    assert result["state"] == "ready"
    assert result["available"] is True
    assert result["details"] == {"status": "ok"}
    assert result["loaded_model"] == "Qwen3.5-9B-Q4_K_M"


async def test_backend_probe_does_not_guess_model_when_listing_is_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(404)

    result = await probe_openai_backend(
        "http://llama.local", transport=httpx.MockTransport(handler)
    )

    assert result["state"] == "ready"
    assert result["loaded_model"] is None
    assert result["models_error"] == "HTTPStatusError"


async def test_backend_probe_reports_http_failure() -> None:
    result = await probe_openai_backend(
        "http://llama.local",
        transport=httpx.MockTransport(lambda _: httpx.Response(503)),
    )

    assert result["state"] == "error"
    assert result["available"] is False
    assert result["status_code"] == 503
