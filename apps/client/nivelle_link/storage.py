import os
import secrets
import tempfile
from ipaddress import IPv4Address, ip_address
from pathlib import Path
from typing import Any

import keyring
import yaml
from nivelle_protocol.configuration import ResolvedSetting, resolve_endpoint, split_http_endpoint
from nivelle_protocol.local_migration import resolve_data_root
from nivelle_protocol.network import validate_advertised_host
from nivelle_protocol.settings import ConnectionProfile
from platformdirs import user_data_path

SERVICE = "NivelleLink"
LEGACY_SERVICE = "NozomiClient"
CONNECTIONS_FILE = "connections.yaml"


def validate_connection_host(value: str) -> str:
    """Validate an explicit Link-to-Core host without guessing another address.

    Link endpoints are destinations, so wildcard/listen-only addresses and
    non-routable IPv4 addresses must never be accepted as connection profiles.
    Host names and loopback addresses remain valid for same-PC installations.
    """

    try:
        host = validate_advertised_host(
            value,
            allow_loopback=True,
            allow_hostname=True,
        )
    except ValueError as exc:
        message = str(exc)
        if "wildcard" in message:
            raise ValueError(
                "a wildcard address cannot be used as a server destination"
            ) from None
        if "link_local" in message or "link-local" in message:
            raise ValueError(
                "an APIPA/link-local address cannot be used as a server destination"
            ) from None
        if "multicast" in message:
            raise ValueError(
                "a multicast address cannot be used as a server destination"
            ) from None
        if "URL" in message:
            raise ValueError("server host must not include a protocol or path") from None
        if "reserved_or_broadcast" in message:
            raise ValueError(
                "a broadcast address cannot be used as a server destination"
            ) from None
        raise ValueError("server host is invalid") from None

    try:
        address = ip_address(host)
    except ValueError:
        return host
    if isinstance(address, IPv4Address) and int(address) & 0xFF == 0xFF:
        raise ValueError("a broadcast address cannot be used as a server destination")
    return host


def is_loopback_connection_host(value: str) -> bool:
    """Return whether a Link destination refers to this same machine."""

    host = value.strip().strip("[]").lower()
    if host == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def client_data_dir() -> Path:
    """Return this client's private data directory.

    ``NIVELLE_LINK_DATA_DIR`` makes it possible to run multiple independent
    clients on one machine without sharing connection profiles. The old
    ``NOZOMI_CLIENT_DATA_DIR`` variable remains a warned compatibility alias.
    """

    return resolve_data_root(
        current_environment_variables=(
            "NIVELLE_LINK_DATA_DIR",
            "NIVELLE_CLIENT_DATA_DIR",
        ),
        legacy_environment_variable="NOZOMI_CLIENT_DATA_DIR",
        current_default=Path(user_data_path("NivelleLink", "Nivelle")),
        legacy_default=Path(user_data_path("NozomiClient", "Nozomi")),
        component="link",
    )


def connections_path() -> Path:
    return client_data_dir() / CONNECTIONS_FILE


def load_connection_profiles(path: Path | None = None) -> list[ConnectionProfile]:
    config_path = path or connections_path()
    if not config_path.exists():
        return []
    try:
        raw: Any = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            return []
        connections = raw.get("connections", [])
        if not isinstance(connections, list):
            return []
        profiles: list[ConnectionProfile] = []
        for item in connections:
            try:
                profile = ConnectionProfile.model_validate(item)
                validate_connection_host(profile.host)
            except (ValueError, TypeError):
                # Keep the file untouched. An unusable destination is simply
                # not eligible for automatic connection and can be replaced
                # through the connection dialog.
                continue
            profiles.append(profile)
        return profiles
    except (OSError, UnicodeError, yaml.YAMLError, ValueError, TypeError):
        # An invalid profile must not prevent the client from opening. The
        # connection dialog lets the user replace it with a valid profile.
        return []


def connection_profile_from_endpoint(endpoint: str) -> ConnectionProfile:
    parsed = split_http_endpoint(endpoint)
    if parsed.path not in {"", "/"}:
        raise ValueError("gateway_endpoint must not include a path")
    host = validate_connection_host(parsed.hostname or "")
    return ConnectionProfile(
        id="runtime-gateway",
        type="vpn",
        host=host,
        port=parsed.port or (443 if parsed.scheme == "https" else 80),
        tls=parsed.scheme == "https",
        priority=1,
        enabled=True,
    )


def resolve_connection_profiles(
    *,
    cli_endpoint: str | None = None,
    environment: dict[str, str] | None = None,
    path: Path | None = None,
) -> tuple[list[ConnectionProfile], ResolvedSetting[str] | None]:
    """Resolve the Link-owned Gateway endpoint without guessing a server."""
    env = environment if environment is not None else dict(os.environ)
    local_profiles = load_connection_profiles(path)
    if cli_endpoint or env.get("NIVELLE_GATEWAY_ENDPOINT"):
        resolved = resolve_endpoint(
            "gateway_endpoint",
            cli_value=cli_endpoint,
            environment=env,
            environment_name="NIVELLE_GATEWAY_ENDPOINT",
            local_value=None,
            safe_default=None,
            required=True,
        )
        return [connection_profile_from_endpoint(resolved.value)], resolved
    return local_profiles, None

def save_connection_profiles(
    profiles: list[ConnectionProfile], path: Path | None = None
) -> Path:
    """Atomically persist connection profiles and return the written path."""

    config_path = path or connections_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    payload = yaml.safe_dump(
        {"connections": [profile.model_dump(mode="json") for profile in profiles]},
        allow_unicode=True,
        sort_keys=False,
    )

    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=config_path.parent,
        prefix=f".{config_path.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, config_path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    return config_path


def token_key_for_profile(profile: ConnectionProfile) -> str:
    host = profile.host.strip().lower()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return f"{host}:{profile.port}"


def save_token(server_id: str, token: str) -> None:
    keyring.set_password(SERVICE, server_id, token)


def load_token(server_id: str) -> str | None:
    token = keyring.get_password(SERVICE, server_id)
    if token:
        return token

    # Credential migration is deliberately lazy: only the exact key required
    # for this connection is copied, and the old credential remains untouched.
    legacy_token = keyring.get_password(LEGACY_SERVICE, server_id)
    if not legacy_token:
        return None
    keyring.set_password(SERVICE, server_id, legacy_token)
    verified_token = keyring.get_password(SERVICE, server_id)
    if not verified_token or not secrets.compare_digest(verified_token, legacy_token):
        raise RuntimeError(
            f"credential migration verification failed for key {server_id!r}"
        )
    return verified_token


def save_token_for_profile(profile: ConnectionProfile, token: str) -> None:
    save_token(token_key_for_profile(profile), token)


def load_token_for_profile(profile: ConnectionProfile) -> str | None:
    key = token_key_for_profile(profile)
    token = load_token(key)
    if token:
        return token

    host = profile.host.strip().lower().strip("[]")
    if host not in {"127.0.0.1", "localhost", "::1"}:
        return None

    # Phase 1 stored the local token under one global key. Read it once and
    # migrate a copy so existing localhost installations keep working.
    legacy_token = load_token("default")
    if legacy_token:
        save_token(key, legacy_token)
    return legacy_token


def delete_token(server_id: str) -> None:
    try:
        keyring.delete_password(SERVICE, server_id)
    except keyring.errors.PasswordDeleteError:
        return
