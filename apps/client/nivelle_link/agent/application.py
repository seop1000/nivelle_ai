from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from nivelle_protocol.tools import OpenApplicationArguments

from .errors import AgentError
from .idempotency import IdempotencyCache
from .models import (
    AgentPolicy,
    AgentToolRequest,
    is_forbidden_application_executable,
)
from .path_security import _validate_raw_windows_path, is_reparse_point


def default_application_launcher(executable: Path) -> int | None:
    process = subprocess.Popen(  # noqa: S603 - executable is a validated local allowlist entry.
        [str(executable)], shell=False, close_fds=True
    )
    return process.pid


def _validate_registered_executable(path: Path, policy: AgentPolicy) -> Path:
    normalized = _validate_raw_windows_path(
        str(path), allow_network_paths=policy.allow_network_paths
    )
    candidate = Path(normalized)
    if not candidate.is_absolute():
        raise AgentError("path_not_allowed", "The registered executable path is not absolute.")
    if not policy.allow_reparse_points:
        current = Path(candidate.anchor)
        for component in candidate.parts[1:]:
            current /= component
            if current.exists() and is_reparse_point(current):
                raise AgentError(
                    "path_not_allowed", "Registered executables cannot pass through reparse points."
                )
    try:
        canonical = candidate.resolve(strict=True)
    except OSError as exc:
        raise AgentError("target_not_found", "The registered application is unavailable.") from exc
    if not canonical.is_file() or canonical.suffix.casefold() != ".exe":
        raise AgentError("target_not_found", "The registered application is not an executable file.")
    if is_forbidden_application_executable(canonical):
        raise AgentError(
            "permission_denied",
            "Shells, interpreters, script hosts, and installers cannot be launched.",
        )
    return canonical


def open_application(
    request: AgentToolRequest,
    *,
    policy: AgentPolicy,
    idempotency: IdempotencyCache,
    launcher: Callable[[Path], int | None] = default_application_launcher,
) -> tuple[dict[str, Any], bool]:
    arguments = OpenApplicationArguments.model_validate(request.arguments)
    application = policy.applications.get(arguments.application_id)
    if application is None or not application.enabled:
        raise AgentError("target_not_found", "The application ID is not enabled locally.")
    executable = _validate_registered_executable(application.executable_path, policy)

    def launch() -> dict[str, Any]:
        process_id = launcher(executable)
        return {
            "application_id": arguments.application_id,
            "display_name": application.display_name,
            "process_id": process_id,
        }

    return idempotency.execute_once(
        idempotency_key=request.idempotency_key,
        tool_name=request.tool_name,
        arguments=request.arguments,
        operation=launch,
    )
