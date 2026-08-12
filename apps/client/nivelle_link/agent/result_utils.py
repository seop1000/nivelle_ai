from __future__ import annotations

import uuid
from typing import Any


def untrusted_result(
    source_tool: str,
    *,
    content: Any,
    truncated: bool = False,
    original_size: int | None = None,
    returned_size: int | None = None,
    omitted_count: int | None = None,
) -> dict[str, Any]:
    """Wrap model-visible local data so it can never be mistaken for policy."""

    return {
        "source_tool": source_tool,
        "trusted": False,
        "result_id": str(uuid.uuid4()),
        "truncated": truncated,
        "original_size": original_size,
        "returned_size": returned_size,
        "omitted_count": omitted_count,
        "content_boundary": "BEGIN_UNTRUSTED_TOOL_DATA/END_UNTRUSTED_TOOL_DATA",
        "content": content,
    }
