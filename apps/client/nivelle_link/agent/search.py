from __future__ import annotations

import os
import threading
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from nivelle_protocol.tools import SearchFilesArguments

from .errors import AgentError
from .models import AgentPolicy
from .path_security import (
    WindowsPathValidator,
    is_reparse_point,
    sanitize_display_text,
)
from .result_utils import untrusted_result


class CancellationSignal(Protocol):
    def is_set(self) -> bool: ...


_MAX_SCANNED_ENTRIES = 50_000


def _check_search_budget(
    *,
    cancellation: CancellationSignal | None,
    monotonic: Any,
    started: float,
    timeout_seconds: float,
) -> None:
    if cancellation is not None and cancellation.is_set():
        raise AgentError("cancelled", "The filename search was cancelled.")
    if monotonic() - started > timeout_seconds:
        raise AgentError("timed_out", "The filename search timed out.")


def search_files(
    arguments_payload: dict[str, Any],
    *,
    policy: AgentPolicy,
    cancellation: CancellationSignal | None = None,
    timeout_seconds: float = 10.0,
    monotonic: Any = time.monotonic,
) -> dict[str, Any]:
    arguments = SearchFilesArguments.model_validate(arguments_payload)
    root_config = policy.filesystem_roots.get(arguments.root_id)
    if root_config is None or not root_config.allow_search:
        raise AgentError("permission_denied", "Filename search is disabled for this root.")
    max_results = min(arguments.max_results, policy.limits.hard_max_results)
    max_depth = min(arguments.max_depth, policy.limits.default_max_depth)
    validator = WindowsPathValidator(policy)
    root = validator.validate(
        root_config.path,
        root_id=arguments.root_id,
        expected_type="directory",
        reject_sensitive=True,
    )
    query = arguments.query.casefold()
    started = monotonic()
    pending: deque[tuple[Path, int]] = deque([(root.path, 0)])
    visited_directories = {os.path.normcase(str(root.path)).casefold()}
    seen_entries: set[str] = set()
    results: list[tuple[str, dict[str, Any]]] = []
    omitted_count = 0
    scanned_entries = 0

    while pending:
        _check_search_budget(
            cancellation=cancellation,
            monotonic=monotonic,
            started=started,
            timeout_seconds=timeout_seconds,
        )
        current, depth = pending.popleft()
        try:
            with os.scandir(current) as iterator:
                for entry in iterator:
                    _check_search_budget(
                        cancellation=cancellation,
                        monotonic=monotonic,
                        started=started,
                        timeout_seconds=timeout_seconds,
                    )
                    scanned_entries += 1
                    if scanned_entries > _MAX_SCANNED_ENTRIES:
                        raise AgentError(
                            "result_too_large",
                            "The filename search scanned too many entries; narrow the query or root.",
                        )
                    entry_path = Path(entry.path)
                    if is_reparse_point(entry_path) and not policy.allow_reparse_points:
                        continue
                    try:
                        validated = validator.validate(
                            entry_path,
                            root_id=arguments.root_id,
                            expected_type="any",
                            reject_sensitive=True,
                        )
                    except AgentError:
                        # Never expose or traverse a target that resolves outside the approved root.
                        continue
                    canonical_key = os.path.normcase(str(validated.path)).casefold()
                    if canonical_key in seen_entries:
                        continue
                    seen_entries.add(canonical_key)
                    try:
                        is_directory = validated.path.is_dir()
                        is_file = validated.path.is_file()
                        info = validated.path.stat()
                    except OSError:
                        continue
                    if is_directory and depth < max_depth:
                        if canonical_key not in visited_directories:
                            visited_directories.add(canonical_key)
                            pending.append((validated.path, depth + 1))

                    suffix_allowed = (
                        not arguments.extensions
                        or validated.path.suffix.casefold().removeprefix(".")
                        in arguments.extensions
                    )
                    type_allowed = is_file or (is_directory and arguments.include_directories)
                    if (
                        query not in entry.name.casefold()
                        or not suffix_allowed
                        or not type_allowed
                    ):
                        continue
                    relative = validated.relative_path
                    path_ref = validator.make_path_ref(arguments.root_id, relative)
                    if len(path_ref) > 512:
                        omitted_count += 1
                        continue
                    item = {
                        "filename": sanitize_display_text(entry.name),
                        "relative_path": sanitize_display_text(relative),
                        "path_ref": path_ref,
                        "type": "directory" if is_directory else "file",
                        "size": None if is_directory else info.st_size,
                        "modified_at": datetime.fromtimestamp(info.st_mtime, UTC).isoformat(),
                    }
                    sort_key = f"{relative.casefold()}\0{relative}"
                    results.append((sort_key, item))
                    results.sort(key=lambda candidate: candidate[0])
                    if len(results) > max_results:
                        results.pop()
                        omitted_count += 1
        except (FileNotFoundError, PermissionError, OSError):
            continue

    validator.revalidate(root, expected_type="directory", reject_sensitive=True)
    result_items = [item for _, item in results]
    content = {
        "root_id": arguments.root_id,
        "query": sanitize_display_text(arguments.query),
        "items": result_items,
    }
    return untrusted_result(
        "search_files",
        content=content,
        truncated=omitted_count > 0,
        original_size=len(result_items) + omitted_count,
        returned_size=len(result_items),
        omitted_count=omitted_count,
    )


def new_cancellation_event() -> threading.Event:
    return threading.Event()
