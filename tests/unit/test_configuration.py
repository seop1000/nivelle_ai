from __future__ import annotations

import pytest
from nivelle_protocol.configuration import (
    ConfigurationError,
    SettingSource,
    parse_http_endpoint,
    resolve_endpoint,
)


@pytest.mark.parametrize(
    ("cli", "environment", "local", "default", "expected", "source"),
    [
        (
            "https://cli.example:9443",
            {"NIVELLE_GATEWAY_ENDPOINT": "https://env.example"},
            "https://local.example",
            "https://default.example",
            "https://cli.example:9443",
            SettingSource.CLI,
        ),
        (
            None,
            {"NIVELLE_GATEWAY_ENDPOINT": "https://env.example"},
            "https://local.example",
            "https://default.example",
            "https://env.example",
            SettingSource.ENVIRONMENT,
        ),
        (
            None,
            {},
            "https://local.example/",
            "https://default.example",
            "https://local.example",
            SettingSource.LOCAL_CONFIG,
        ),
        (
            None,
            {},
            None,
            "http://127.0.0.1:8765",
            "http://127.0.0.1:8765",
            SettingSource.SAFE_DEFAULT,
        ),
    ],
)
def test_endpoint_priority(
    cli: str | None,
    environment: dict[str, str],
    local: str | None,
    default: str | None,
    expected: str,
    source: SettingSource,
) -> None:
    resolved = resolve_endpoint(
        "gateway_endpoint",
        cli_value=cli,
        environment=environment,
        environment_name="NIVELLE_GATEWAY_ENDPOINT",
        local_value=local,
        safe_default=default,
    )

    assert resolved.value == expected
    assert resolved.source is source
    assert expected not in resolved.diagnostic()


@pytest.mark.parametrize(
    "value",
    [
        "",
        "localhost:8765",
        "ftp://example.test/model",
        "http://user:password@example.test",
        "http://example.test/path?token=secret",
        "http://example.test/#fragment",
        "http://example.test:bad",
    ],
)
def test_endpoint_validation_rejects_ambiguous_or_secret_values(value: str) -> None:
    with pytest.raises(ValueError):
        parse_http_endpoint(value)


def test_required_endpoint_never_guesses() -> None:
    with pytest.raises(ConfigurationError, match="gateway_endpoint is required"):
        resolve_endpoint(
            "gateway_endpoint",
            cli_value=None,
            environment={},
            environment_name="NIVELLE_GATEWAY_ENDPOINT",
            local_value=None,
            safe_default=None,
            required=True,
        )
