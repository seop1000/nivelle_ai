"""Shared, source-aware runtime configuration resolution.

Runtime components use this module instead of independently interpreting CLI,
environment, and local configuration values.  Values are resolved in the P0
order: CLI -> environment -> local config -> safe default.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import SplitResult, urlsplit


class ConfigurationError(ValueError):
    """Raised when a configured value is missing or unsafe."""


class SettingSource(StrEnum):
    CLI = "cli"
    ENVIRONMENT = "environment"
    LOCAL_CONFIG = "local_config"
    SAFE_DEFAULT = "safe_default"


@dataclass(frozen=True)
class ResolvedSetting[ValueT]:
    name: str
    value: ValueT
    source: SettingSource

    def diagnostic(self, *, reveal_value: bool = False) -> str:
        rendered = str(self.value) if reveal_value else "<redacted>"
        return f"{self.name}: source={self.source.value}, value={rendered}"


def _present(value: object | None) -> bool:
    return value is not None and (not isinstance(value, str) or bool(value.strip()))


def resolve_setting[ValueT](
    name: str,
    *,
    cli_value: object | None,
    environment: Mapping[str, str],
    environment_name: str,
    local_value: object | None,
    safe_default: object | None,
    parser: Callable[[object], ValueT],
    required: bool = False,
) -> ResolvedSetting[ValueT]:
    """Resolve one setting with an explicit source and a single parser."""

    candidates = (
        (SettingSource.CLI, cli_value),
        (SettingSource.ENVIRONMENT, environment.get(environment_name)),
        (SettingSource.LOCAL_CONFIG, local_value),
        (SettingSource.SAFE_DEFAULT, safe_default),
    )
    for source, candidate in candidates:
        if not _present(candidate):
            continue
        try:
            return ResolvedSetting(name, parser(candidate), source)
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(
                f"{name} from {source.value} is invalid: {exc}"
            ) from exc
    if required:
        raise ConfigurationError(
            f"{name} is required; set it by CLI, {environment_name}, or local config"
        )
    raise ConfigurationError(f"{name} has no configured or safe default value")


def parse_http_endpoint(value: object) -> str:
    """Return a normalized HTTP(S) endpoint without credentials or ambiguity."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError("endpoint must be a non-empty string")
    normalized = value.strip().rstrip("/")
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("endpoint must use http or https and include a host")
    if parsed.username or parsed.password:
        raise ValueError("endpoint must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("endpoint must not contain a query or fragment")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("endpoint port is invalid") from exc
    return normalized


def split_http_endpoint(value: str) -> SplitResult:
    normalized = parse_http_endpoint(value)
    return urlsplit(normalized)


def resolve_endpoint(
    name: str,
    *,
    cli_value: str | None,
    environment: Mapping[str, str],
    environment_name: str,
    local_value: str | None,
    safe_default: str | None,
    required: bool = False,
) -> ResolvedSetting[str]:
    return resolve_setting(
        name,
        cli_value=cli_value,
        environment=environment,
        environment_name=environment_name,
        local_value=local_value,
        safe_default=safe_default,
        parser=parse_http_endpoint,
        required=required,
    )


__all__ = [
    "ConfigurationError",
    "ResolvedSetting",
    "SettingSource",
    "parse_http_endpoint",
    "resolve_endpoint",
    "resolve_setting",
    "split_http_endpoint",
]
