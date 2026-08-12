from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from nivelle_protocol import (
    TOOL_REGISTRY,
    ApprovalMode,
    ClientCapabilities,
    RiskLevel,
    ToolCapability,
    ToolEvent,
    ToolEventType,
    ToolRequest,
    ToolResult,
    ToolStatus,
)
from nivelle_protocol.tools import (
    CreateNoteArguments,
    CreateNoteResult,
    DuplicateToolRegistrationError,
    GetActiveWindowArguments,
    GetActiveWindowResult,
    GetSystemStatusArguments,
    GetSystemStatusResult,
    InvalidToolStateTransitionError,
    OpenApplicationArguments,
    OpenApplicationResult,
    OpenFolderArguments,
    OpenFolderResult,
    ReadTextFileArguments,
    ReadTextFileResult,
    SearchFileItem,
    SearchFilesArguments,
    SearchFilesResult,
    SetReminderArguments,
    SetReminderResult,
    ToolApprovalDecision,
    ToolErrorCode,
    ToolPlatform,
    ToolProgress,
    ToolProtocolError,
    ToolRegistry,
    UnknownToolError,
    UnsupportedToolVersionError,
    is_valid_tool_state_transition,
    validate_tool_event_transition,
    validate_tool_state_transition,
)
from nivelle_protocol.version import APP_VERSION, PROTOCOL_VERSION
from pydantic import ValidationError

NOW = datetime(2026, 8, 4, 0, 0, tzinfo=UTC)
FUTURE = datetime.now(UTC) + timedelta(days=2)


def make_request(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    tool_version: str | None = None,
    timeout_ms: int | None = None,
    risk_level: RiskLevel | None = None,
) -> ToolRequest:
    definition = TOOL_REGISTRY.require(tool_name)
    return ToolRequest(
        tool_call_id=uuid4(),
        request_id=uuid4(),
        idempotency_key=uuid4(),
        conversation_id=uuid4(),
        user_message_id=uuid4(),
        target_client_id=uuid4(),
        target_session_id=uuid4(),
        tool_name=tool_name,
        tool_version=tool_version or definition.version,
        arguments=arguments,
        risk_level=risk_level or definition.risk_level,
        created_at=NOW,
        timeout_ms=timeout_ms or definition.default_timeout_ms,
        user_intent_summary="사용자가 안전한 로컬 작업을 요청했습니다.",
    )


def make_result(tool_name: str, result: dict[str, Any]) -> ToolResult:
    definition = TOOL_REGISTRY.require(tool_name)
    return ToolResult(
        result_id=uuid4(),
        source_tool=tool_name,
        tool_call_id=uuid4(),
        request_id=uuid4(),
        target_client_id=uuid4(),
        target_session_id=uuid4(),
        tool_name=tool_name,
        tool_version=definition.version,
        status=ToolStatus.COMPLETED,
        started_at=NOW,
        completed_at=NOW + timedelta(milliseconds=20),
        duration_ms=20,
        result=result,
        safe_summary="요청한 작업을 완료했습니다.",
    )


def event_fields() -> dict[str, Any]:
    return {
        "tool_call_id": uuid4(),
        "request_id": uuid4(),
        "target_client_id": uuid4(),
        "target_session_id": uuid4(),
        "occurred_at": NOW,
    }


def test_registry_contains_only_the_eight_classified_phase3_tools() -> None:
    expected = {
        "get_system_status": RiskLevel.SAFE_STATUS,
        "get_active_window": RiskLevel.LOCAL_READ,
        "search_files": RiskLevel.LOCAL_READ,
        "read_text_file": RiskLevel.LOCAL_READ,
        "open_application": RiskLevel.INTERACTIVE,
        "open_folder": RiskLevel.INTERACTIVE,
        "create_note": RiskLevel.LOCAL_WRITE,
        "set_reminder": RiskLevel.LOCAL_WRITE,
    }

    assert len(TOOL_REGISTRY) == 8
    assert TOOL_REGISTRY.names == frozenset(expected)
    assert {definition.name: definition.risk_level for definition in TOOL_REGISTRY} == expected
    assert all(
        definition.supported_platforms == frozenset({ToolPlatform.WINDOWS})
        for definition in TOOL_REGISTRY
    )
    assert not any(
        token in definition.name
        for definition in TOOL_REGISTRY
        for token in ("shell", "command", "delete", "overwrite", "terminate")
    )


def test_registry_rejects_duplicates_unknown_tools_and_versions() -> None:
    definition = TOOL_REGISTRY.require("get_system_status")
    with pytest.raises(DuplicateToolRegistrationError):
        ToolRegistry([definition, definition])

    unsafe = definition.model_copy(
        update={
            "name": "run_shell",
            "client_implementation_id": "nivelle_agent.run_shell",
        }
    )
    with pytest.raises(UnknownToolError):
        ToolRegistry([unsafe])
    with pytest.raises(UnknownToolError):
        TOOL_REGISTRY.require("invented_tool")
    with pytest.raises(UnsupportedToolVersionError):
        TOOL_REGISTRY.require("get_system_status", "2.0")


def test_registry_is_frozen_against_runtime_tool_injection() -> None:
    with pytest.raises(ToolProtocolError, match="frozen"):
        TOOL_REGISTRY.register(TOOL_REGISTRY.require("get_system_status"))


def test_tool_request_requires_uuid_correlations_and_supported_protocol() -> None:
    request = make_request("get_system_status", {})
    dumped = request.model_dump(mode="json")

    assert dumped["type"] == "tool.request"
    for field in (
        "tool_call_id",
        "request_id",
        "idempotency_key",
        "conversation_id",
        "user_message_id",
        "target_client_id",
        "target_session_id",
    ):
        assert UUID(dumped[field])
    assert request.protocol_version == PROTOCOL_VERSION

    invalid = dumped | {"target_session_id": "not-a-uuid"}
    with pytest.raises(ValidationError):
        ToolRequest.model_validate(invalid)
    with pytest.raises(ValidationError, match="Unsupported protocol version"):
        ToolRequest.model_validate(dumped | {"protocol_version": "99.0"})
    with pytest.raises(ValidationError):
        ToolRequest.model_validate(dumped | {"created_at": "2026-08-04T00:00:00"})
    with pytest.raises(ValidationError):
        ToolRequest.model_validate(dumped | {"unexpected": "blocked"})


@pytest.mark.parametrize(
    ("tool_name", "arguments", "model_type"),
    [
        ("get_system_status", {}, GetSystemStatusArguments),
        ("get_active_window", {}, GetActiveWindowArguments),
        ("open_application", {"application_id": "visual-studio-code"}, OpenApplicationArguments),
        ("open_folder", {"path_ref": "root-1:project"}, OpenFolderArguments),
        (
            "search_files",
            {"query": "README", "root_id": "project", "extensions": [".MD", "md"]},
            SearchFilesArguments,
        ),
        (
            "read_text_file",
            {"path_ref": "root-1:readme", "start_line": 1, "max_lines": 50},
            ReadTextFileArguments,
        ),
        (
            "create_note",
            {"title": "Phase 3 결과", "content": "정확히 보존할 내용\n", "format": "md"},
            CreateNoteArguments,
        ),
        (
            "set_reminder",
            {
                "title": "테스트",
                "reminder_text": "다시 확인",
                "scheduled_at": FUTURE.isoformat(),
                "timezone": "UTC",
            },
            SetReminderArguments,
        ),
    ],
)
def test_all_tool_argument_schemas_are_registry_validated(
    tool_name: str, arguments: dict[str, Any], model_type: type[Any]
) -> None:
    validated = TOOL_REGISTRY.validate_request(make_request(tool_name, arguments))

    assert isinstance(validated, model_type)
    if isinstance(validated, SearchFilesArguments):
        assert validated.extensions == ["md"]
    if isinstance(validated, CreateNoteArguments):
        assert validated.content.endswith("\n")


def test_tool_argument_schemas_exclude_command_and_destination_injection() -> None:
    with pytest.raises(ValidationError):
        OpenApplicationArguments(application_id="C:\\Windows\\System32\\cmd.exe")
    with pytest.raises(ValidationError):
        OpenApplicationArguments(application_id="code", arguments=["--unsafe"])
    with pytest.raises(ValidationError):
        OpenFolderArguments(path_ref="folder", path="D:\\folder")
    with pytest.raises(ValidationError):
        CreateNoteArguments(
            title="note",
            content="body",
            format="md",
            destination="C:\\Windows\\note.md",
        )
    with pytest.raises(ValidationError):
        SetReminderArguments(
            title="bad timezone",
            reminder_text="body",
            scheduled_at=FUTURE,
            timezone="Not/A_Timezone",
        )
    with pytest.raises(ValidationError, match="future"):
        SetReminderArguments(
            title="past reminder",
            reminder_text="body",
            scheduled_at=datetime.now(UTC) - timedelta(days=1),
            timezone="UTC",
        )


def test_registry_rejects_risk_mismatch_and_tool_timeout_overrides() -> None:
    with pytest.raises(ToolProtocolError, match="risk"):
        TOOL_REGISTRY.validate_request(
            make_request(
                "get_system_status",
                {},
                risk_level=RiskLevel.LOCAL_WRITE,
            )
        )
    definition = TOOL_REGISTRY.require("get_system_status")
    with pytest.raises(ToolProtocolError, match="timeout"):
        TOOL_REGISTRY.validate_request(
            make_request(
                "get_system_status",
                {},
                timeout_ms=definition.maximum_timeout_ms + 1,
            )
        )
    with pytest.raises(UnsupportedToolVersionError):
        TOOL_REGISTRY.validate_request(make_request("get_system_status", {}, tool_version="2.0"))


def test_client_capability_schema_and_registry_validation() -> None:
    capabilities = [
        ToolCapability.from_definition(definition, enabled=True) for definition in TOOL_REGISTRY
    ]
    event = ClientCapabilities(
        client_id=uuid4(),
        session_id=uuid4(),
        platform=ToolPlatform.WINDOWS,
        app_version=APP_VERSION,
        tools=capabilities,
        advertised_at=NOW,
    )

    TOOL_REGISTRY.validate_capabilities(event)
    assert event.type == "client.capabilities"
    assert len(event.tools) == 8
    model_tools = TOOL_REGISTRY.model_tool_definitions(event.tools)
    assert {item["function"]["name"] for item in model_tools} == TOOL_REGISTRY.names
    assert all(item["function"]["strict"] is True for item in model_tools)

    with pytest.raises(ValidationError, match="duplicate"):
        ClientCapabilities(
            client_id=uuid4(),
            session_id=uuid4(),
            platform=ToolPlatform.WINDOWS,
            app_version=APP_VERSION,
            tools=[capabilities[0], capabilities[0]],
            advertised_at=NOW,
        )
    with pytest.raises(ValidationError, match="Unsupported protocol version"):
        ClientCapabilities(
            protocol_version="2.0",
            client_id=uuid4(),
            session_id=uuid4(),
            platform=ToolPlatform.WINDOWS,
            app_version=APP_VERSION,
            tools=[],
            advertised_at=NOW,
        )


def test_capability_cannot_broaden_timeout_result_or_persistent_limits() -> None:
    search = TOOL_REGISTRY.require("search_files")
    capability = ToolCapability.from_definition(search, enabled=True)
    with pytest.raises(ToolProtocolError, match="timeout"):
        TOOL_REGISTRY.validate_capability(
            capability.model_copy(update={"maximum_timeout_ms": search.maximum_timeout_ms + 1})
        )
    with pytest.raises(ToolProtocolError, match="result limit"):
        TOOL_REGISTRY.validate_capability(
            capability.model_copy(
                update={"maximum_result_size_bytes": search.maximum_result_size_bytes + 1}
            )
        )
    with pytest.raises(ToolProtocolError, match="persistent"):
        TOOL_REGISTRY.validate_capability(
            capability.model_copy(update={"persistent_approval_supported": True})
        )
    with pytest.raises(ToolProtocolError, match="result limit"):
        TOOL_REGISTRY.validate_capability(
            capability.model_copy(update={"maximum_result_items": None})
        )


def tool_results() -> dict[str, Any]:
    return {
        "get_system_status": GetSystemStatusResult(
            operating_system="Windows 11",
            client_display_name="Desktop",
            cpu_usage_percent=20,
            ram_usage_percent=50,
            ram_total_bytes=16_000,
            ram_available_bytes=8_000,
            link_uptime_seconds=120,
            link_version=APP_VERSION,
        ),
        "get_active_window": GetActiveWindowResult(
            result_id=uuid4(),
            window_found=True,
            title="README — Visual Studio Code",
            process_name="Code.exe",
            process_id=1234,
            executable_basename="Code.exe",
            timestamp=NOW,
        ),
        "open_application": OpenApplicationResult(
            application_id="visual-studio-code", process_id=1234
        ),
        "open_folder": OpenFolderResult(
            result_id=uuid4(),
            path_ref="root-1:project",
            safe_path_summary="Nivelle project",
        ),
        "search_files": SearchFilesResult(
            result_id=uuid4(),
            root_id="project",
            query="README",
            items=[
                SearchFileItem(
                    path_ref="root-1:readme",
                    name="README.md",
                    relative_path="README.md",
                    type="file",
                    size_bytes=100,
                    modified_at=NOW,
                )
            ],
            truncated=False,
            original_size=1,
            returned_size=1,
            omitted_count=0,
        ),
        "read_text_file": ReadTextFileResult(
            result_id=uuid4(),
            path_ref="root-1:readme",
            content="untrusted file text",
            encoding="utf-8",
            encoding_uncertain=False,
            start_line=1,
            returned_lines=1,
            has_more=False,
            truncated=False,
            original_size=19,
            returned_size=19,
            omitted_count=0,
        ),
        "create_note": CreateNoteResult(
            note_id=uuid4(),
            title="Phase 3",
            format="md",
            path_ref="notes:phase-3",
            safe_path_summary="Nivelle Notes/Phase 3.md",
            size_bytes=10,
        ),
        "set_reminder": SetReminderResult(
            reminder_id=uuid4(),
            title="Phase 3",
            scheduled_at=NOW + timedelta(days=1),
            timezone="UTC",
        ),
    }


def test_all_result_schemas_validate_and_mark_untrusted_sources() -> None:
    for tool_name, result_model in tool_results().items():
        wrapper = make_result(tool_name, result_model.model_dump(mode="json"))
        validated = TOOL_REGISTRY.validate_result(wrapper)
        assert type(validated) is type(result_model)

    for tool_name in ("get_active_window", "open_folder", "search_files", "read_text_file"):
        marked = tool_results()[tool_name]
        assert marked.trusted is False
        assert marked.source_tool == tool_name
        assert isinstance(marked.result_id, UUID)


def test_tool_result_requires_consistent_terminal_success_or_failure() -> None:
    completed = make_result(
        "get_system_status", tool_results()["get_system_status"].model_dump(mode="json")
    )
    assert completed.status is ToolStatus.COMPLETED
    assert completed.trusted is False
    assert completed.source_tool == completed.tool_name

    with pytest.raises(ValidationError, match="source_tool"):
        ToolResult.model_validate(
            completed.model_dump(mode="json") | {"source_tool": "read_text_file"}
        )

    with pytest.raises(ValidationError, match="terminal"):
        ToolResult.model_validate(
            completed.model_dump(mode="json") | {"status": ToolStatus.RUNNING}
        )
    with pytest.raises(ValidationError, match="cannot contain an error"):
        ToolResult.model_validate(
            completed.model_dump(mode="json")
            | {
                "error_code": ToolErrorCode.EXECUTION_FAILED,
                "error_message": "not allowed on success",
            }
        )
    failed = ToolResult.model_validate(
        completed.model_dump(mode="json")
        | {
            "status": ToolStatus.FAILED,
            "result": None,
            "error_code": ToolErrorCode.EXECUTION_FAILED,
            "error_message": "application did not start",
            "retryable": True,
        }
    )
    assert failed.status is ToolStatus.FAILED
    assert TOOL_REGISTRY.validate_result(failed) is None


def test_result_truncation_and_registry_byte_limits_are_enforced() -> None:
    status = tool_results()["get_system_status"].model_dump(mode="json")
    with pytest.raises(ValidationError, match="original_size"):
        ToolResult.model_validate(
            make_result("get_system_status", status).model_dump(mode="json") | {"truncated": True}
        )

    oversized = make_result("get_system_status", {"junk": "x" * 70_000})
    with pytest.raises(ToolProtocolError, match="maximum"):
        TOOL_REGISTRY.validate_result(oversized)
    too_slow = make_result("get_system_status", status).model_copy(
        update={"duration_ms": TOOL_REGISTRY.require("get_system_status").maximum_timeout_ms + 1}
    )
    with pytest.raises(ToolProtocolError, match="duration"):
        TOOL_REGISTRY.validate_result(too_slow)


def test_approval_events_require_a_bound_decision() -> None:
    decision = ToolApprovalDecision(
        approval_id=uuid4(),
        mode=ApprovalMode.ALLOW_ONCE,
        decided_at=NOW,
        policy_version="1",
    )
    approved = ToolEvent(
        type=ToolEventType.APPROVED,
        status=ToolStatus.APPROVED,
        approval=decision,
        **event_fields(),
    )
    assert approved.approval is not None
    assert approved.approval.mode is ApprovalMode.ALLOW_ONCE

    with pytest.raises(ValidationError, match="requires an approval"):
        ToolEvent(
            type=ToolEventType.APPROVED,
            status=ToolStatus.APPROVED,
            **event_fields(),
        )
    with pytest.raises(ValidationError, match="decision=deny"):
        ToolEvent(
            type=ToolEventType.DENIED,
            status=ToolStatus.DENIED,
            approval=decision,
            error_code=ToolErrorCode.APPROVAL_DENIED,
            error_message="사용자가 거부했습니다.",
            **event_fields(),
        )
    with pytest.raises(ValidationError, match="exact target"):
        ToolApprovalDecision(
            approval_id=uuid4(),
            mode=ApprovalMode.ALLOW_ALWAYS_EXACT,
            decided_at=NOW,
            policy_version="1",
        )


def test_progress_event_is_correlated_bounded_and_running() -> None:
    progress = ToolEvent(
        type=ToolEventType.PROGRESS,
        status=ToolStatus.RUNNING,
        progress=ToolProgress(sequence=1, completed_units=4, total_units=10),
        **event_fields(),
    )

    assert progress.tool_call_id
    assert progress.request_id
    assert progress.progress is not None
    assert progress.progress.completed_units == 4
    with pytest.raises(ValidationError, match="cannot exceed"):
        ToolProgress(sequence=1, completed_units=11, total_units=10)
    with pytest.raises(ValidationError, match="requires status"):
        ToolEvent(
            type=ToolEventType.PROGRESS,
            status=ToolStatus.COMPLETED,
            progress=ToolProgress(sequence=1, completed_units=1, total_units=1),
            **event_fields(),
        )


def test_failure_event_requires_typed_error_details() -> None:
    with pytest.raises(ValidationError, match="requires error details"):
        ToolEvent(
            type=ToolEventType.FAILED,
            status=ToolStatus.FAILED,
            **event_fields(),
        )
    failed = ToolEvent(
        type=ToolEventType.FAILED,
        status=ToolStatus.FAILED,
        error_code=ToolErrorCode.EXECUTION_FAILED,
        error_message="safe failure summary",
        **event_fields(),
    )
    assert failed.error_code is ToolErrorCode.EXECUTION_FAILED


def test_legal_state_transitions_reject_terminal_replay() -> None:
    assert is_valid_tool_state_transition(ToolStatus.PROPOSED, ToolStatus.VALIDATED)
    assert is_valid_tool_state_transition(ToolStatus.VALIDATED, ToolStatus.QUEUED)
    assert not is_valid_tool_state_transition(ToolStatus.VALIDATED, ToolStatus.APPROVED)
    assert is_valid_tool_state_transition(ToolStatus.RUNNING, ToolStatus.COMPLETED)
    assert not is_valid_tool_state_transition(ToolStatus.QUEUED, ToolStatus.COMPLETED)

    validate_tool_state_transition(ToolStatus.RUNNING, ToolStatus.COMPLETED)
    with pytest.raises(InvalidToolStateTransitionError):
        validate_tool_state_transition(ToolStatus.COMPLETED, ToolStatus.RUNNING)
    with pytest.raises(InvalidToolStateTransitionError):
        validate_tool_state_transition(ToolStatus.COMPLETED, ToolStatus.COMPLETED)


def test_progress_is_the_only_non_transition_event() -> None:
    progress = ToolEvent(
        type=ToolEventType.PROGRESS,
        status=ToolStatus.RUNNING,
        progress=ToolProgress(sequence=2, completed_units=10),
        **event_fields(),
    )
    validate_tool_event_transition(ToolStatus.RUNNING, progress)

    completed = ToolEvent(
        type=ToolEventType.COMPLETED,
        status=ToolStatus.COMPLETED,
        **event_fields(),
    )
    with pytest.raises(InvalidToolStateTransitionError):
        validate_tool_event_transition(ToolStatus.COMPLETED, completed)


def test_persistent_approval_is_limited_to_exact_interactive_targets() -> None:
    persistent_tools = {
        definition.name for definition in TOOL_REGISTRY if definition.persistent_approval_supported
    }
    assert persistent_tools == {"open_application", "open_folder"}
    assert all(
        not definition.persistent_approval_supported
        for definition in TOOL_REGISTRY
        if definition.risk_level is RiskLevel.LOCAL_WRITE
    )
