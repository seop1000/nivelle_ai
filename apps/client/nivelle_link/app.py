import asyncio
import math
import os
import sys
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import qasync  # type: ignore[import-untyped]
from keyring.errors import KeyringError
from nivelle_protocol.settings import ConnectionProfile
from nivelle_protocol.tools import TOOL_REGISTRY
from nivelle_protocol.version import APP_VERSION, PROTOCOL_VERSION, protocol_compatibility
from PySide6.QtWidgets import QApplication, QDialog

from .agent import (
    AgentError,
    AgentPolicy,
    ApprovalManager,
    ApprovalMode,
    AuditLog,
    LocalAgentPolicyEditor,
    PolicyStore,
)
from .agent_controller import AgentController
from .network import (
    ConnectionManager,
    ConnectionState,
    NetworkClient,
    ServerIdentityMismatchError,
)
from .storage import (
    client_data_dir,
    load_connection_profiles,
    load_token_for_profile,
    load_token_for_server,
    resolve_connection_profiles,
    save_connection_profiles,
    save_token_for_profile,
    save_token_for_server,
)
from .windows import ConnectionDialog, MainChatWindow, PairingDialog

AUTHENTICATION_FAILURES_BEFORE_PAIRING = 2


class NivelleLinkApplication:
    def __init__(self, *, gateway_endpoint: str | None = None) -> None:
        self.qt = QApplication.instance() or QApplication(sys.argv)
        if gateway_endpoint or os.environ.get("NIVELLE_GATEWAY_ENDPOINT"):
            profiles, resolved_gateway = resolve_connection_profiles(
                cli_endpoint=gateway_endpoint
            )
        else:
            profiles, resolved_gateway = load_connection_profiles(), None
        self.connections = ConnectionManager(profiles)
        if resolved_gateway is not None:
            print(resolved_gateway.diagnostic())
        self.client = NetworkClient(self.connections)
        self.client.chat_disconnect_callback = self._chat_connection_lost
        self.client.agent_disconnect_callback = self._agent_connection_lost
        self.window = MainChatWindow()
        self.window.send_requested.connect(self._schedule_send)
        self.window.reconnect_requested.connect(self._schedule_connection_settings)
        self.window.disconnect_requested.connect(self._disconnect_manually)
        self.window.admin_requested.connect(self._admin_opened)
        self.window.memory_requested.connect(self._memory_opened)
        self.window.history_requested.connect(self._history_opened)
        self.window.persona_requested.connect(self._persona_opened)
        self.window.agent_requested.connect(self._agent_opened)
        self.window.tool_decision_requested.connect(self._schedule_tool_decision)
        self.window.new_conversation_requested.connect(self._new_conversation)
        self._startup_task: asyncio.Task[None] | None = None
        self._connection_task: asyncio.Task[None] | None = None
        self._connection_lock = asyncio.Lock()
        self._send_task: asyncio.Task[None] | None = None
        self._active_conversation_id: str | None = None
        self._active_server_key: str | None = None
        self._conversation_titles: dict[str, str] = {}
        self._admin_console_connected = False
        self._admin_tasks: set[asyncio.Task[None]] = set()
        self._audio_job_id: str | None = None
        self._audio_generation = 0
        self._memory_window_connected = False
        self._memory_tasks: set[asyncio.Task[None]] = set()
        self._history_window_connected = False
        self._history_tasks: set[asyncio.Task[None]] = set()
        self._history_refresh_task: asyncio.Task[None] | None = None
        self._history_refresh_generation = 0
        self._conversation_load_task: asyncio.Task[None] | None = None
        self._conversation_load_generation = 0
        self._persona_window_connected = False
        self._persona_tasks: set[asyncio.Task[None]] = set()
        self._agent_window_connected = False
        self._agent_tasks: set[asyncio.Task[None]] = set()
        self._agent_controller: AgentController | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        self._auto_reconnect_task: asyncio.Task[None] | None = None
        self._chat_close_task: asyncio.Task[None] | None = None
        self._reconnect_attempts = 0
        self._status_failures = 0
        self._authentication_failures = 0
        self._last_server_status: dict[str, Any] = {}
        self._protocol_compatible = True
        self._protocol_warning: str | None = None
        self._shutdown_started = False
        self.qt.aboutToQuit.connect(self._cancel_background_tasks)

    async def start(self) -> None:
        self.window.show()
        await self._connect_or_configure()

    def _cancel_background_tasks(self) -> None:
        self._shutdown_started = True
        for task in self._application_tasks():
            if task is not None and not task.done():
                task.cancel()
        self._schedule_chat_close()

    def _application_tasks(self) -> set[asyncio.Task[None]]:
        tasks = {
            task
            for task in (
                self._startup_task,
                self._connection_task,
                self._send_task,
                self._history_refresh_task,
                self._conversation_load_task,
                self._monitor_task,
                self._auto_reconnect_task,
            )
            if task is not None
        }
        tasks.update(self._admin_tasks)
        tasks.update(self._memory_tasks)
        tasks.update(self._history_tasks)
        tasks.update(self._persona_tasks)
        tasks.update(self._agent_tasks)
        return tasks

    async def shutdown(self) -> None:
        """Stop every application task and close both authoritative sockets."""
        self._shutdown_started = True
        current = asyncio.current_task()
        tasks = {task for task in self._application_tasks() if task is not current}
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        self._end_agent_session()
        await self.connections.shutdown()
        client_shutdown = getattr(self.client, "shutdown", None)
        if callable(client_shutdown):
            await client_shutdown()
            return
        close_task = self._chat_close_task
        if close_task is None or close_task.done():
            close_task = asyncio.create_task(self._close_network_connections())
            self._chat_close_task = close_task
            close_task.add_done_callback(self._chat_close_finished)
        await asyncio.gather(close_task, return_exceptions=True)

    def _schedule_chat_close(self) -> None:
        if self._chat_close_task is not None and not self._chat_close_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._end_agent_session()
        task = loop.create_task(self._close_network_connections())
        self._chat_close_task = task
        task.add_done_callback(self._chat_close_finished)

    def _chat_close_finished(self, task: asyncio.Task[None]) -> None:
        if task is self._chat_close_task:
            self._chat_close_task = None
        if not task.cancelled():
            task.exception()

    async def _close_network_connections(self) -> None:
        await asyncio.gather(
            self.client.close_agent_connection(),
            self.client.close_chat_connection(),
        )

    def _schedule_send(self, text: str) -> None:
        if self._send_task is not None and not self._send_task.done():
            self.window.restore_input(text)
            self.window.show_error("답변을 생성 중입니다. 완료된 뒤 다시 보내세요.")
            return
        if self._conversation_load_task is not None and not self._conversation_load_task.done():
            self.window.restore_input(text)
            self.window.show_error("대화를 불러오는 중입니다. 완료된 뒤 메시지를 보내세요.")
            return
        if not self.connections.active:
            self.window.restore_input(text)
            self.window.show_error("먼저 서버에 연결하세요.")
            self._schedule_connection_settings()
            return
        if not self.client.token:
            self.window.restore_input(text)
            self.window.show_error("서버 페어링이 필요합니다.")
            return
        if not self._protocol_compatible:
            self.window.restore_input(text)
            self.window.show_error(
                self._protocol_warning or "서버와 클라이언트 프로토콜이 호환되지 않습니다."
            )
            return
        self.window.set_generating(True)
        self._send_task = asyncio.create_task(self.send(text))
        self._send_task.add_done_callback(self._send_task_finished)

    def _send_task_finished(self, task: asyncio.Task[None]) -> None:
        if task is not self._send_task:
            return
        self._send_task = None
        self.window.set_generating(False)
        if not task.cancelled():
            error = task.exception()
            if error:
                self.window.finish_assistant_message()
                self.window.show_error(f"대화 처리 중 오류가 발생했습니다: {error}")

    def _history_opened(self) -> None:
        window = self.window.history_window
        if window is None:
            return
        if not self._history_window_connected:
            window.refresh_requested.connect(self._schedule_history_refresh)
            window.conversation_requested.connect(self._schedule_conversation_load)
            window.new_conversation_requested.connect(self._new_conversation)
            self._history_window_connected = True
        self._schedule_history_refresh()

    def _persona_opened(self) -> None:
        window = self.window.persona_window
        if window is None:
            return
        setter = getattr(window, "set_online", None)
        if callable(setter):
            setter(self.connections.active is not None and bool(self.client.token))
        if not self._persona_window_connected:
            window.refresh_requested.connect(self._schedule_persona_refresh)
            window.save_requested.connect(self._schedule_persona_save)
            self._persona_window_connected = True
        self._schedule_persona_refresh()

    def _agent_opened(self) -> None:
        window = self.window.agent_window
        if window is None:
            return
        if not self._agent_window_connected:
            window.refresh_requested.connect(self._refresh_agent_snapshot)
            window.enabled_changed.connect(self._set_agent_enabled)
            window.revoke_requested.connect(self._revoke_agent_approval)
            window.tool_policy_changed.connect(self._update_agent_tool_policy)
            window.application_upsert_requested.connect(
                self._upsert_agent_application
            )
            window.application_remove_requested.connect(
                self._remove_agent_application
            )
            window.root_upsert_requested.connect(self._upsert_agent_root)
            window.root_remove_requested.connect(self._remove_agent_root)
            window.path_policy_changed.connect(self._update_agent_path_policy)
            self._agent_window_connected = True
        self._refresh_agent_snapshot()

    def _schedule_tool_decision(self, tool_call_id: str, decision: str) -> None:
        controller = self._agent_controller
        if controller is None:
            self.window.update_tool_status(
                tool_call_id,
                "client_disconnected",
                "Agent 연결이 종료되었습니다.",
            )
            return
        self._start_agent_task(controller.decide(tool_call_id, decision))

    def _start_agent_task(self, coroutine: Any) -> None:
        task = asyncio.create_task(coroutine)
        self._agent_tasks.add(task)
        task.add_done_callback(self._agent_task_finished)

    def _agent_task_finished(self, task: asyncio.Task[None]) -> None:
        self._agent_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error and self.window.agent_window is not None:
            self.window.agent_window.show_error(f"Agent 작업을 처리하지 못했습니다: {error}")
        self._refresh_agent_snapshot()

    def _set_agent_enabled(self, enabled: bool) -> None:
        try:
            self._agent_policy_editor().set_agent_enabled(enabled)
        except (AgentError, OSError, ValueError) as exc:
            self._show_agent_policy_error(exc)
            return
        self._agent_policy_changed("Agent 사용 설정을 저장했습니다.")

    @staticmethod
    def _agent_policy_editor() -> LocalAgentPolicyEditor:
        return LocalAgentPolicyEditor(
            PolicyStore(client_data_dir() / "agent-policy.json")
        )

    def _update_agent_tool_policy(
        self, tool_name: str, enabled: bool, approval_mode: str, timeout_ms: int
    ) -> None:
        try:
            self._agent_policy_editor().update_tool(
                tool_name,
                enabled=enabled,
                approval_mode=ApprovalMode(approval_mode),
                timeout_ms=timeout_ms,
            )
        except (AgentError, OSError, ValueError) as exc:
            self._show_agent_policy_error(exc)
            return
        self._agent_policy_changed(f"{tool_name} 도구 정책을 저장했습니다.")

    def _upsert_agent_application(
        self,
        previous_application_id: str,
        application_id: str,
        display_name: str,
        executable_path: str,
        enabled: bool,
    ) -> None:
        try:
            self._agent_policy_editor().upsert_application(
                application_id,
                previous_application_id=previous_application_id or None,
                display_name=display_name,
                executable_path=executable_path,
                enabled=enabled,
            )
        except (AgentError, OSError, ValueError) as exc:
            self._show_agent_policy_error(exc)
            return
        self._agent_policy_changed("애플리케이션 등록을 저장했습니다.")

    def _remove_agent_application(self, application_id: str) -> None:
        try:
            self._agent_policy_editor().remove_application(application_id)
        except (AgentError, OSError, ValueError) as exc:
            self._show_agent_policy_error(exc)
            return
        self._agent_policy_changed("애플리케이션 등록을 제거했습니다.")

    def _upsert_agent_root(
        self,
        previous_root_id: str,
        root_id: str,
        display_name: str,
        path: str,
        allow_search: bool,
        allow_read: bool,
        allow_open_folder: bool,
    ) -> None:
        try:
            self._agent_policy_editor().upsert_root(
                root_id,
                previous_root_id=previous_root_id or None,
                display_name=display_name,
                path=path,
                allow_search=allow_search,
                allow_read=allow_read,
                allow_open_folder=allow_open_folder,
            )
        except (AgentError, OSError, ValueError) as exc:
            self._show_agent_policy_error(exc)
            return
        self._agent_policy_changed("파일시스템 루트를 저장했습니다.")

    def _remove_agent_root(self, root_id: str) -> None:
        try:
            self._agent_policy_editor().remove_root(root_id)
        except (AgentError, OSError, ValueError) as exc:
            self._show_agent_policy_error(exc)
            return
        self._agent_policy_changed("파일시스템 루트를 제거했습니다.")

    def _update_agent_path_policy(
        self, allow_hidden_files: bool, allow_network_paths: bool
    ) -> None:
        try:
            self._agent_policy_editor().set_path_policies(
                allow_hidden_files=allow_hidden_files,
                allow_network_paths=allow_network_paths,
            )
        except (AgentError, OSError, ValueError) as exc:
            self._show_agent_policy_error(exc)
            return
        self._agent_policy_changed("공통 경로 정책을 저장했습니다.")

    def _show_agent_policy_error(self, error: Exception) -> None:
        window = self.window.agent_window
        if window is not None:
            safe_message = (
                error.safe_message if isinstance(error, AgentError) else str(error)
            )
            window.show_error(f"로컬 정책을 저장하지 못했습니다: {safe_message}")
        self._refresh_agent_snapshot()

    def _agent_policy_changed(self, message: str) -> None:
        controller = self._agent_controller
        if controller is not None and self.client.agent_connected:
            self._start_agent_task(
                controller.advertise(
                    app_version=APP_VERSION,
                    protocol_version=PROTOCOL_VERSION,
                )
            )
        self._refresh_agent_snapshot()
        if self.window.agent_window is not None:
            self.window.agent_window.show_message(message)

    def _revoke_agent_approval(self, approval_id: str) -> None:
        controller = self._agent_controller
        revoked = (
            controller.revoke(approval_id)
            if controller is not None
            else ApprovalManager(client_data_dir() / "agent-approvals.json").revoke(
                approval_id
            )
        )
        if self.window.agent_window is not None:
            self.window.agent_window.show_message(
                "승인을 철회했습니다." if revoked else "이미 없거나 철회된 승인입니다."
            )
        self._refresh_agent_snapshot()

    def _refresh_agent_snapshot(self) -> None:
        window = self.window.agent_window
        if window is None:
            return
        controller = self._agent_controller
        if controller is not None:
            profile = self.connections.active
            connected_core = (
                f"{profile.host}:{profile.port}" if profile is not None else None
            )
            try:
                snapshot = controller.snapshot(connected_core=connected_core)
                window.set_snapshot(self._with_local_path_policy(snapshot))
                return
            except (OSError, ValueError) as exc:
                window.show_error(f"로컬 Agent 정책을 읽지 못했습니다: {exc}")
                return
        window.set_snapshot(self._offline_agent_snapshot())

    @staticmethod
    def _with_local_path_policy(snapshot: dict[str, Any]) -> dict[str, Any]:
        try:
            policy = PolicyStore(client_data_dir() / "agent-policy.json").load()
        except (OSError, ValueError):
            policy = AgentPolicy()
        tools = snapshot.get("tools")
        configured_tools = [
            {**item, "enabled": str(item.get("name") or "") in policy.enabled_tools}
            for item in tools
            if isinstance(item, dict)
        ] if isinstance(tools, list) else []
        return {
            **snapshot,
            "tools": configured_tools,
            "allow_hidden_files": policy.allow_hidden_files,
            "allow_network_paths": policy.allow_network_paths,
        }

    @staticmethod
    def _offline_agent_snapshot() -> dict[str, Any]:
        root = client_data_dir()
        try:
            policy = PolicyStore(root / "agent-policy.json").load()
        except (OSError, ValueError):
            policy = AgentPolicy()
        try:
            approvals = ApprovalManager(root / "agent-approvals.json").list_active(
                policy
            )
        except (OSError, ValueError):
            approvals = []
        try:
            audit = AuditLog(root / "agent-audit.json").list_recent()
        except (OSError, ValueError):
            audit = []
        return {
            "enabled": policy.agent_enabled,
            "connected_core": None,
            "client_id": None,
            "session_id": None,
            "enabled_tool_count": len(policy.enabled_tools) if policy.agent_enabled else 0,
            "pending_approval_count": 0,
            "recent_failure_count": sum(item.status != "completed" for item in audit[-20:]),
            "allow_hidden_files": policy.allow_hidden_files,
            "allow_network_paths": policy.allow_network_paths,
            "tools": [
                {
                    "name": definition.name,
                    "enabled": definition.name in policy.enabled_tools,
                    "risk_level": definition.risk_level.value,
                    "approval_mode": policy.approval_defaults.get(
                        definition.name, definition.default_approval_mode
                    ).value,
                    "available": True,
                    "timeout_ms": policy.tool_timeouts_ms.get(
                        definition.name, definition.default_timeout_ms
                    ),
                }
                for definition in TOOL_REGISTRY
            ],
            "applications": [
                {
                    "application_id": key,
                    "display_name": value.display_name,
                    "executable_path": str(value.executable_path),
                    "enabled": value.enabled,
                    "persistent_approval": (
                        "open_application" in policy.persistent_approval_tools
                    ),
                }
                for key, value in sorted(policy.applications.items())
            ],
            "roots": [
                {
                    "root_id": key,
                    "display_name": value.display_name,
                    "path": str(value.path),
                    "allow_search": value.allow_search,
                    "allow_read": value.allow_read,
                    "allow_open": value.allow_open_folder,
                }
                for key, value in sorted(policy.filesystem_roots.items())
            ],
            "approvals": [
                {
                    "approval_id": value.approval_id,
                    "tool_name": value.tool_name,
                    "scope": value.exact_target,
                    "mode": value.mode.value,
                    "created_at": value.created_at.isoformat(),
                    "last_used_at": (
                        value.last_used_at.isoformat() if value.last_used_at else None
                    ),
                }
                for value in approvals
            ],
            "audit": [
                {
                    "created_at": value.completed_at.isoformat(),
                    "tool_name": value.tool_name,
                    "status": value.status,
                    "target_summary": value.target_summary,
                    "duration_ms": value.duration_ms,
                    "error_code": value.error_code,
                }
                for value in audit[-100:]
            ],
        }

    async def _start_agent_session(self, profile: ConnectionProfile) -> None:
        client_id = self.client.client_id
        if not client_id:
            raise RuntimeError("인증된 Link 클라이언트 ID를 확인하지 못했습니다.")
        # Detach and close the old runtime before closing its socket. Any old
        # handler that resumes during reconnect then sees a closed controller
        # and cannot send through the replacement socket.
        self._end_agent_session()
        await self.client.close_agent_connection()
        controller = AgentController(
            data_directory=client_data_dir(),
            client_id=client_id,
            session_id=str(uuid4()),
            client_display_name="Nivelle Link",
            link_version=APP_VERSION,
            send_event=self.client.send_agent_event,
            show_approval=self.window.show_tool_approval,
            update_status=self.window.update_tool_status,
        )
        self._agent_controller = controller
        self.client.agent_event_callback = controller.handle_server_event
        try:
            await self.client.ensure_agent_connection()
            await controller.advertise(
                app_version=APP_VERSION,
                protocol_version=PROTOCOL_VERSION,
            )
        except BaseException:
            if self._agent_controller is controller:
                self._end_agent_session()
                await self.client.close_agent_connection()
            raise
        self._refresh_agent_snapshot()

    def _end_agent_session(self) -> None:
        controller = self._agent_controller
        self._agent_controller = None
        self.client.agent_event_callback = None
        if controller is not None:
            controller.close()
        self._refresh_agent_snapshot()

    def _agent_connection_lost(self) -> None:
        self._end_agent_session()
        if self.window.agent_window is not None:
            self.window.agent_window.show_error(
                "Agent 채널 연결이 끊겼습니다. 채팅 연결을 유지한 채 다시 연결을 시도합니다."
            )

    def _track_history_task(self, task: asyncio.Task[None]) -> None:
        self._history_tasks.add(task)
        task.add_done_callback(self._history_task_finished)

    def _history_task_finished(self, task: asyncio.Task[None]) -> None:
        self._history_tasks.discard(task)
        was_current = task is self._history_refresh_task or task is self._conversation_load_task
        if task is self._history_refresh_task:
            self._history_refresh_task = None
        if task is self._conversation_load_task:
            self._conversation_load_task = None
        if task.cancelled():
            return
        error = task.exception()
        if error and was_current and self.window.history_window is not None:
            self.window.history_window.show_loading(False)
            self.window.history_window.show_error(f"대화 기록 작업 중 오류가 발생했습니다: {error}")

    def _cancel_conversation_load(self) -> None:
        self._conversation_load_generation += 1
        task = self._conversation_load_task
        self._conversation_load_task = None
        if task is not None and not task.done():
            task.cancel()
        if self.window.history_window is not None:
            self.window.history_window.show_loading(False)

    def _cancel_history_operations(self) -> None:
        self._cancel_conversation_load()
        self._history_refresh_generation += 1
        task = self._history_refresh_task
        self._history_refresh_task = None
        if task is not None and not task.done():
            task.cancel()
        if self.window.history_window is not None:
            self.window.history_window.show_loading(False)

    def _start_persona_task(self, coroutine: Any) -> None:
        task = asyncio.create_task(coroutine)
        self._persona_tasks.add(task)
        task.add_done_callback(self._persona_task_finished)

    def _persona_task_finished(self, task: asyncio.Task[None]) -> None:
        self._persona_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error and self.window.persona_window is not None:
            self.window.persona_window.show_loading(False)
            self.window.persona_window.show_error(f"성격 설정 작업 중 오류가 발생했습니다: {error}")

    def _schedule_history_refresh(self) -> None:
        self._history_refresh_generation += 1
        generation = self._history_refresh_generation
        previous = self._history_refresh_task
        if previous is not None and not previous.done():
            previous.cancel()
        task = asyncio.create_task(self._refresh_conversations(generation=generation))
        self._history_refresh_task = task
        self._track_history_task(task)

    def _schedule_conversation_load(self, conversation_id: str) -> None:
        if self._send_task is not None and not self._send_task.done():
            if self.window.history_window is not None:
                self.window.history_window.show_error(
                    "답변을 생성 중에는 다른 대화를 열 수 없습니다."
                )
            return
        self._conversation_load_generation += 1
        generation = self._conversation_load_generation
        previous = self._conversation_load_task
        if previous is not None and not previous.done():
            previous.cancel()
        task = asyncio.create_task(self._load_conversation(conversation_id, generation=generation))
        self._conversation_load_task = task
        self._track_history_task(task)

    def _schedule_persona_refresh(self) -> None:
        self._start_persona_task(self._refresh_persona())

    def _schedule_persona_save(self, value: object) -> None:
        if not isinstance(value, dict):
            if self.window.persona_window is not None:
                self.window.persona_window.show_error("저장할 성격 설정 형식이 올바르지 않습니다.")
            return
        self._start_persona_task(self._save_persona(value))

    def _new_conversation(self) -> None:
        if self._send_task is not None and not self._send_task.done():
            return
        self._cancel_conversation_load()
        self._active_conversation_id = None
        self.window.clear_conversation()
        context_setter = getattr(self.window, "set_used_memories", None)
        if callable(context_setter):
            context_setter([])
        metrics_setter = getattr(self.window, "set_generation_metrics", None)
        if callable(metrics_setter):
            metrics_setter({})
        if self.window.history_window is not None:
            self.window.history_window.set_preview("새 대화", [])
            self.window.history_window.show_message("새 대화를 시작합니다.")
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()

    def _admin_opened(self) -> None:
        console = self.window.console
        if console is None:
            return
        setter = getattr(console, "set_online", None)
        if callable(setter):
            setter(self.connections.active is not None and bool(self.client.token))
        if not self._admin_console_connected:
            console.refresh_requested.connect(self._schedule_admin_refresh)
            console.save_requested.connect(self._schedule_admin_save)
            console.rollback_requested.connect(self._schedule_admin_rollback)
            console.pairing_code_requested.connect(self._schedule_pairing_code)
            console.audio_page.file_selected.connect(self._schedule_audio_analysis)
            console.audio_page.cancellation_requested.connect(
                self._schedule_audio_cancellation
            )
            self._admin_console_connected = True
        self._schedule_admin_refresh()

    def _start_admin_task(self, coroutine: Any) -> None:
        task = asyncio.create_task(coroutine)
        self._admin_tasks.add(task)
        task.add_done_callback(self._admin_task_finished)

    def _admin_task_finished(self, task: asyncio.Task[None]) -> None:
        self._admin_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error and self.window.console is not None:
            self.window.console.show_loading(False)
            self.window.console.show_error(f"서버 관리 작업 중 오류가 발생했습니다: {error}")

    def _memory_opened(self) -> None:
        window = self.window.memory_window
        if window is None:
            return
        setter = getattr(window, "set_online", None)
        if callable(setter):
            setter(self.connections.active is not None and bool(self.client.token))
        if not self._memory_window_connected:
            window.refresh_requested.connect(self._schedule_memory_refresh)
            window.search_requested.connect(self._schedule_memory_search)
            window.create_requested.connect(self._schedule_memory_create)
            window.update_requested.connect(self._schedule_memory_update)
            window.delete_requested.connect(self._schedule_memory_delete)
            self._memory_window_connected = True
        self._schedule_memory_refresh()

    def _start_memory_task(self, coroutine: Any) -> None:
        task = asyncio.create_task(coroutine)
        self._memory_tasks.add(task)
        task.add_done_callback(self._memory_task_finished)

    def _memory_task_finished(self, task: asyncio.Task[None]) -> None:
        self._memory_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error and self.window.memory_window is not None:
            self.window.memory_window.show_loading(False)
            self.window.memory_window.show_error(f"장기 기억 작업 중 오류가 발생했습니다: {error}")

    def _schedule_memory_refresh(self) -> None:
        self._start_memory_task(self._refresh_memories())

    def _schedule_memory_search(self, query: str) -> None:
        self._start_memory_task(self._refresh_memories(query))

    def _schedule_memory_create(self, value: object) -> None:
        if not isinstance(value, dict):
            if self.window.memory_window is not None:
                self.window.memory_window.show_error("저장할 기억 형식이 올바르지 않습니다.")
            return
        self._start_memory_task(self._create_memory(value))

    def _schedule_memory_update(self, memory_id: str, value: object) -> None:
        if not isinstance(value, dict):
            if self.window.memory_window is not None:
                self.window.memory_window.show_error("수정할 기억 형식이 올바르지 않습니다.")
            return
        self._start_memory_task(self._update_memory(memory_id, value))

    def _schedule_memory_delete(self, memory_id: str) -> None:
        self._start_memory_task(self._delete_memory(memory_id))

    def _schedule_admin_refresh(self) -> None:
        self._start_admin_task(self._refresh_admin())

    def _schedule_admin_save(self, section: str, value: object) -> None:
        if not isinstance(value, dict):
            if self.window.console is not None:
                self.window.console.show_error("저장할 설정 형식이 올바르지 않습니다.")
            return
        self._start_admin_task(self._save_admin(section, value))

    def _schedule_admin_rollback(self, revision_id: int) -> None:
        self._start_admin_task(self._rollback_admin(revision_id))

    def _schedule_pairing_code(self) -> None:
        self._start_admin_task(self._create_pairing_code())

    def _schedule_audio_analysis(self, path: str) -> None:
        self._audio_generation += 1
        self._start_admin_task(self._analyze_audio(Path(path), self._audio_generation))

    def _schedule_audio_cancellation(self) -> None:
        if self._audio_job_id is not None:
            self._start_admin_task(self._cancel_audio_analysis(self._audio_job_id))

    async def _cancel_audio_analysis(self, job_id: str) -> None:
        console = self.window.console
        if console is None:
            return
        try:
            value = await self.client.delete(f"/api/v1/audio-analysis/jobs/{job_id}")
            if isinstance(value, dict):
                console.audio_page.set_job_progress(
                    str(value.get("status") or "cancelling"),
                    float(value.get("progress") or 0.0),
                    str(value.get("stage") or "cancelling"),
                )
        except httpx.HTTPStatusError as exc:
            console.audio_page.set_error(self._admin_http_error(exc))
        except (httpx.HTTPError, OSError, TypeError, ValueError) as exc:
            console.audio_page.set_error(f"오디오 분석을 취소하지 못했습니다: {exc}")

    async def _analyze_audio(self, path: Path, generation: int) -> None:
        console = self.window.console
        if console is None:
            return
        page = console.audio_page
        if not self.connections.active or not self.client.token:
            page.set_error("오디오 분석에는 Core 연결과 관리자 인증이 필요합니다.")
            return
        try:
            if not path.is_file():
                raise ValueError("선택한 오디오 파일을 찾을 수 없습니다.")
            previous_job = self._audio_job_id
            if previous_job is not None:
                try:
                    await self.client.delete(
                        f"/api/v1/audio-analysis/jobs/{previous_job}"
                    )
                except httpx.HTTPError:
                    pass
            created = await self.client.upload_audio_file(
                path, progress=page.set_upload_progress
            )
            if not isinstance(created, dict):
                raise TypeError("Core가 올바르지 않은 오디오 분석 응답을 반환했습니다.")
            job_id = str(created.get("job_id") or "")
            if not job_id:
                raise TypeError("Core 오디오 분석 응답에 작업 ID가 없습니다.")
            self._audio_job_id = job_id
            value = created
            while True:
                if generation != self._audio_generation:
                    try:
                        await self.client.delete(
                            f"/api/v1/audio-analysis/jobs/{job_id}"
                        )
                    except httpx.HTTPError:
                        pass
                    return
                status_value = str(value.get("status") or "failed")
                progress_value = float(value.get("progress") or 0.0)
                stage = str(value.get("stage") or status_value)
                page.set_job_progress(status_value, progress_value, stage)
                if status_value == "completed":
                    result = value.get("result")
                    if not isinstance(result, dict):
                        raise TypeError("완료된 오디오 분석 결과가 올바르지 않습니다.")
                    page.set_analysis_result(
                        result, cache_hit=bool(value.get("cache_hit"))
                    )
                    return
                if status_value in {"failed", "cancelled"}:
                    error = value.get("error")
                    message = (
                        str(error.get("message"))
                        if isinstance(error, dict) and error.get("message")
                        else "오디오 분석이 취소되었습니다."
                        if status_value == "cancelled"
                        else "Core에서 오디오 분석에 실패했습니다."
                    )
                    if status_value == "failed":
                        page.set_error(message)
                    return
                await asyncio.sleep(0.2)
                refreshed = await self.client.get(
                    f"/api/v1/audio-analysis/jobs/{job_id}"
                )
                if not isinstance(refreshed, dict):
                    raise TypeError("Core가 올바르지 않은 오디오 분석 상태를 반환했습니다.")
                value = refreshed
        except httpx.HTTPStatusError as exc:
            page.set_error(self._admin_http_error(exc))
        except (httpx.HTTPError, OSError, TypeError, ValueError) as exc:
            page.set_error(f"오디오 파일을 분석하지 못했습니다: {exc}")
        finally:
            if generation == self._audio_generation:
                self._audio_job_id = None

    def _schedule_connection_settings(self) -> None:
        if self._send_task is not None and not self._send_task.done():
            self.window.show_error("답변 생성 중에는 서버 연결을 변경할 수 없습니다.")
            return
        if self._conversation_load_task is not None and not self._conversation_load_task.done():
            self.window.show_error("대화를 불러오는 중에는 서버 연결을 변경할 수 없습니다.")
            return
        if self._connection_task and not self._connection_task.done():
            self.window.raise_()
            self.window.activateWindow()
            return
        self._authentication_failures = 0
        self.connections.auto_reconnect_enabled = True
        if self.connections.state == ConnectionState.MANUAL_OFFLINE:
            self.connections.state = ConnectionState.DISCONNECTED
        self._cancel_auto_reconnect()
        self._cancel_connection_monitor()
        self._connection_task = asyncio.create_task(self._connect_or_configure(force_dialog=True))
        self._connection_task.add_done_callback(self._connection_task_finished)

    def _connection_task_finished(self, task: asyncio.Task[None]) -> None:
        self._connection_task = None
        if task.cancelled():
            return
        error = task.exception()
        if error:
            self._set_connection_state("offline")
            self.window.show_error(f"서버 연결 설정 중 오류가 발생했습니다: {error}")
            self._schedule_auto_reconnect()

    def _disconnect_manually(self) -> None:
        if self._send_task is not None and not self._send_task.done():
            self.window.show_error("답변 생성 중에는 연결을 끊을 수 없습니다.")
            return
        self._cancel_connection_monitor()
        self._cancel_auto_reconnect()
        self.connections.disconnect(manual=True)
        self.client.token = None
        self._schedule_chat_close()
        self._set_connection_state(ConnectionState.MANUAL_OFFLINE)

    def _cancel_connection_monitor(self) -> None:
        task = self._monitor_task
        self._monitor_task = None
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()

    def _cancel_auto_reconnect(self) -> None:
        task = self._auto_reconnect_task
        self._auto_reconnect_task = None
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()

    def _set_remote_controls_enabled(self, enabled: bool) -> None:
        main_setter = getattr(self.window, "set_management_online", None)
        if callable(main_setter):
            main_setter(enabled)
        for child in (
            self.window.console,
            self.window.memory_window,
            self.window.persona_window,
        ):
            setter = getattr(child, "set_online", None)
            if callable(setter):
                setter(enabled)

    def _set_connection_state(self, state: ConnectionState | str) -> None:
        state_value = state.value if isinstance(state, ConnectionState) else state
        display_state = {
            ConnectionState.CONNECTED.value: "online",
            ConnectionState.CONNECTING.value: "connecting",
            ConnectionState.AUTHENTICATING.value: "authenticating",
            ConnectionState.RECONNECT_WAIT.value: "reconnecting",
            ConnectionState.FAILED.value: "error",
            ConnectionState.MANUAL_OFFLINE.value: "offline",
            ConnectionState.DISCONNECTED.value: "offline",
        }.get(state_value, state_value)
        profile = self.connections.active or self._preferred_profile()
        if display_state == "online" and profile is not None:
            self.window.status.setText(f"연결됨: {profile.host}:{profile.port}")
        elif display_state == "reconnecting":
            self.window.status.setText(f"재연결 중… ({self._reconnect_attempts}회)")
        elif display_state in {"connecting", "authenticating"}:
            label = "인증 중…" if display_state == "authenticating" else "서버 연결 중…"
            self.window.status.setText(label)
        else:
            self.window.status.setText("오프라인")
        self._set_remote_controls_enabled(
            display_state == "online"
            and self.connections.active is not None
            and bool(self.client.token)
        )
        self._publish_connection_context(display_state)

    def _mark_connection_lost(self) -> None:
        self.connections.disconnect(manual=False)
        self._cancel_connection_monitor()
        self._schedule_chat_close()
        self._set_connection_state(ConnectionState.RECONNECT_WAIT)
        self._schedule_auto_reconnect()

    def _chat_connection_lost(self) -> None:
        if self.connections.state in {
            ConnectionState.MANUAL_OFFLINE,
            ConnectionState.DISCONNECTED,
            ConnectionState.FAILED,
        }:
            return
        self._mark_connection_lost()

    def _mark_authentication_required(self) -> None:
        self._cancel_connection_monitor()
        self._cancel_auto_reconnect()
        self.connections.disconnect(manual=False)
        self.connections.auto_reconnect_enabled = False
        self.connections.state = ConnectionState.FAILED
        self.client.token = None
        self._schedule_chat_close()
        self._set_connection_state(ConnectionState.FAILED)

    def _handle_authentication_failure(self) -> bool:
        """Retry one ambiguous auth failure, then require explicit repair."""
        self._authentication_failures += 1
        if self._authentication_failures < AUTHENTICATION_FAILURES_BEFORE_PAIRING:
            self._mark_connection_lost()
            return True
        self._mark_authentication_required()
        return False

    def _publish_connection_context(self, state: str) -> None:
        updater = getattr(self.window, "set_connection_context", None)
        if not callable(updater):
            return
        profile = self.connections.active or self._preferred_profile()
        checked_at = self.connections.last_checked_at
        updater(
            {
                "state": state,
                "profile_id": profile.id if profile is not None else None,
                "profile_type": profile.type if profile is not None else None,
                "host": profile.host if profile is not None else None,
                "port": profile.port if profile is not None else None,
                "tls": profile.tls if profile is not None else None,
                "latency_ms": self.connections.last_latency_ms,
                "last_checked_at": checked_at.isoformat() if checked_at is not None else None,
                "reconnect_attempts": self._reconnect_attempts,
                "consecutive_failures": self.connections.consecutive_failures,
                "client_version": APP_VERSION,
                "compatibility_warning": self._protocol_warning,
                "server_status": self._last_server_status,
            }
        )

    def _schedule_auto_reconnect(self) -> None:
        if (
            self._shutdown_started
            or self.connections.shutdown_started
            or self.connections.active is not None
            or not self.connections.auto_reconnect_enabled
            or self.connections.state == ConnectionState.MANUAL_OFFLINE
            or not any(
                profile.enabled for profile in self.connections.profiles
            )
        ):
            return
        if self._auto_reconnect_task is not None and not self._auto_reconnect_task.done():
            return
        task = self.connections.schedule_reconnect(
            self._connected,
            on_attempt=self._reconnect_attempt_started,
        )
        if task is None:
            return
        self._auto_reconnect_task = task
        task.add_done_callback(self._auto_reconnect_finished)

    def _reconnect_attempt_started(self) -> None:
        self._reconnect_attempts += 1
        self._set_connection_state(ConnectionState.WAITING_RETRY)

    def _auto_reconnect_finished(self, task: asyncio.Task[None]) -> None:
        if task is self._auto_reconnect_task:
            self._auto_reconnect_task = None
        if task.cancelled():
            return
        error = task.exception()
        if error:
            self.connections.state = ConnectionState.FAILED
            self._set_connection_state(ConnectionState.FAILED)
            return
        if (
            self.connections.state == ConnectionState.FAILED
            and isinstance(
                self.connections.last_error, ServerIdentityMismatchError
            )
        ):
            self._set_connection_state(ConnectionState.FAILED)
            self.window.show_error(str(self.connections.last_error))
            return
        # A health probe can succeed while authenticated status or WebSocket
        # establishment still fails.  `_connected` then returns after moving the
        # manager back to RECONNECT_WAIT, but could not reschedule from inside
        # this still-running task.  Re-arm only after the task is cleared here.
        if (
            self.connections.active is None
            and self.connections.state == ConnectionState.RECONNECT_WAIT
            and self.connections.auto_reconnect_enabled
        ):
            self._schedule_auto_reconnect()

    async def _auto_reconnect_loop(self) -> None:
        async for _delay in self.connections.reconnect_delays():
            self._reconnect_attempts += 1
            self._set_connection_state(ConnectionState.RECONNECT_WAIT)
        if self.connections.active is not None:
            await self._connected(self.connections.active)

    def _ensure_connection_monitor(self) -> None:
        if self._shutdown_started:
            return
        if self._monitor_task is not None and not self._monitor_task.done():
            return
        task = asyncio.create_task(self._monitor_connection())
        self._monitor_task = task
        task.add_done_callback(self._connection_monitor_finished)

    def _connection_monitor_finished(self, task: asyncio.Task[None]) -> None:
        if task is self._monitor_task:
            self._monitor_task = None
        if task.cancelled():
            return
        error = task.exception()
        if error:
            self.connections.disconnect(manual=False)
            self._set_connection_state(ConnectionState.FAILED)
            self._schedule_auto_reconnect()

    async def _monitor_connection(self) -> None:
        status_check_every = max(
            1,
            math.ceil(
                self.connections.status_interval / self.connections.health_interval
            ),
        )
        health_checks_since_status = status_check_every - 1
        while self.connections.active is not None:
            await asyncio.sleep(self.connections.health_interval)
            if not await self.connections.check_active():
                if isinstance(
                    self.connections.last_error, ServerIdentityMismatchError
                ):
                    self._set_connection_state(ConnectionState.RECONNECT_WAIT)
                    self._schedule_auto_reconnect()
                    self.window.show_error(
                        "연결 주소에서 다른 Core가 감지되어 저장된 다른 주소를 확인합니다."
                    )
                    return
                if self.connections.active is None:
                    self._set_connection_state(ConnectionState.RECONNECT_WAIT)
                    self._schedule_auto_reconnect()
                    return
                self._publish_connection_context("online")
                continue
            if self.client.token:
                health_checks_since_status += 1
                if health_checks_since_status < status_check_every:
                    self._set_connection_state(ConnectionState.CONNECTED)
                    continue
                health_checks_since_status = 0
                try:
                    status = await self.client.get("/api/v1/status")
                    if not isinstance(status, dict):
                        raise TypeError("서버가 올바르지 않은 상태 응답을 반환했습니다.")
                    profile = self.connections.active
                    if profile is not None:
                        self._bind_authenticated_server(
                            profile, status, promote_token=False
                        )
                except ServerIdentityMismatchError as exc:
                    self._mark_authentication_required()
                    self.window.show_error(f"서버 식별 정보 확인에 실패했습니다: {exc}")
                    return
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code in {401, 403}:
                        retrying = self._handle_authentication_failure()
                        self.window.show_error(
                            "서버 인증 확인에 일시적으로 실패했습니다. 자동으로 다시 연결합니다."
                            if retrying
                            else "서버 인증이 만료되었습니다. 새 페어링이 필요합니다."
                        )
                        return
                    self._status_failures += 1
                    self.connections.last_error = exc
                    if (
                        self._status_failures
                        >= self.connections.failures_before_offline
                    ):
                        self._mark_connection_lost()
                        return
                    health_checks_since_status = status_check_every - 1
                    self._publish_connection_context("online")
                    continue
                except (httpx.HTTPError, OSError) as exc:
                    self._status_failures += 1
                    self.connections.last_error = exc
                    if (
                        self._status_failures
                        >= self.connections.failures_before_offline
                    ):
                        self._mark_connection_lost()
                        return
                    health_checks_since_status = status_check_every - 1
                    self._publish_connection_context("online")
                    continue
                except TypeError as exc:
                    self.connections.state = ConnectionState.FAILED
                    self._set_connection_state(ConnectionState.FAILED)
                    self.window.show_error(str(exc))
                    return
                self._status_failures = 0
                self._authentication_failures = 0
                self._apply_server_status(status)
                agent_status = status.get("agent")
                if isinstance(agent_status, dict) and agent_status.get("enabled"):
                    try:
                        if (
                            self._agent_controller is None
                            or not self.client.agent_connected
                        ):
                            profile = self.connections.active
                            if profile is not None:
                                await self._start_agent_session(profile)
                        elif self._agent_controller is not None:
                            await self._agent_controller.advertise(
                                app_version=APP_VERSION,
                                protocol_version=PROTOCOL_VERSION,
                            )
                    except (ConnectionError, OSError, RuntimeError, ValueError):
                        self._end_agent_session()
                elif self._agent_controller is not None:
                    await self.client.close_agent_connection()
                    self._end_agent_session()
            self._set_connection_state(ConnectionState.CONNECTED)

    async def _connect_or_configure(self, *, force_dialog: bool = False) -> None:
        async with self._connection_lock:
            selected = self.connections.active or self._preferred_profile()
            error_message: str | None = None

            if not force_dialog and self.connections.profiles:
                self._set_connection_state(ConnectionState.CONNECTING)
                connected = await self.connections.connect()
                if connected:
                    await self._connected(connected)
                    return
                error_message = self._connection_failure_message(saved_profile=True)
                if (
                    self.connections.auto_reconnect_enabled
                    and any(profile.enabled for profile in self.connections.profiles)
                ):
                    self.connections.state = ConnectionState.RECONNECT_WAIT
                    self._set_connection_state(ConnectionState.RECONNECT_WAIT)
                    self._schedule_auto_reconnect()
                    return

            while True:
                dialog = ConnectionDialog(selected, error_message, self.window)
                if dialog.exec() != QDialog.DialogCode.Accepted:
                    if not self.connections.active:
                        self._set_connection_state(ConnectionState.DISCONNECTED)
                        self._schedule_auto_reconnect()
                    else:
                        self._set_connection_state(ConnectionState.CONNECTED)
                        self._ensure_connection_monitor()
                    return

                selected = dialog.connection_profile()
                profiles = self._replace_profile(selected)
                try:
                    save_connection_profiles(profiles)
                except OSError as exc:
                    error_message = f"연결 설정을 저장하지 못했습니다.\n{exc}"
                    continue

                self.connections.set_profiles(profiles)
                self._set_connection_state(ConnectionState.CONNECTING)
                connected = await self.connections.connect()
                if connected:
                    await self._connected(connected)
                    return
                error_message = self._connection_failure_message(saved_profile=False)

    def _preferred_profile(self) -> ConnectionProfile | None:
        enabled = [profile for profile in self.connections.profiles if profile.enabled]
        return min(enabled, key=lambda profile: profile.priority) if enabled else None

    def _replace_profile(self, selected: ConnectionProfile) -> list[ConnectionProfile]:
        profiles = list(self.connections.profiles)
        for index, profile in enumerate(profiles):
            if profile.id == selected.id:
                profiles[index] = selected
                return profiles
        profiles.append(selected)
        return profiles

    def _connection_failure_message(self, *, saved_profile: bool) -> str:
        prefix = (
            "저장된 서버에 연결할 수 없습니다."
            if saved_profile
            else "입력한 서버에 연결할 수 없습니다."
        )
        error = self.connections.last_error
        if isinstance(error, httpx.TimeoutException):
            detail = "연결 시간이 초과되었습니다."
        elif isinstance(error, httpx.ConnectError):
            detail = "서버가 응답하지 않습니다."
        elif isinstance(error, ServerIdentityMismatchError):
            detail = str(error)
        elif isinstance(error, httpx.HTTPStatusError):
            detail = f"서버 상태 확인 응답: HTTP {error.response.status_code}"
        else:
            detail = "서버 주소, 포트, TLS 설정과 서버 실행 상태를 확인하세요."
        return f"{prefix}\n{detail}"

    async def _connected(self, profile: ConnectionProfile) -> None:
        self._cancel_auto_reconnect()
        self.connections.state = ConnectionState.AUTHENTICATING
        self._set_connection_state(ConnectionState.AUTHENTICATING)
        observed_server_id = self.connections.server_id_for(profile)
        try:
            self.client.token = (
                load_token_for_server(profile, observed_server_id)
                if observed_server_id is not None
                else load_token_for_profile(profile)
            )
        except Exception as exc:
            self.client.token = None
            self.window.show_error(f"저장된 인증 정보를 읽지 못했습니다: {exc}")

        if not self.client.token and not await self._pair_if_required(profile):
            self.window.model.setText("모델: 페어링 필요")
            self._mark_authentication_required()
            return

        try:
            status = await self.client.get("/api/v1/status")
            if not isinstance(status, dict):
                raise TypeError("서버가 올바르지 않은 상태 응답을 반환했습니다.")
            profile = self._bind_authenticated_server(profile, status)
            server_key = (
                f"server:{profile.server_id}"
                if profile.server_id is not None
                else f"{'https' if profile.tls else 'http'}://{profile.host}:{profile.port}"
            )
            self._activate_server_key(server_key)
            self._authentication_failures = 0
            self._apply_server_status(status)
            await self.client.ensure_chat_connection()
            agent_status = status.get("agent")
            if isinstance(agent_status, dict) and agent_status.get("enabled"):
                try:
                    await self._start_agent_session(profile)
                except (ConnectionError, OSError, RuntimeError, ValueError) as exc:
                    self._end_agent_session()
                    if self.window.agent_window is not None:
                        self.window.agent_window.show_error(
                            f"Agent 채널을 연결하지 못했습니다: {exc}"
                        )
            self.connections.mark_connected()
            self._reconnect_attempts = 0
            self._status_failures = 0
            self._set_connection_state(ConnectionState.CONNECTED)
            self._ensure_connection_monitor()
            if self.window.console is not None and self.window.console.isVisible():
                await self._refresh_admin()
            if self.window.memory_window is not None and self.window.memory_window.isVisible():
                await self._refresh_memories()
            if self.window.history_window is not None and self.window.history_window.isVisible():
                await self._refresh_conversations()
            if self.window.persona_window is not None and self.window.persona_window.isVisible():
                await self._refresh_persona()
        except ServerIdentityMismatchError as exc:
            self._mark_authentication_required()
            self.window.show_error(f"서버 식별 정보 확인에 실패했습니다: {exc}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {401, 403}:
                retrying = self._handle_authentication_failure()
                self.window.show_error(
                    "서버 인증 확인에 일시적으로 실패했습니다. 자동으로 다시 연결합니다."
                    if retrying
                    else "이 서버의 인증 정보가 유효하지 않습니다. "
                    "서버 관리자에게 새 페어링을 요청하세요."
                )
            else:
                self._mark_connection_lost()
                self.window.show_error(
                    f"서버 상태를 확인하지 못했습니다: HTTP {exc.response.status_code}"
                )
        except (httpx.HTTPError, OSError, TypeError) as exc:
            if isinstance(exc, (httpx.TransportError, OSError)):
                self._mark_connection_lost()
            else:
                self.connections.state = ConnectionState.FAILED
                self._set_connection_state(ConnectionState.FAILED)
            self.window.show_error(f"서버 상태를 확인하지 못했습니다: {exc}")

    def _activate_server_key(self, server_key: str) -> None:
        if self._active_server_key == server_key:
            return
        self._cancel_history_operations()
        self._active_server_key = server_key
        self._active_conversation_id = None
        self._conversation_titles.clear()
        self.window.clear_conversation()

    def _bind_authenticated_server(
        self,
        profile: ConnectionProfile,
        status: dict[str, Any],
        *,
        promote_token: bool = True,
    ) -> ConnectionProfile:
        """Pin a verified Core identity and promote its legacy endpoint token."""

        observed_server_id = self.connections.server_id_for(profile)
        raw_status_server_id = status.get("server_id")
        if raw_status_server_id in (None, ""):
            if observed_server_id is not None:
                raise ServerIdentityMismatchError(
                    "health와 인증된 status의 서버 ID가 일치하지 않습니다."
                )
            return profile
        try:
            status_server_id = str(UUID(str(raw_status_server_id)))
        except ValueError as exc:
            raise ServerIdentityMismatchError(
                "인증된 status가 올바르지 않은 서버 ID를 반환했습니다."
            ) from exc
        if observed_server_id is not None and status_server_id != observed_server_id:
            raise ServerIdentityMismatchError(
                "health와 인증된 status가 서로 다른 서버를 가리킵니다."
            )
        if profile.server_id is not None and status_server_id != profile.server_id:
            raise ServerIdentityMismatchError(
                "저장된 프로필과 다른 Nivelle Core 서버가 응답했습니다."
            )

        token = self.client.token
        if promote_token and token:
            try:
                save_token_for_server(status_server_id, token)
            except (KeyringError, OSError, RuntimeError) as exc:
                self.window.show_error(
                    f"서버별 인증 정보를 저장하지 못했습니다: {exc}"
                )

        if profile.server_id == status_server_id:
            return profile
        pinned = profile.model_copy(update={"server_id": status_server_id})
        profiles = [
            pinned if item is profile or item.id == profile.id else item
            for item in self.connections.profiles
        ]
        self.connections.profiles = profiles
        if self.connections.active is profile:
            self.connections.active = pinned
        if profile.id != "runtime-gateway":
            try:
                save_connection_profiles(profiles)
            except OSError as exc:
                self.window.show_error(f"서버 식별 정보를 저장하지 못했습니다: {exc}")
        return pinned

    def _apply_server_status(self, status: dict[str, Any]) -> None:
        self._last_server_status = dict(status)
        client_id = status.get("client_id")
        if client_id not in (None, ""):
            self.client.client_id = str(client_id)
        runtime_value = status.get("runtime")
        runtime = runtime_value if isinstance(runtime_value, dict) else {}
        remote_protocol = status.get("protocol_version") or runtime.get("protocol_version")
        if isinstance(remote_protocol, str):
            compatibility = protocol_compatibility(remote_protocol)
            self._protocol_compatible = compatibility.compatible
            self._protocol_warning = compatibility.warning
        else:
            # Phase 2.0 servers did not report this field. Their WebSocket still
            # performs the authoritative protocol check.
            self._protocol_compatible = True
            self._protocol_warning = "서버가 프로토콜 버전을 보고하지 않는 구버전입니다."
        backend_value = status.get("llama_server")
        backend = backend_value if isinstance(backend_value, dict) else {}
        loaded_model = status.get("model_name") or backend.get("loaded_model")
        if loaded_model:
            self.window.model.setText(f"모델: {loaded_model}")
        else:
            self.window.model.setText(
                f"모델 상태: {backend.get('state') or status.get('assistant_state') or '미확인'}"
            )
        current_state = (
            "online"
            if self.connections.state == ConnectionState.CONNECTED
            else "authenticating"
            if self.connections.active is not None
            else "offline"
        )
        self._publish_connection_context(current_state)

    async def _refresh_admin(self) -> None:
        console = self.window.console
        if console is None:
            return
        if not self.connections.active:
            console.show_error("서버에 연결되어 있지 않습니다. 먼저 '서버 연결'을 눌러 연결하세요.")
            return
        if not self.client.token:
            console.show_error("서버 설정을 보려면 페어링이 필요합니다.")
            return

        console.show_loading(True)
        try:
            await self._load_admin_data()
            console.show_message("서버 상태와 설정을 최신 정보로 갱신했습니다.")
        except httpx.HTTPStatusError as exc:
            console.show_error(self._admin_http_error(exc))
        except (httpx.HTTPError, OSError, TypeError, ValueError) as exc:
            console.show_error(f"서버 상태와 설정을 불러오지 못했습니다: {exc}")
        finally:
            console.show_loading(False)

    async def _create_pairing_code(self) -> None:
        console = self.window.console
        if console is None:
            return
        if not self.connections.active or not self.client.token:
            console.show_error("새 Link를 등록하려면 서버 연결과 관리자 인증이 필요합니다.")
            return
        console.show_loading(True, "새 Link 페어링 코드를 생성하는 중…")
        try:
            value = await self.client.post("/api/v1/pairing/code")
            if not isinstance(value, dict):
                raise TypeError("서버가 올바르지 않은 페어링 코드 응답을 반환했습니다.")
            code = str(value.get("code") or "")
            if len(code) != 6 or not code.isdigit():
                raise ValueError("서버가 올바르지 않은 페어링 코드를 반환했습니다.")
            expires_at = value.get("expires_at")
            console.show_pairing_code(
                code, str(expires_at) if expires_at not in (None, "") else None
            )
            console.show_message(
                "새 Link에서 아래 일회성 코드를 입력하세요. 코드는 10분 뒤 만료됩니다."
            )
        except httpx.HTTPStatusError as exc:
            console.show_error(self._admin_http_error(exc))
        except (httpx.HTTPError, OSError, TypeError, ValueError) as exc:
            console.show_error(f"페어링 코드를 생성하지 못했습니다: {exc}")
        finally:
            console.show_loading(False)

    async def _save_admin(self, section: str, value: dict[str, Any]) -> None:
        console = self.window.console
        if console is None:
            return
        if not self.connections.active or not self.client.token:
            console.show_error("서버 연결 및 페어링 상태를 확인하세요.")
            return
        if section not in {"server", "models", "inference"}:
            console.show_error(f"지원하지 않는 설정 영역입니다: {section}")
            return

        console.show_loading(True, "설정을 검증하고 저장하는 중…")
        try:
            await self.client.post(
                "/api/v1/settings/validate", {"section": section, "value": value}
            )
            await self.client.put(f"/api/v1/settings/{section}", value)
            await self._load_admin_data()
            console.show_message(
                "설정을 저장했습니다. 화면에 안내된 항목은 서버 또는 llama-server를 "
                "다시 시작한 뒤 적용됩니다."
            )
        except httpx.HTTPStatusError as exc:
            console.show_error(self._admin_http_error(exc))
        except (httpx.HTTPError, OSError, TypeError, ValueError) as exc:
            console.show_error(f"설정을 저장하지 못했습니다: {exc}")
        finally:
            console.show_loading(False)

    async def _rollback_admin(self, revision_id: int) -> None:
        console = self.window.console
        if console is None:
            return
        if not self.connections.active or not self.client.token:
            console.show_error("서버 연결 및 페어링 상태를 확인하세요.")
            return

        console.show_loading(True, f"변경 #{revision_id}을 되돌리는 중…")
        try:
            await self.client.post(f"/api/v1/settings/rollback/{revision_id}")
            await self._load_admin_data()
            console.show_message(f"변경 #{revision_id} 직전 설정으로 되돌렸습니다.")
        except httpx.HTTPStatusError as exc:
            console.show_error(self._admin_http_error(exc))
        except (httpx.HTTPError, OSError, TypeError, ValueError) as exc:
            console.show_error(f"설정을 되돌리지 못했습니다: {exc}")
        finally:
            console.show_loading(False)

    async def _load_admin_data(self) -> None:
        console = self.window.console
        if console is None:
            return
        status, settings, revisions = await asyncio.gather(
            self.client.get("/api/v1/status"),
            self.client.get("/api/v1/settings"),
            self.client.get("/api/v1/settings/revisions"),
        )
        if not isinstance(status, dict) or not isinstance(settings, dict):
            raise TypeError("서버가 올바르지 않은 설정 응답을 반환했습니다.")
        if not isinstance(revisions, list) or not all(
            isinstance(revision, dict) for revision in revisions
        ):
            raise TypeError("서버가 올바르지 않은 변경 이력 응답을 반환했습니다.")
        console.set_status(status)
        console.set_settings(settings)
        console.set_revisions(revisions)
        self._apply_server_status(status)

    def _memory_access_ready(self) -> bool:
        window = self.window.memory_window
        if window is None:
            return False
        if not self.connections.active:
            window.show_error("서버에 연결되어 있지 않습니다. 먼저 '서버 연결'을 눌러 연결하세요.")
            return False
        if not self.client.token:
            window.show_error("장기 기억을 관리하려면 서버 페어링이 필요합니다.")
            return False
        return True

    async def _refresh_memories(self, query: str | None = None) -> None:
        window = self.window.memory_window
        if window is None or not self._memory_access_ready():
            return
        effective_query = window.current_query() if query is None else query.strip()
        window.show_loading(
            True, "장기 기억을 검색하는 중…" if effective_query else "장기 기억을 불러오는 중…"
        )
        try:
            count = await self._load_memories(effective_query)
            if effective_query:
                window.show_message(f"'{effective_query}' 검색 결과 {count}개를 불러왔습니다.")
            else:
                window.show_message(f"저장된 장기 기억 {count}개를 불러왔습니다.")
        except httpx.HTTPStatusError as exc:
            window.show_error(self._memory_http_error(exc))
        except (httpx.HTTPError, OSError, TypeError, ValueError) as exc:
            window.show_error(f"장기 기억을 불러오지 못했습니다: {exc}")
        finally:
            window.show_loading(False)

    async def _create_memory(self, value: dict[str, Any]) -> None:
        window = self.window.memory_window
        if window is None or not self._memory_access_ready():
            return
        window.show_loading(True, "새 장기 기억을 저장하는 중…")
        try:
            await self.client.post("/api/v1/memories", value)
            window.clear_editor()
            await self._load_memories(window.current_query())
            window.show_message("새 장기 기억을 저장했습니다.")
        except httpx.HTTPStatusError as exc:
            window.show_error(self._memory_http_error(exc))
        except (httpx.HTTPError, OSError, TypeError, ValueError) as exc:
            window.show_error(f"장기 기억을 저장하지 못했습니다: {exc}")
        finally:
            window.show_loading(False)

    async def _update_memory(self, memory_id: str, value: dict[str, Any]) -> None:
        window = self.window.memory_window
        if window is None or not self._memory_access_ready():
            return
        window.show_loading(True, "장기 기억을 수정하는 중…")
        try:
            await self.client.patch(f"/api/v1/memories/{memory_id}", value)
            await self._load_memories(window.current_query())
            window.show_message("선택한 장기 기억을 수정했습니다.")
        except httpx.HTTPStatusError as exc:
            window.show_error(self._memory_http_error(exc))
        except (httpx.HTTPError, OSError, TypeError, ValueError) as exc:
            window.show_error(f"장기 기억을 수정하지 못했습니다: {exc}")
        finally:
            window.show_loading(False)

    async def _delete_memory(self, memory_id: str) -> None:
        window = self.window.memory_window
        if window is None or not self._memory_access_ready():
            return
        window.show_loading(True, "장기 기억을 삭제하는 중…")
        try:
            await self.client.delete(f"/api/v1/memories/{memory_id}")
            window.clear_editor()
            await self._load_memories(window.current_query())
            window.show_message("선택한 장기 기억을 삭제했습니다.")
        except httpx.HTTPStatusError as exc:
            window.show_error(self._memory_http_error(exc))
        except (httpx.HTTPError, OSError, TypeError, ValueError) as exc:
            window.show_error(f"장기 기억을 삭제하지 못했습니다: {exc}")
        finally:
            window.show_loading(False)

    async def _load_memories(self, query: str = "") -> int:
        window = self.window.memory_window
        if window is None:
            return 0
        if query:
            params: dict[str, str | int | bool] = {"q": query, "limit": 50}
            if window.include_inactive():
                params["include_inactive"] = True
            values = await self.client.get("/api/v1/memories/search", params)
        else:
            values = await self.client.get("/api/v1/memories", {"limit": 50})
        if not isinstance(values, list) or not all(isinstance(value, dict) for value in values):
            raise TypeError("서버가 올바르지 않은 장기 기억 응답을 반환했습니다.")
        window.set_memories(values)
        return len(values)

    def _history_access_ready(self) -> bool:
        window = self.window.history_window
        if window is None:
            return False
        if not self.connections.active:
            window.show_error("서버에 연결되어 있지 않습니다. 먼저 '서버 연결'을 선택하세요.")
            return False
        if not self.client.token:
            window.show_error("대화 기록을 보려면 서버 페어링이 필요합니다.")
            return False
        return True

    async def _refresh_conversations(self, *, generation: int | None = None) -> None:
        window = self.window.history_window
        if window is None or not self._history_access_ready():
            return
        server_key = self._active_server_key
        window.show_loading(True)
        try:
            values = await self.client.get("/api/v1/conversations")
            if generation is not None and (
                generation != self._history_refresh_generation
                or server_key != self._active_server_key
            ):
                return
            if not isinstance(values, list) or not all(isinstance(value, dict) for value in values):
                raise TypeError("서버가 올바르지 않은 대화 목록을 반환했습니다.")
            self._conversation_titles = {
                str(value.get("id")): str(value.get("title") or "제목 없는 대화")
                for value in values
                if value.get("id")
            }
            window.set_conversations(values)
            window.show_message(f"저장된 대화 {len(values)}개를 불러왔습니다.")
        except httpx.HTTPStatusError as exc:
            if generation is not None and (
                generation != self._history_refresh_generation
                or server_key != self._active_server_key
            ):
                return
            window.show_error(self._conversation_http_error(exc))
        except (httpx.HTTPError, OSError, TypeError, ValueError) as exc:
            if generation is not None and (
                generation != self._history_refresh_generation
                or server_key != self._active_server_key
            ):
                return
            window.show_error(f"대화 목록을 불러오지 못했습니다: {exc}")
        finally:
            if generation is None or generation == self._history_refresh_generation:
                window.show_loading(False)

    async def _load_conversation(
        self,
        conversation_id: str,
        *,
        generation: int | None = None,
        update_main: bool = True,
    ) -> None:
        window = self.window.history_window
        if window is None or not self._history_access_ready():
            return
        server_key = self._active_server_key
        window.show_loading(True, "선택한 대화를 불러오는 중…")
        try:
            values = await self.client.get(f"/api/v1/conversations/{conversation_id}/messages")
            tool_values: object = []
            agent_status = self._last_server_status.get("agent")
            if isinstance(agent_status, dict) and agent_status.get("enabled"):
                try:
                    tool_values = await self.client.get(
                        f"/api/v1/conversations/{conversation_id}/tool-calls"
                    )
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code != 404:
                        raise
            if generation is not None and (
                generation != self._conversation_load_generation
                or server_key != self._active_server_key
            ):
                return
            if update_main and self._send_task is not None and not self._send_task.done():
                return
            if not isinstance(values, list) or not all(isinstance(value, dict) for value in values):
                raise TypeError("서버가 올바르지 않은 대화 메시지를 반환했습니다.")
            if tool_values is None:
                tool_values = []
            if not isinstance(tool_values, list) or not all(
                isinstance(value, dict) for value in tool_values
            ):
                raise TypeError("서버가 올바르지 않은 도구 호출 기록을 반환했습니다.")
            title = self._conversation_titles.get(conversation_id, "저장된 대화")
            if update_main:
                self._active_conversation_id = conversation_id
                self.window.load_messages(values)
                self.window.load_tool_calls(tool_values)
            window.set_preview(title, values)
            window.show_message(
                "선택한 대화를 열었습니다. 메인 채팅에서 이어서 대화할 수 있습니다."
                if update_main
                else "대화 기록을 최신 내용으로 갱신했습니다."
            )
        except httpx.HTTPStatusError as exc:
            if generation is not None and (
                generation != self._conversation_load_generation
                or server_key != self._active_server_key
            ):
                return
            window.show_error(self._conversation_http_error(exc))
        except (httpx.HTTPError, OSError, TypeError, ValueError) as exc:
            if generation is not None and (
                generation != self._conversation_load_generation
                or server_key != self._active_server_key
            ):
                return
            window.show_error(f"대화를 불러오지 못했습니다: {exc}")
        finally:
            if generation is None or generation == self._conversation_load_generation:
                window.show_loading(False)

    def _persona_access_ready(self) -> bool:
        window = self.window.persona_window
        if window is None:
            return False
        if not self.connections.active:
            window.show_error("서버에 연결되어 있지 않습니다. 먼저 '서버 연결'을 선택하세요.")
            return False
        if not self.client.token:
            window.show_error("성격 설정을 관리하려면 서버 페어링이 필요합니다.")
            return False
        return True

    async def _refresh_persona(self) -> None:
        window = self.window.persona_window
        if window is None or not self._persona_access_ready():
            return
        window.show_loading(True)
        try:
            value = await self.client.get("/api/v1/persona")
            if not isinstance(value, dict):
                raise TypeError("서버가 올바르지 않은 성격 설정을 반환했습니다.")
            window.set_persona(value)
            window.show_message("현재 성격 설정을 불러왔습니다.")
        except httpx.HTTPStatusError as exc:
            window.show_error(self._admin_http_error(exc))
        except (httpx.HTTPError, OSError, TypeError, ValueError) as exc:
            window.show_error(f"성격 설정을 불러오지 못했습니다: {exc}")
        finally:
            window.show_loading(False)

    async def _save_persona(self, value: dict[str, Any]) -> None:
        window = self.window.persona_window
        if window is None or not self._persona_access_ready():
            return
        window.show_loading(True, "성격 설정을 저장하는 중…")
        try:
            saved = await self.client.put("/api/v1/persona", value)
            if not isinstance(saved, dict):
                raise TypeError("서버가 올바르지 않은 성격 저장 응답을 반환했습니다.")
            window.set_persona(saved)
            window.show_message("성격 설정을 저장했습니다. 다음 답변부터 적용됩니다.")
        except httpx.HTTPStatusError as exc:
            window.show_error(self._admin_http_error(exc))
        except (httpx.HTTPError, OSError, TypeError, ValueError) as exc:
            window.show_error(f"성격 설정을 저장하지 못했습니다: {exc}")
        finally:
            window.show_loading(False)

    @staticmethod
    def _conversation_http_error(exc: httpx.HTTPStatusError) -> str:
        if exc.response.status_code in {401, 403}:
            return "대화 기록을 보려면 서버 연결과 페어링이 필요합니다."
        if exc.response.status_code == 404:
            return "선택한 대화를 서버에서 찾을 수 없습니다. 목록을 새로고침하세요."
        return f"대화 기록 요청에 실패했습니다: HTTP {exc.response.status_code}"

    @staticmethod
    def _admin_http_error(exc: httpx.HTTPStatusError) -> str:
        response = exc.response
        message = f"서버 관리 요청에 실패했습니다: HTTP {response.status_code}"
        try:
            body = response.json()
        except ValueError:
            return message
        if not isinstance(body, dict):
            return message

        error = body.get("error")
        if isinstance(error, dict):
            server_message = error.get("message")
            if server_message:
                message = str(server_message)
            details = error.get("details")
            validation_errors = details.get("errors") if isinstance(details, dict) else None
            if isinstance(validation_errors, list):
                descriptions: list[str] = []
                for validation_error in validation_errors:
                    if not isinstance(validation_error, dict):
                        continue
                    location = ".".join(str(item) for item in validation_error.get("loc", []))
                    detail = str(validation_error.get("msg", "올바르지 않은 값"))
                    descriptions.append(f"{location}: {detail}" if location else detail)
                if descriptions:
                    message += "\n" + "\n".join(descriptions)
            return message

        if body.get("detail"):
            return str(body["detail"])
        return message

    @staticmethod
    def _memory_http_error(exc: httpx.HTTPStatusError) -> str:
        response = exc.response
        if response.status_code in {401, 403}:
            return "장기 기억을 관리할 인증이 없거나 만료되었습니다. 서버에 다시 페어링하세요."
        if response.status_code == 404:
            return "선택한 장기 기억을 서버에서 찾을 수 없습니다. 목록을 새로고침하세요."

        message = f"장기 기억 요청에 실패했습니다: HTTP {response.status_code}"
        try:
            body = response.json()
        except ValueError:
            return message
        if not isinstance(body, dict):
            return message

        error = body.get("error")
        if isinstance(error, dict):
            server_message = error.get("message")
            return str(server_message) if server_message else message

        detail = body.get("detail")
        if isinstance(detail, str):
            return detail
        if isinstance(detail, dict):
            if detail.get("code") == "MEMORY_DUPLICATE":
                existing_id = detail.get("existing_memory_id")
                suffix = f" (기존 기억 ID: {existing_id})" if existing_id else ""
                return f"동일한 활성 장기 기억이 이미 있습니다.{suffix}"
            detail_message = detail.get("message")
            if detail_message:
                return str(detail_message)
        if isinstance(detail, list):
            descriptions: list[str] = []
            for validation_error in detail:
                if not isinstance(validation_error, dict):
                    continue
                location_items = validation_error.get("loc", [])
                location = ".".join(
                    str(item) for item in location_items if str(item) not in {"body", "query"}
                )
                description = str(validation_error.get("msg", "올바르지 않은 값"))
                descriptions.append(f"{location}: {description}" if location else description)
            if descriptions:
                return "입력값을 확인하세요.\n" + "\n".join(descriptions)
        return message

    async def _pair_if_required(self, profile: ConnectionProfile) -> bool:
        try:
            async with httpx.AsyncClient(timeout=10) as http:
                response = await http.get(self.connections.base_url() + "/api/v1/pairing/status")
                response.raise_for_status()
                pairing_status = response.json()
                if not isinstance(pairing_status, dict):
                    raise TypeError("서버가 올바르지 않은 페어링 상태를 반환했습니다.")
                required = bool(pairing_status.get("pairing_required"))
                available = bool(pairing_status.get("pairing_available"))
            if not required and not available:
                self.window.show_error(
                    "이 서버에 저장된 인증 정보가 없습니다. 서버 관리자에게 새 페어링을 요청하세요."
                )
                return False

            dialog = PairingDialog()
            if dialog.exec() != QDialog.DialogCode.Accepted:
                self.window.status.setText("연결됨 · 페어링 필요")
                return False
            token = await self.client.pair(dialog.code.text(), dialog.name.text())
            save_token_for_profile(profile, token)
            self.window.status.setText(f"연결됨: {profile.host}:{profile.port} · 페어링 완료")
            return True
        except httpx.HTTPStatusError as exc:
            self.window.show_error(f"페어링에 실패했습니다: HTTP {exc.response.status_code}")
        except (httpx.HTTPError, OSError, KeyError, TypeError) as exc:
            self.window.show_error(f"페어링에 실패했습니다: {exc}")
        return False

    async def send(self, text: str) -> None:
        if not self.connections.active:
            self.window.show_error("먼저 서버에 연결하세요.")
            return
        if not self.client.token:
            self.window.show_error("서버 페어링이 필요합니다.")
            return
        if not self._protocol_compatible:
            self.window.show_error(
                self._protocol_warning or "서버와 클라이언트 프로토콜이 호환되지 않습니다."
            )
            return

        request_id = str(uuid4())
        client_message_id = str(uuid4())
        self.window.append_user_message(
            text,
            client_message_id=client_message_id,
            request_id=request_id,
        )
        profile = self.connections.active
        request: dict[str, Any] = {
            "type": "chat.request",
            "protocol_version": PROTOCOL_VERSION,
            "request_id": request_id,
            "client_message_id": client_message_id,
            "content": text,
        }
        if profile is not None:
            request["runtime_context"] = {
                "profile_id": profile.id,
                "connection_type": profile.type,
                "host": profile.host,
                "port": profile.port,
                "tls": profile.tls,
                "client_version": APP_VERSION,
                "latency_ms": self.connections.last_latency_ms,
            }
        if self._active_conversation_id is not None:
            request["conversation_id"] = self._active_conversation_id

        completed = False
        expected_assistant_message_id: str | None = None
        context_setter = getattr(self.window, "set_used_memories", None)
        if callable(context_setter):
            context_setter([])
        metrics_setter = getattr(self.window, "set_generation_metrics", None)
        if callable(metrics_setter):
            metrics_setter({})
        self.window.begin_assistant_message(request_id=request_id)
        try:
            async for event in self.client.chat(request):
                event_request_id = event.get("request_id")
                if event_request_id not in (None, "") and str(event_request_id) != request_id:
                    continue
                event_type = event.get("type")
                payload_value = event.get("payload")
                payload = payload_value if isinstance(payload_value, dict) else {}
                event_client_message_id = payload.get("client_message_id")
                if (
                    event_client_message_id not in (None, "")
                    and str(event_client_message_id) != client_message_id
                ):
                    continue
                if completed:
                    # Resume the authoritative NetworkClient generator so its
                    # terminal return/finally removes the request queue, while
                    # ignoring any replay yielded by a test or legacy transport.
                    continue
                if event_type == "chat.accepted":
                    conversation_id = payload.get("conversation_id")
                    if conversation_id:
                        self._active_conversation_id = str(conversation_id)
                    accepted_assistant_id = payload.get("assistant_message_id")
                    if accepted_assistant_id not in (None, ""):
                        accepted_id = str(accepted_assistant_id)
                        if (
                            expected_assistant_message_id is not None
                            and expected_assistant_message_id != accepted_id
                        ):
                            continue
                        expected_assistant_message_id = accepted_id
                    self.window.bind_turn_message_ids(
                        request_id=request_id,
                        client_message_id=client_message_id,
                        user_message_id=payload.get("user_message_id"),
                        assistant_message_id=expected_assistant_message_id,
                    )
                elif event_type in {"assistant.context", "chat.context"}:
                    context_assistant_id = payload.get("assistant_message_id")
                    if context_assistant_id not in (None, ""):
                        context_id = str(context_assistant_id)
                        if (
                            expected_assistant_message_id is not None
                            and expected_assistant_message_id != context_id
                        ):
                            continue
                        expected_assistant_message_id = context_id
                        self.window.bind_turn_message_ids(
                            request_id=request_id,
                            client_message_id=client_message_id,
                            user_message_id=payload.get("user_message_id"),
                            assistant_message_id=context_id,
                        )
                    retrieval_setter = getattr(self.window, "set_retrieval_context", None)
                    if callable(retrieval_setter):
                        retrieval_setter(payload)
                    memories = payload.get("memories")
                    if isinstance(memories, list) and all(
                        isinstance(memory, dict) for memory in memories
                    ):
                        context_setter = getattr(self.window, "set_used_memories", None)
                        if callable(context_setter):
                            context_setter(memories)
                elif event_type == "assistant.delta":
                    delta_assistant_id = payload.get("assistant_message_id")
                    if delta_assistant_id not in (None, ""):
                        delta_message_id = str(delta_assistant_id)
                        if (
                            expected_assistant_message_id is not None
                            and expected_assistant_message_id != delta_message_id
                        ):
                            continue
                        expected_assistant_message_id = delta_message_id
                    sequence_value = payload.get("sequence")
                    sequence = (
                        sequence_value
                        if isinstance(sequence_value, int)
                        and not isinstance(sequence_value, bool)
                        else None
                    )
                    delta = str(payload.get("delta") or "")
                    if delta:
                        self.window.append_delta(
                            delta,
                            request_id=request_id,
                            assistant_message_id=expected_assistant_message_id,
                            sequence=sequence,
                        )
                elif event_type == "assistant.completed":
                    message_value = payload.get("message")
                    completed_message = message_value if isinstance(message_value, dict) else {}
                    payload_message_id = payload.get("message_id")
                    payload_assistant_id = payload.get("assistant_message_id")
                    nested_assistant_id = completed_message.get("id")
                    if (
                        payload_assistant_id not in (None, "")
                        and payload_message_id not in (None, "")
                        and str(payload_assistant_id) != str(payload_message_id)
                    ) or (
                        payload_message_id not in (None, "")
                        and nested_assistant_id not in (None, "")
                        and str(payload_message_id) != str(nested_assistant_id)
                    ) or (
                        payload_assistant_id not in (None, "")
                        and nested_assistant_id not in (None, "")
                        and str(payload_assistant_id) != str(nested_assistant_id)
                    ):
                        continue
                    canonical_id_value = (
                        payload_assistant_id
                        or payload_message_id
                        or nested_assistant_id
                        or expected_assistant_message_id
                    )
                    if canonical_id_value in (None, ""):
                        continue
                    completed_message_id = str(canonical_id_value)
                    if (
                        expected_assistant_message_id is not None
                        and expected_assistant_message_id != completed_message_id
                    ):
                        continue
                    expected_assistant_message_id = completed_message_id
                    conversation_id = payload.get("conversation_id")
                    if conversation_id:
                        self._active_conversation_id = str(conversation_id)
                    content = str(completed_message.get("content") or "")
                    if not self.window.complete_assistant_message(
                        content,
                        request_id=request_id,
                        assistant_message_id=completed_message_id,
                    ):
                        continue
                    metrics_value = payload.get("metrics", payload.get("generation_metrics"))
                    metrics = dict(metrics_value) if isinstance(metrics_value, dict) else {}
                    metrics.setdefault("request_id", request_id)
                    metrics.setdefault("finish_reason", payload.get("finish_reason"))
                    metrics.setdefault("interrupted", False)
                    if callable(metrics_setter):
                        metrics_setter(metrics)
                    completed = True
                elif event_type == "error":
                    code = str(payload.get("code") or "")
                    error_message = str(payload.get("message") or "오류가 발생했습니다.")
                    if code == "CONVERSATION_NOT_FOUND":
                        self._active_conversation_id = None
                    details_value = payload.get("details")
                    details = details_value if isinstance(details_value, dict) else {}
                    metrics_value = payload.get("metrics", details.get("metrics"))
                    metrics = dict(metrics_value) if isinstance(metrics_value, dict) else {}
                    interrupted = bool(metrics.get("interrupted")) or (
                        str(details.get("state") or payload.get("state") or "")
                        == "interrupted"
                    ) or code == "LLM_STREAM_INTERRUPTED"
                    metrics.setdefault("request_id", request_id)
                    metrics.setdefault(
                        "finish_reason", "error" if interrupted else "rejected"
                    )
                    metrics["interrupted"] = interrupted
                    if callable(metrics_setter):
                        metrics_setter(metrics)
                    self.window.append_delta(
                        f"응답 오류: {error_message}",
                        request_id=request_id,
                        assistant_message_id=expected_assistant_message_id,
                    )
                    self.window.finish_assistant_message(
                        request_id=request_id,
                        assistant_message_id=expected_assistant_message_id,
                    )
                    completed = True
                elif event_type == "chat.cancelled":
                    metrics_value = payload.get("metrics")
                    metrics = dict(metrics_value) if isinstance(metrics_value, dict) else {}
                    metrics.setdefault("request_id", request_id)
                    metrics.setdefault("finish_reason", "cancelled")
                    metrics.setdefault("interrupted", True)
                    if callable(metrics_setter):
                        metrics_setter(metrics)
                    self.window.append_delta(
                        "응답 생성이 취소되었습니다.",
                        request_id=request_id,
                        assistant_message_id=expected_assistant_message_id,
                    )
                    self.window.finish_assistant_message(
                        request_id=request_id,
                        assistant_message_id=expected_assistant_message_id,
                    )
                    completed = True
            if not completed:
                self._mark_connection_lost()
                if callable(metrics_setter):
                    metrics_setter(
                        {
                            "request_id": request_id,
                            "finish_reason": "unknown",
                            "interrupted": True,
                        }
                    )
                self.window.append_delta(
                    "서버가 완료 신호 없이 연결을 종료해 답변을 완료하지 못했습니다.",
                    request_id=request_id,
                    assistant_message_id=expected_assistant_message_id,
                )
            if (
                completed
                and self.window.history_window is not None
                and self.window.history_window.isVisible()
            ):
                await self._refresh_conversations()
                if self._active_conversation_id is not None:
                    await self._load_conversation(self._active_conversation_id, update_main=False)
        except Exception as exc:
            self._mark_connection_lost()
            if callable(metrics_setter):
                metrics_setter(
                    {
                        "request_id": request_id,
                        "finish_reason": "unknown",
                        "interrupted": True,
                    }
                )
            self.window.append_delta(
                "서버 연결이 끊겨 답변을 완료하지 못했습니다.",
                request_id=request_id,
                assistant_message_id=expected_assistant_message_id,
            )
            self.window.show_error(f"서버 연결이 끊겼습니다: {exc}")
        finally:
            if not completed:
                self.window.finish_assistant_message(
                    request_id=request_id,
                    assistant_message_id=expected_assistant_message_id,
                )


def run(*, gateway_endpoint: str | None = None) -> None:
    app = NivelleLinkApplication(gateway_endpoint=gateway_endpoint)
    loop = qasync.QEventLoop(app.qt)
    asyncio.set_event_loop(loop)
    with loop:
        app._startup_task = loop.create_task(app.start())
        try:
            loop.run_forever()
        finally:
            loop.run_until_complete(app.shutdown())
