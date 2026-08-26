from __future__ import annotations

import codecs
import os
from pathlib import Path
from typing import Any

from nivelle_protocol.tools import ReadTextFileArguments

from .errors import AgentError, PathValidationError
from .models import AgentPolicy
from .path_security import PathIdentity, WindowsPathValidator, sanitize_display_text
from .result_utils import untrusted_result
from .search import CancellationSignal


def _looks_binary(payload: bytes) -> bool:
    if b"\x00" in payload[:8_192]:
        # UTF-16/32 BOMs are handled before this check.
        return True
    sample = payload[:8_192]
    if not sample:
        return False
    suspicious = sum(byte < 32 and byte not in {8, 9, 10, 12, 13} for byte in sample)
    return suspicious / len(sample) > 0.05


def _decode_text(payload: bytes) -> tuple[str, str, bool]:
    if payload.startswith(codecs.BOM_UTF8):
        return payload.decode("utf-8-sig"), "utf-8-sig", False
    if payload.startswith(codecs.BOM_UTF32_LE) or payload.startswith(codecs.BOM_UTF32_BE):
        return payload.decode("utf-32"), "utf-32", False
    if payload.startswith(codecs.BOM_UTF16_LE) or payload.startswith(codecs.BOM_UTF16_BE):
        return payload.decode("utf-16"), "utf-16", False
    if _looks_binary(payload):
        raise AgentError("validation_failed", "Binary files cannot be read as text.")
    try:
        return payload.decode("utf-8"), "utf-8", False
    except UnicodeDecodeError:
        pass
    for encoding in ("cp949", "cp1252"):
        try:
            return payload.decode(encoding), encoding, True
        except UnicodeDecodeError:
            continue
    raise AgentError("validation_failed", "The file encoding is not safely supported.")


def _opened_identity(stream: Any) -> PathIdentity:
    info = os.fstat(stream.fileno())
    return PathIdentity(info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _raise_if_cancelled(cancellation: CancellationSignal | None) -> None:
    if cancellation is not None and cancellation.is_set():
        raise AgentError("cancelled", "The text read was cancelled.")


def read_text_file(
    arguments_payload: dict[str, Any],
    *,
    policy: AgentPolicy,
    cancellation: CancellationSignal | None = None,
) -> dict[str, Any]:
    _raise_if_cancelled(cancellation)
    arguments = ReadTextFileArguments.model_validate(arguments_payload)
    validator = WindowsPathValidator(policy)
    if arguments.path_ref is not None:
        root_id, target = validator.resolve_path_ref(arguments.path_ref)
    else:
        if not policy.allow_direct_paths:
            raise AgentError("permission_denied", "Direct filesystem paths are disabled locally.")
        root_id, target = None, Path(arguments.path or "")
    byte_limit = min(
        policy.limits.default_read_file_bytes, policy.limits.hard_read_file_bytes
    )
    validated = validator.validate(
        target,
        root_id=root_id,
        expected_type="file",
        max_size=byte_limit,
        reject_sensitive=True,
    )
    root = policy.filesystem_roots[validated.root_id]
    if not root.allow_read:
        raise AgentError("permission_denied", "Text reads are disabled for this root.")
    checked = validator.revalidate(
        validated, expected_type="file", max_size=byte_limit, reject_sensitive=True
    )
    _raise_if_cancelled(cancellation)
    try:
        with checked.path.open("rb") as stream:
            if checked.identity is not None and _opened_identity(stream) != checked.identity:
                raise PathValidationError(
                    "path_not_allowed", "The file changed during validation; access was cancelled."
                )
            chunks: list[bytes] = []
            remaining = byte_limit + 1
            while remaining > 0:
                _raise_if_cancelled(cancellation)
                chunk = stream.read(min(65_536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
    except OSError as exc:
        raise AgentError("execution_failed", "The text file could not be read.") from exc
    if len(payload) > byte_limit:
        raise AgentError("result_too_large", "The requested file is too large.")
    _raise_if_cancelled(cancellation)
    text, encoding, encoding_uncertain = _decode_text(payload)
    lines = text.splitlines(keepends=True)
    start = arguments.start_line - 1
    max_lines = min(arguments.max_lines, policy.limits.hard_return_lines)
    max_characters = min(arguments.max_characters, policy.limits.hard_return_characters)
    selected_lines = lines[start : start + max_lines]
    selected = "".join(selected_lines)
    truncated_by_characters = len(selected) > max_characters
    selected = selected[:max_characters]
    truncated = (
        truncated_by_characters
        or start > 0
        or start + len(selected_lines) < len(lines)
    )
    safe_text = sanitize_display_text(selected)
    path_ref = validator.make_path_ref(checked.root_id, checked.relative_path)
    if len(path_ref) > 512:
        raise AgentError("result_too_large", "The file path reference is too long to return safely.")
    content = {
        "root_id": checked.root_id,
        "path_ref": path_ref,
        "relative_path": sanitize_display_text(checked.relative_path),
        "encoding": encoding,
        "encoding_uncertain": encoding_uncertain,
        "start_line": arguments.start_line,
        "returned_lines": len(selected_lines),
        "text": safe_text,
    }
    return untrusted_result(
        "read_text_file",
        content=content,
        truncated=truncated,
        original_size=len(text.encode("utf-8")),
        returned_size=len(safe_text.encode("utf-8")),
        omitted_count=max(0, len(lines) - len(selected_lines)),
    )
