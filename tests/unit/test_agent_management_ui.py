from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from nivelle_link import app as client_app_module
from nivelle_link.agent import (
    AgentError,
    AgentPolicy,
    ApprovalMode,
    LocalAgentPolicyEditor,
    PolicyStore,
)
from nivelle_link.app import NivelleLinkApplication
from nivelle_link.windows import AgentManagementWindow, ToolPolicyDialog
from PySide6.QtCore import QSettings


def _editor(tmp_path: Path) -> LocalAgentPolicyEditor:
    return LocalAgentPolicyEditor(PolicyStore(tmp_path / "agent-policy.json"))


def test_local_policy_editor_crud_uses_canonical_existing_targets(tmp_path: Path) -> None:
    editor = _editor(tmp_path)
    executable = tmp_path / "NivelleEditor.exe"
    executable.write_bytes(b"MZ")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    policy = editor.upsert_application(
        "editor",
        display_name="Nivelle Editor",
        executable_path=executable,
        enabled=True,
    )
    assert policy.applications["editor"].executable_path == executable.resolve()

    with pytest.raises(ValueError, match="already registered"):
        editor.upsert_application(
            "editor",
            display_name="Duplicate",
            executable_path=executable,
            enabled=True,
        )

    policy = editor.upsert_application(
        "editor-next",
        previous_application_id="editor",
        display_name="Nivelle Editor",
        executable_path=executable,
        enabled=False,
    )
    assert "editor" not in policy.applications
    assert policy.applications["editor-next"].enabled is False

    policy = editor.upsert_root(
        "projects",
        display_name="Projects",
        path=workspace,
        allow_search=True,
        allow_read=True,
        allow_open_folder=False,
    )
    assert policy.filesystem_roots["projects"].path == workspace.resolve()
    assert policy.filesystem_roots["projects"].allow_search is True

    policy = editor.remove_application("editor-next")
    policy = editor.remove_root("projects")
    assert not policy.applications
    assert not policy.filesystem_roots


def test_local_policy_editor_rejects_dangerous_or_missing_executables(
    tmp_path: Path,
) -> None:
    editor = _editor(tmp_path)
    dangerous = tmp_path / "powershell.exe"
    dangerous.write_bytes(b"MZ")

    with pytest.raises(AgentError, match="cannot be launched"):
        editor.upsert_application(
            "shell",
            display_name="Shell",
            executable_path=dangerous,
            enabled=True,
        )
    with pytest.raises(AgentError, match="unavailable"):
        editor.upsert_application(
            "missing",
            display_name="Missing",
            executable_path=tmp_path / "missing.exe",
            enabled=True,
        )


def test_tool_edits_keep_no_approval_and_local_write_rules(tmp_path: Path) -> None:
    editor = _editor(tmp_path)
    editor.update_tool(
        "get_system_status",
        enabled=True,
        approval_mode=ApprovalMode.NOT_REQUIRED,
        timeout_ms=5_000,
    )

    with pytest.raises(ValueError, match="Only get_system_status"):
        editor.update_tool(
            "read_text_file",
            enabled=True,
            approval_mode=ApprovalMode.NOT_REQUIRED,
            timeout_ms=5_000,
        )
    with pytest.raises(ValueError, match="LOCAL_WRITE"):
        editor.update_tool(
            "create_note",
            enabled=True,
            approval_mode=ApprovalMode.ALLOW_SESSION,
            timeout_ms=5_000,
        )
    with pytest.raises(ValueError, match="cannot exceed"):
        editor.update_tool(
            "get_system_status",
            enabled=True,
            approval_mode=ApprovalMode.NOT_REQUIRED,
            timeout_ms=10_001,
        )


def test_every_local_policy_edit_changes_policy_version(tmp_path: Path) -> None:
    editor = _editor(tmp_path)
    first = editor.set_agent_enabled(True)
    second = editor.set_path_policies(
        allow_hidden_files=True, allow_network_paths=False
    )
    assert first.policy_version != AgentPolicy().policy_version
    assert second.policy_version != first.policy_version


def test_invalid_existing_policy_fails_closed_without_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "agent-policy.json"
    original = b'{"agent_enabled": true, "unknown_privilege": true}'
    path.write_bytes(original)

    with pytest.raises(ValueError):
        LocalAgentPolicyEditor(PolicyStore(path)).set_agent_enabled(False)

    assert path.read_bytes() == original


def test_management_window_exposes_edit_controls_and_safe_modes(qtbot: Any) -> None:
    window = AgentManagementWindow()
    qtbot.addWidget(window)
    snapshot = {
        "enabled": True,
        "allow_hidden_files": True,
        "allow_network_paths": False,
        "tools": [
            {
                "name": "create_note",
                "enabled": True,
                "risk_level": "LOCAL_WRITE",
                "approval_mode": "allow_once",
                "available": True,
                "timeout_ms": 10_000,
            }
        ],
        "applications": [],
        "roots": [],
        "audit": [],
    }
    window.set_snapshot(snapshot)

    dialog = ToolPolicyDialog(snapshot["tools"][0], window)
    qtbot.addWidget(dialog)
    modes = [dialog.approval.itemData(index) for index in range(dialog.approval.count())]
    assert modes == ["allow_once"]
    assert window.allow_hidden_files.isChecked()
    assert not window.allow_network_paths.isChecked()
    assert window.add_application_button.isEnabled()
    assert window.add_root_button.isEnabled()


def test_management_window_emits_local_path_policy_and_persists_geometry(
    qtbot: Any,
) -> None:
    settings = QSettings("Nivelle", "NivelleLink")
    settings.remove("agent_management/geometry")
    window = AgentManagementWindow()
    qtbot.addWidget(window)
    decisions: list[tuple[bool, bool]] = []
    window.path_policy_changed.connect(
        lambda hidden, network: decisions.append((hidden, network))
    )
    window.allow_hidden_files.setChecked(True)
    window.allow_network_paths.setChecked(False)
    window.save_path_policy_button.click()
    window.close()

    assert decisions == [(True, False)]
    assert settings.value("agent_management/geometry") is not None
    settings.remove("agent_management/geometry")


def test_application_edits_policy_while_agent_channel_is_offline(
    qtbot: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client_app_module, "client_data_dir", lambda: tmp_path)
    management = AgentManagementWindow()
    qtbot.addWidget(management)
    application = object.__new__(NivelleLinkApplication)
    application.window = SimpleNamespace(agent_window=management)
    application.client = SimpleNamespace(agent_connected=False)
    application._agent_controller = None
    application._agent_tasks = set()

    application._update_agent_tool_policy(
        "read_text_file", True, "allow_once", 12_000
    )
    executable = tmp_path / "NivelleEditor.exe"
    executable.write_bytes(b"MZ")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    application._upsert_agent_application(
        "", "editor", "Nivelle Editor", str(executable), True
    )
    application._upsert_agent_root(
        "", "workspace", "Workspace", str(workspace), True, True, False
    )

    policy = PolicyStore(tmp_path / "agent-policy.json").load()
    assert "read_text_file" in policy.enabled_tools
    assert policy.tool_timeouts_ms["read_text_file"] == 12_000
    assert policy.applications["editor"].executable_path == executable.resolve()
    assert policy.filesystem_roots["workspace"].allow_read is True
    assert "저장했습니다" in management.message.text()
