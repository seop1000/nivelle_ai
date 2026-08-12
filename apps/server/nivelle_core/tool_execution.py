from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from nivelle_protocol.settings import AgentSettings
from nivelle_protocol.tools import (
    TOOL_REGISTRY,
    ApprovalMode,
    ClientCapabilities,
    ToolCapability,
    ToolErrorCode,
    ToolRequest,
    ToolStatus,
)
from pydantic import ValidationError

from .agent_gateway import (
    AgentGateway,
    AgentGatewayError,
    AgentRemoteTerminalError,
)
from .llm import LLMToolProposal
from .tool_orchestrator import ToolOrchestrator
from .tool_repository import ToolCallCreate, ToolLimitExceededError


def _safe_failure(
    *,
    source_tool: str,
    status: str,
    error_code: str,
    safe_summary: str,
) -> dict[str, Any]:
    return {
        "source_tool": source_tool,
        "trusted": False,
        "result_id": str(uuid4()),
        "status": status,
        "result": None,
        "safe_summary": safe_summary,
        "error_code": error_code,
        "retryable": False,
    }


def planning_failure() -> dict[str, Any]:
    return _safe_failure(
        source_tool="tool_planner",
        status=ToolStatus.VALIDATION_FAILED.value,
        error_code=ToolErrorCode.VALIDATION_FAILED.value,
        safe_summary=(
            "No local action was executed because the model did not produce a valid "
            "structured tool proposal."
        ),
    )


def _capability_map(
    capabilities: ClientCapabilities,
) -> dict[str, ToolCapability]:
    return {
        item.tool_name: item
        for item in capabilities.tools
        if item.enabled and item.implementation_available
    }


async def execute_tool_proposals(
    proposals: Sequence[LLMToolProposal],
    *,
    request_id: UUID,
    conversation_id: UUID,
    user_message_id: UUID,
    assistant_message_id: UUID,
    capabilities: ClientCapabilities,
    settings: AgentSettings,
    orchestrator: ToolOrchestrator,
    gateway: AgentGateway,
) -> list[dict[str, Any]]:
    """Validate, persist, route, and await a bounded list of model proposals."""

    results: list[dict[str, Any]] = []
    capability_by_name = _capability_map(capabilities)
    for proposal in proposals[: settings.max_calls_per_turn]:
        capability = capability_by_name.get(proposal.name)
        if capability is None:
            results.append(
                _safe_failure(
                    source_tool=proposal.name or "unknown_tool",
                    status=ToolStatus.VALIDATION_FAILED.value,
                    error_code=ToolErrorCode.UNSUPPORTED_TOOL.value,
                    safe_summary="The active Link did not advertise this tool.",
                )
            )
            continue
        try:
            definition = TOOL_REGISTRY.validate_capability(capability)
            typed_arguments = definition.argument_schema.model_validate(
                proposal.arguments
            )
        except (ValidationError, ValueError):
            results.append(
                _safe_failure(
                    source_tool=proposal.name or "unknown_tool",
                    status=ToolStatus.VALIDATION_FAILED.value,
                    error_code=ToolErrorCode.VALIDATION_FAILED.value,
                    safe_summary="The proposed local tool arguments were invalid.",
                )
            )
            continue

        tool_call_id = uuid4()
        idempotency_key = uuid4()
        timeout_ms = min(
            capability.default_timeout_ms,
            capability.maximum_timeout_ms,
            definition.maximum_timeout_ms,
            settings.result_timeout_seconds * 1_000,
        )
        request = ToolRequest(
            tool_call_id=tool_call_id,
            request_id=request_id,
            idempotency_key=idempotency_key,
            conversation_id=conversation_id,
            user_message_id=user_message_id,
            target_client_id=capabilities.client_id,
            target_session_id=capabilities.session_id,
            tool_name=definition.name,
            tool_version=definition.version,
            arguments=typed_arguments.model_dump(mode="json"),
            risk_level=definition.risk_level,
            created_at=datetime.now(UTC),
            timeout_ms=timeout_ms,
            user_intent_summary=(
                f"The user requested the registered local tool: {definition.display_name}."
            ),
        )
        call = ToolCallCreate(
            tool_call_id=str(tool_call_id),
            request_id=str(request_id),
            idempotency_key=str(idempotency_key),
            conversation_id=str(conversation_id),
            user_message_id=str(user_message_id),
            assistant_message_id=str(assistant_message_id),
            target_client_id=str(capabilities.client_id),
            target_session_id=str(capabilities.session_id),
            tool_name=definition.name,
            tool_version=definition.version,
            risk_level=definition.risk_level.value,
            arguments_summary=(
                "validated argument fields: "
                + ", ".join(sorted(typed_arguments.model_dump(mode="json")))
            ),
            approval_mode=capability.default_approval_mode.value,
        )
        try:
            outcome = await orchestrator.propose(call)
            if outcome.replayed:
                results.append(
                    _safe_failure(
                        source_tool=definition.name,
                        status=ToolStatus.FAILED.value,
                        error_code=ToolErrorCode.DUPLICATE_REQUEST.value,
                        safe_summary=(
                            "The duplicate local tool request was not executed again."
                        ),
                    )
                )
                continue
            if not await orchestrator.validate(str(tool_call_id)):
                results.append(
                    _safe_failure(
                        source_tool=definition.name,
                        status=ToolStatus.VALIDATION_FAILED.value,
                        error_code=ToolErrorCode.CLIENT_OFFLINE.value,
                        safe_summary="The exact target Link capability was unavailable.",
                    )
                )
                continue
            if capability.default_approval_mode is not ApprovalMode.NOT_REQUIRED:
                await orchestrator.require_approval(str(tool_call_id))
            result = await gateway.execute(request)
            results.append(result.model_dump(mode="json"))
        except ToolLimitExceededError:
            results.append(
                _safe_failure(
                    source_tool=definition.name,
                    status=ToolStatus.FAILED.value,
                    error_code=ToolErrorCode.EXECUTION_FAILED.value,
                    safe_summary=(
                        "The bounded local tool-call limit was reached; the extra "
                        "proposal was not executed."
                    ),
                )
            )
        except AgentRemoteTerminalError as exc:
            results.append(
                _safe_failure(
                    source_tool=definition.name,
                    status=exc.status.value,
                    error_code=(
                        exc.error_code.value
                        if exc.error_code is not None
                        else ToolErrorCode.EXECUTION_FAILED.value
                    ),
                    safe_summary=(
                        "The local tool did not complete successfully on Nivelle Link."
                    ),
                )
            )
        except AgentGatewayError:
            results.append(
                _safe_failure(
                    source_tool=definition.name,
                    status=ToolStatus.CLIENT_DISCONNECTED.value,
                    error_code=ToolErrorCode.CLIENT_DISCONNECTED.value,
                    safe_summary=(
                        "The exact target Link session became unavailable; the action was not "
                        "rerouted or retried."
                    ),
                )
            )
    return results


__all__ = ["execute_tool_proposals", "planning_failure"]
