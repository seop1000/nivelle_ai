from __future__ import annotations

import os
import re
import tempfile
import unicodedata
import uuid
from pathlib import Path
from typing import Any

from nivelle_protocol.tools import CreateNoteArguments

from .errors import AgentError
from .idempotency import IdempotencyCache
from .models import AgentToolRequest

_INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "CLOCK$",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


def sanitize_note_filename(title: str) -> str:
    normalized = unicodedata.normalize("NFKC", title)
    cleaned = _INVALID_FILENAME.sub("_", normalized).strip(" .")
    cleaned = re.sub(r"\s+", " ", cleaned)[:80].rstrip(" .")
    if not cleaned or cleaned.split(".", 1)[0].upper() in _RESERVED:
        return "note"
    return cleaned


class NoteWriter:
    def __init__(self, notes_directory: Path, idempotency: IdempotencyCache) -> None:
        self.notes_directory = notes_directory
        self.idempotency = idempotency

    def create(
        self, request: AgentToolRequest, arguments_payload: dict[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        arguments = CreateNoteArguments.model_validate(arguments_payload)
        filename = sanitize_note_filename(arguments.title)
        encoded = arguments.content.encode("utf-8")

        def write_note() -> dict[str, Any]:
            self.notes_directory.mkdir(parents=True, exist_ok=True)
            descriptor, temp_name = tempfile.mkstemp(
                prefix=".nivelle-note-", suffix=".tmp", dir=self.notes_directory
            )
            temporary = Path(temp_name)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                for suffix in range(0, 10_000):
                    suffix_text = "" if suffix == 0 else f" ({suffix})"
                    destination = self.notes_directory / f"{filename}{suffix_text}.{arguments.format}"
                    try:
                        os.link(temporary, destination)
                    except FileExistsError:
                        continue
                    except OSError as exc:
                        raise AgentError(
                            "execution_failed", "The note could not be committed atomically."
                        ) from exc
                    return {
                        "note_id": str(uuid.uuid4()),
                        "title": arguments.title,
                        "format": arguments.format,
                        "filename": destination.name,
                        "path": str(destination.resolve()),
                        "path_ref": f"nivelle-notes:{destination.name}",
                        "safe_path_summary": f"Nivelle Notes / {destination.name}",
                        "size_bytes": len(encoded),
                    }
                raise AgentError("execution_failed", "No safe note filename was available.")
            finally:
                temporary.unlink(missing_ok=True)

        return self.idempotency.execute_once(
            idempotency_key=request.idempotency_key,
            tool_name=request.tool_name,
            arguments=request.arguments,
            reconcile_completed=True,
            business_arguments={
                "conversation_id": request.conversation_id,
                "arguments": arguments.model_dump(mode="json"),
            },
            operation=write_note,
        )
