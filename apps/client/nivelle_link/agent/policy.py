from __future__ import annotations

import re
import threading
from collections.abc import Callable
from pathlib import Path

from nivelle_protocol.tools import TOOL_REGISTRY

from .application import _validate_registered_executable
from .atomic_store import atomic_write_json, read_json
from .errors import PathValidationError
from .models import (
    AgentPolicy,
    ApprovalMode,
    FilesystemRoot,
    RegisteredApplication,
)
from .path_security import _validate_raw_windows_path, is_reparse_point

_LOCAL_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")


def validate_local_id(value: str, *, kind: str) -> str:
    normalized = value.strip()
    if not _LOCAL_ID_PATTERN.fullmatch(normalized):
        raise ValueError(
            f"{kind} ID must be 1-100 characters using letters, numbers, '.', '_', or '-'."
        )
    return normalized


def canonical_existing_directory(path: Path | str, policy: AgentPolicy) -> Path:
    normalized = _validate_raw_windows_path(
        str(path), allow_network_paths=policy.allow_network_paths
    )
    candidate = Path(normalized)
    if not candidate.is_absolute():
        raise PathValidationError("path_not_allowed", "The root path must be absolute.")
    try:
        canonical = candidate.resolve(strict=True)
    except OSError as exc:
        raise PathValidationError("target_not_found", "The root folder is unavailable.") from exc
    if not canonical.is_dir():
        raise PathValidationError("path_not_allowed", "The root path must be a folder.")
    if not policy.allow_reparse_points:
        current = Path(canonical.anchor)
        for component in canonical.parts[1:]:
            current /= component
            if is_reparse_point(current):
                raise PathValidationError(
                    "path_not_allowed",
                    "Filesystem roots cannot pass through symbolic links or junctions.",
                )
    return canonical


class PolicyStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()

    def load(self) -> AgentPolicy:
        payload = read_json(self.path, {})
        return AgentPolicy.model_validate(payload)

    def save(self, policy: AgentPolicy) -> None:
        atomic_write_json(self.path, policy.model_dump(mode="json"))

    def update(self, change: Callable[[AgentPolicy], AgentPolicy]) -> AgentPolicy:
        """Apply one validated local policy edit and persist it atomically."""

        with self._lock:
            # Missing storage already loads the deny-by-default model.  A
            # present but unreadable/invalid policy must fail closed instead of
            # being silently replaced with a broader default document.
            current = self.load()
            updated = change(current)
            # A policy edit must invalidate approvals granted against the old
            # allowlist even when the effective permission looks similar.
            try:
                next_version = str(int(current.policy_version) + 1)
            except ValueError:
                next_version = f"{current.policy_version}.1"[-64:]
            updated = updated.model_copy(update={"policy_version": next_version})
            updated = AgentPolicy.model_validate(updated.model_dump(mode="python"))
            self.save(updated)
            return updated


class LocalAgentPolicyEditor:
    """Client-owned CRUD boundary used by the management window, online or offline."""

    def __init__(self, store: PolicyStore) -> None:
        self.store = store

    def set_agent_enabled(self, enabled: bool) -> AgentPolicy:
        return self.store.update(
            lambda policy: policy.model_copy(update={"agent_enabled": enabled})
        )

    def update_tool(
        self,
        tool_name: str,
        *,
        enabled: bool,
        approval_mode: ApprovalMode,
        timeout_ms: int,
    ) -> AgentPolicy:
        definition = next(
            (item for item in TOOL_REGISTRY if item.name == tool_name), None
        )
        if definition is None:
            raise ValueError("Unknown Agent tool.")
        if timeout_ms > definition.maximum_timeout_ms:
            raise ValueError(
                f"{tool_name} timeout cannot exceed {definition.maximum_timeout_ms} ms."
            )

        def change(policy: AgentPolicy) -> AgentPolicy:
            enabled_tools = set(policy.enabled_tools)
            if enabled:
                enabled_tools.add(tool_name)
            else:
                enabled_tools.discard(tool_name)
            approvals = dict(policy.approval_defaults)
            approvals[tool_name] = approval_mode
            timeouts = dict(policy.tool_timeouts_ms)
            timeouts[tool_name] = timeout_ms
            return policy.model_copy(
                update={
                    "enabled_tools": enabled_tools,
                    "approval_defaults": approvals,
                    "tool_timeouts_ms": timeouts,
                }
            )

        return self.store.update(change)

    def upsert_application(
        self,
        application_id: str,
        *,
        previous_application_id: str | None = None,
        display_name: str,
        executable_path: Path | str,
        enabled: bool,
    ) -> AgentPolicy:
        application_id = validate_local_id(application_id, kind="Application")

        def change(policy: AgentPolicy) -> AgentPolicy:
            canonical = _validate_registered_executable(Path(executable_path), policy)
            applications = dict(policy.applications)
            if application_id in applications and previous_application_id != application_id:
                raise ValueError("The application ID is already registered.")
            if previous_application_id and previous_application_id != application_id:
                applications.pop(
                    validate_local_id(previous_application_id, kind="Application"), None
                )
            applications[application_id] = RegisteredApplication(
                display_name=display_name.strip(),
                executable_path=canonical,
                enabled=enabled,
            )
            return policy.model_copy(update={"applications": applications})

        return self.store.update(change)

    def remove_application(self, application_id: str) -> AgentPolicy:
        application_id = validate_local_id(application_id, kind="Application")

        def change(policy: AgentPolicy) -> AgentPolicy:
            applications = dict(policy.applications)
            applications.pop(application_id, None)
            return policy.model_copy(update={"applications": applications})

        return self.store.update(change)

    def upsert_root(
        self,
        root_id: str,
        *,
        previous_root_id: str | None = None,
        display_name: str,
        path: Path | str,
        allow_search: bool,
        allow_read: bool,
        allow_open_folder: bool,
    ) -> AgentPolicy:
        root_id = validate_local_id(root_id, kind="Root")

        def change(policy: AgentPolicy) -> AgentPolicy:
            canonical = canonical_existing_directory(path, policy)
            roots = dict(policy.filesystem_roots)
            if root_id in roots and previous_root_id != root_id:
                raise ValueError("The root ID is already registered.")
            if previous_root_id and previous_root_id != root_id:
                roots.pop(validate_local_id(previous_root_id, kind="Root"), None)
            roots[root_id] = FilesystemRoot(
                display_name=display_name.strip(),
                path=canonical,
                allow_search=allow_search,
                allow_read=allow_read,
                allow_open_folder=allow_open_folder,
            )
            return policy.model_copy(update={"filesystem_roots": roots})

        return self.store.update(change)

    def remove_root(self, root_id: str) -> AgentPolicy:
        root_id = validate_local_id(root_id, kind="Root")

        def change(policy: AgentPolicy) -> AgentPolicy:
            roots = dict(policy.filesystem_roots)
            roots.pop(root_id, None)
            return policy.model_copy(update={"filesystem_roots": roots})

        return self.store.update(change)

    def set_path_policies(
        self, *, allow_hidden_files: bool, allow_network_paths: bool
    ) -> AgentPolicy:
        def change(policy: AgentPolicy) -> AgentPolicy:
            if not allow_network_paths:
                registered_paths = [
                    *(item.executable_path for item in policy.applications.values()),
                    *(item.path for item in policy.filesystem_roots.values()),
                ]
                if any(str(path).replace("/", "\\").startswith("\\\\") for path in registered_paths):
                    raise PathValidationError(
                        "path_not_allowed",
                        "Remove registered network paths before disabling network-path access.",
                    )
            return policy.model_copy(
                update={
                    "allow_hidden_files": allow_hidden_files,
                    "allow_network_paths": allow_network_paths,
                }
            )

        return self.store.update(change)


__all__ = [
    "LocalAgentPolicyEditor",
    "PolicyStore",
    "canonical_existing_directory",
    "validate_local_id",
]
