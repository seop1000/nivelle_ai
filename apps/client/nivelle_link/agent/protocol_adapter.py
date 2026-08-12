from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from nivelle_protocol.tools import (
    TOOL_REGISTRY,
    ToolErrorCode,
    ToolRequest,
    ToolResult,
    ToolStatus,
)
from pydantic import BaseModel

from .models import AgentToolRequest


def to_agent_request(request: ToolRequest) -> AgentToolRequest:
    """Revalidate the shared request and copy only execution-relevant fields."""

    validated_arguments = TOOL_REGISTRY.validate_request(request)
    return AgentToolRequest(
        tool_call_id=str(request.tool_call_id),
        request_id=str(request.request_id),
        idempotency_key=str(request.idempotency_key),
        conversation_id=str(request.conversation_id),
        user_message_id=str(request.user_message_id),
        target_client_id=str(request.target_client_id),
        target_session_id=str(request.target_session_id),
        tool_name=request.tool_name,
        tool_version=request.tool_version,
        arguments=validated_arguments.model_dump(mode="json"),
        risk_level=request.risk_level.value,
        created_at=request.created_at,
        timeout_ms=request.timeout_ms,
        user_intent_summary=request.user_intent_summary,
    )


def _base_untrusted(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_tool": raw["source_tool"],
        "trusted": False,
        "result_id": raw["result_id"],
    }


def normalize_result_payload(
    tool_name: str, raw: dict[str, Any], *, replayed: bool
) -> BaseModel:
    definition = TOOL_REGISTRY.require(tool_name)
    if tool_name == "get_system_status":
        operating_system = raw.get("operating_system") or {}
        unsupported: list[str] = []
        if raw.get("battery") is None:
            unsupported.append("battery")
        payload: dict[str, Any] = {
            "operating_system": " ".join(
                str(operating_system.get(key, "")) for key in ("name", "release", "architecture")
            ).strip()
            or None,
            "client_display_name": raw["client_display_name"],
            "cpu_usage_percent": raw.get("cpu_percent"),
            "ram_usage_percent": raw.get("ram", {}).get("percent"),
            "ram_total_bytes": raw.get("ram", {}).get("total_bytes"),
            "ram_available_bytes": raw.get("ram", {}).get("available_bytes"),
            "disk_volumes": [
                {
                    "volume_name": item["volume"],
                    "total_bytes": item["total_bytes"],
                    "free_bytes": item["free_bytes"],
                }
                for item in raw.get("local_volumes", [])
            ],
            "battery": (
                {
                    "percent": raw["battery"].get("percent"),
                    "plugged_in": raw["battery"].get("plugged_in"),
                    "seconds_remaining": raw["battery"].get("seconds_left"),
                }
                if raw.get("battery") is not None
                else None
            ),
            "network": {
                "connected": raw.get("network", {}).get("available"),
                "connection_type": "unknown",
                "internet_reachable": None,
            },
            "link_uptime_seconds": raw["link_uptime_seconds"],
            "link_version": raw["link_version"],
            "unsupported_metrics": unsupported,
        }
    elif tool_name == "get_active_window":
        content = raw["content"]
        payload = _base_untrusted(raw) | content
    elif tool_name == "open_application":
        payload = {
            "application_id": raw["application_id"],
            "launched": True,
            "process_id": raw.get("process_id"),
            "already_executed": replayed,
        }
    elif tool_name == "open_folder":
        content = raw["content"]
        payload = _base_untrusted(raw) | {
            "path_ref": content["path_ref"],
            "safe_path_summary": f"{content['root_id']} / {content['relative_path']}",
            "opened": True,
            "already_executed": replayed,
        }
    elif tool_name == "search_files":
        content = raw["content"]
        payload = _base_untrusted(raw) | {
            "root_id": content["root_id"],
            "query": content["query"],
            "items": [
                {
                    "path_ref": item["path_ref"],
                    "name": item["filename"],
                    "relative_path": item["relative_path"],
                    "type": item["type"],
                    "size_bytes": item["size"],
                    "modified_at": item["modified_at"],
                }
                for item in content["items"]
            ],
            "truncated": raw["truncated"],
            "original_size": raw["original_size"],
            "returned_size": raw["returned_size"],
            "omitted_count": raw["omitted_count"],
        }
    elif tool_name == "read_text_file":
        content = raw["content"]
        payload = _base_untrusted(raw) | {
            "path_ref": content["path_ref"],
            "content": content["text"],
            "encoding": content["encoding"],
            "encoding_uncertain": content["encoding_uncertain"],
            "start_line": content["start_line"],
            "returned_lines": content["returned_lines"],
            "has_more": raw["truncated"],
            "truncated": raw["truncated"],
            "original_size": raw["original_size"],
            "returned_size": raw["returned_size"],
            "omitted_count": raw["omitted_count"],
        }
    elif tool_name == "create_note":
        payload = {
            "note_id": raw["note_id"],
            "title": raw["title"],
            "format": raw["format"],
            "path_ref": raw["path_ref"],
            "safe_path_summary": raw["safe_path_summary"],
            "size_bytes": raw["size_bytes"],
            "already_executed": replayed,
        }
    elif tool_name == "set_reminder":
        payload = {
            "reminder_id": raw["reminder_id"],
            "title": raw["title"],
            "scheduled_at": raw["scheduled_at"],
            "timezone": raw["timezone"],
            "created": True,
            "already_executed": replayed,
        }
    else:
        raise ValueError(f"Unsupported local tool result: {tool_name}")
    return definition.result_schema.model_validate(payload)


def make_tool_result(
    request: ToolRequest,
    *,
    status: ToolStatus,
    started_at: datetime,
    completed_at: datetime,
    result: BaseModel | None,
    safe_summary: str,
    error_code: str | None = None,
    error_message: str | None = None,
    retryable: bool = False,
) -> ToolResult:
    result_payload = result.model_dump(mode="json") if result is not None else None
    truncated = bool(result_payload and result_payload.get("truncated", False))
    tool_result = ToolResult(
        result_id=uuid4(),
        source_tool=request.tool_name,
        trusted=False,
        tool_call_id=request.tool_call_id,
        request_id=request.request_id,
        target_client_id=request.target_client_id,
        target_session_id=request.target_session_id,
        tool_name=request.tool_name,
        tool_version=request.tool_version,
        status=status,
        started_at=started_at,
        completed_at=completed_at,
        duration_ms=max(0, int((completed_at - started_at).total_seconds() * 1_000)),
        result=result_payload,
        safe_summary=safe_summary,
        truncated=truncated,
        original_size=(result_payload or {}).get("original_size"),
        returned_size=(result_payload or {}).get("returned_size"),
        omitted_count=(result_payload or {}).get("omitted_count"),
        error_code=ToolErrorCode(error_code) if error_code is not None else None,
        error_message=error_message,
        retryable=retryable,
    )
    if result is not None:
        TOOL_REGISTRY.validate_result(tool_result)
    return tool_result
