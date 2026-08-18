"""Shared application, protocol, and runtime build identity helpers."""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Mapping
from importlib import metadata
from pathlib import Path
from typing import Literal, TextIO

from pydantic import BaseModel

_APP_VERSION_PATTERN = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,63}$")
_DEVELOPMENT_VERSION_PATTERN = re.compile(r"(?:^|-)dev\.g(?P<commit>[0-9a-fA-F]{12})(?:$|[.+-])")


def _load_app_version() -> str:
    """Load the application version from the canonical ``VERSION`` artifact."""

    source_path = Path(__file__).resolve()
    candidates = [source_path.with_name("VERSION")]
    if len(source_path.parents) >= 3:
        candidates.append(source_path.parents[2] / "VERSION")
    for candidate in candidates:
        try:
            value = candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if _APP_VERSION_PATTERN.fullmatch(value):
            return value
        raise RuntimeError(f"Invalid Nivelle VERSION value in {candidate}")
    installed_value: str | None = None
    for distribution in ("nivelle-ai", "nozomi-ai"):
        try:
            installed_value = metadata.version(distribution)
            break
        except metadata.PackageNotFoundError:
            continue
    if installed_value is None:
        raise RuntimeError("Nivelle application version source was not found")
    if not _APP_VERSION_PATTERN.fullmatch(installed_value):
        raise RuntimeError("Installed Nivelle package has an invalid version")
    return installed_value


APP_VERSION = _load_app_version()
PROTOCOL_VERSION = "1.0"

_PROTOCOL_PATTERN = re.compile(r"^(?P<major>0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){0,2}$")


class RuntimeIdentity(BaseModel):
    """Non-secret identity of the currently running Nivelle component."""

    component: str
    app_version: str = APP_VERSION
    protocol_version: str = PROTOCOL_VERSION
    build_commit: str | None = None
    build_time: str | None = None
    executable_path: str
    frozen: bool = False


class ProtocolCompatibility(BaseModel):
    """Result of comparing two protocol versions by compatibility boundary."""

    compatible: bool
    local_version: str
    remote_version: str
    status: Literal["compatible", "version_warning", "major_mismatch", "invalid"]
    warning: str | None = None


def protocol_major(version: str) -> int:
    """Return a validated protocol major version."""

    match = _PROTOCOL_PATTERN.fullmatch(version.strip())
    if match is None:
        raise ValueError(f"Invalid protocol version: {version!r}")
    return int(match.group("major"))


def protocol_compatibility(
    remote_version: str, *, local_version: str = PROTOCOL_VERSION
) -> ProtocolCompatibility:
    """Treat equal major versions as compatible and explain lesser differences."""

    try:
        local_major = protocol_major(local_version)
        remote_major = protocol_major(remote_version)
    except (AttributeError, ValueError):
        return ProtocolCompatibility(
            compatible=False,
            local_version=local_version,
            remote_version=str(remote_version),
            status="invalid",
            warning="상대 프로토콜 버전 형식을 확인할 수 없습니다.",
        )
    if local_major != remote_major:
        return ProtocolCompatibility(
            compatible=False,
            local_version=local_version,
            remote_version=remote_version,
            status="major_mismatch",
            warning=(
                "프로토콜 주 버전이 호환되지 않습니다: "
                f"local={local_version}, remote={remote_version}"
            ),
        )
    if local_version != remote_version:
        return ProtocolCompatibility(
            compatible=True,
            local_version=local_version,
            remote_version=remote_version,
            status="version_warning",
            warning=(
                "프로토콜 세부 버전이 다르지만 주 버전이 같아 호환됩니다: "
                f"local={local_version}, remote={remote_version}"
            ),
        )
    return ProtocolCompatibility(
        compatible=True,
        local_version=local_version,
        remote_version=remote_version,
        status="compatible",
    )


def is_protocol_compatible(remote_version: str, *, local_version: str = PROTOCOL_VERSION) -> bool:
    """Return whether the remote protocol has the same validated major version."""

    return protocol_compatibility(remote_version, local_version=local_version).compatible


def runtime_identity(
    component: str,
    *,
    environ: Mapping[str, str] | None = None,
    executable: str | Path | None = None,
    frozen: bool | None = None,
) -> RuntimeIdentity:
    """Build runtime identity from explicit build metadata when it is available.

    Release builders may set ``NIVELLE_BUILD_COMMIT`` and ``NIVELLE_BUILD_TIME``.
    The former environment names remain read-only fallbacks for the 0.4 transition.
    Missing values intentionally remain null instead of being guessed at runtime.
    """

    source = os.environ if environ is None else environ
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    executable_value = (
        str(executable)
        if executable is not None
        else source.get("NIVELLE_EXECUTABLE_PATH")
        or source.get("NOZOMI_EXECUTABLE_PATH", sys.executable)
    )
    resolved_executable = str(Path(executable_value).expanduser().resolve())
    build_commit = source.get("NIVELLE_BUILD_COMMIT") or source.get("NOZOMI_BUILD_COMMIT") or None
    if build_commit is None:
        development = _DEVELOPMENT_VERSION_PATTERN.search(APP_VERSION)
        if development is not None:
            build_commit = development.group("commit").lower()
    build_time = source.get("NIVELLE_BUILD_TIME") or source.get("NOZOMI_BUILD_TIME") or None
    return RuntimeIdentity(
        component=component,
        app_version=APP_VERSION,
        build_commit=build_commit,
        build_time=build_time,
        executable_path=resolved_executable,
        frozen=is_frozen,
    )


def startup_log_line(component: str) -> str:
    """Return one structured startup record without credentials or request data."""

    return json.dumps(
        runtime_identity(component).model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def emit_startup_log(component: str, *, stream: TextIO | None = None) -> str:
    """Write startup identity when a console stream exists and return the record."""

    line = startup_log_line(component)
    destination = stream if stream is not None else (sys.stdout or sys.stderr)
    if destination is not None:
        destination.write(line + "\n")
        destination.flush()
    return line
