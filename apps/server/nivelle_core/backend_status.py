from typing import Any

import httpx


async def probe_openai_backend(
    base_url: str,
    *,
    request_timeout: float = 1.5,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Return a small, truthful health snapshot for the configured LLM backend."""
    health_url = f"{base_url.rstrip('/')}/health"
    try:
        loaded_model: str | None = None
        models_error: str | None = None
        async with httpx.AsyncClient(timeout=request_timeout, transport=transport) as client:
            response = await client.get(health_url)
            if response.is_success:
                try:
                    models_response = await client.get(
                        f"{base_url.rstrip('/')}/v1/models"
                    )
                    models_response.raise_for_status()
                    models_payload = models_response.json()
                    model_items = (
                        models_payload.get("data")
                        if isinstance(models_payload, dict)
                        else None
                    )
                    if isinstance(model_items, list) and model_items:
                        first_model = model_items[0]
                        model_id = (
                            first_model.get("id")
                            if isinstance(first_model, dict)
                            else None
                        )
                        if isinstance(model_id, str) and model_id.strip():
                            loaded_model = model_id.strip()
                except (httpx.HTTPError, OSError, ValueError) as exc:
                    # Health and model identity are separate facts.  A backend
                    # can remain ready while its loaded model is unconfirmed.
                    models_error = type(exc).__name__
        available = response.is_success
        details: object | None = None
        if response.headers.get("content-type", "").startswith("application/json"):
            try:
                details = response.json()
            except ValueError:
                details = None
        return {
            "state": "ready" if available else "error",
            "reachable": True,
            "available": available,
            "url": base_url.rstrip("/"),
            "status_code": response.status_code,
            "details": details,
            "loaded_model": loaded_model,
            "models_error": models_error,
            "error": None if available else f"HTTP {response.status_code}",
        }
    except (httpx.HTTPError, OSError) as exc:
        return {
            "state": "unreachable",
            "reachable": False,
            "available": False,
            "url": base_url.rstrip("/"),
            "status_code": None,
            "details": None,
            "loaded_model": None,
            "models_error": None,
            "error": str(exc),
        }


__all__ = ["probe_openai_backend"]
