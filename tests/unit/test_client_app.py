import asyncio
from typing import Any

import httpx
import pytest
from nivelle_link import app as client_app
from nivelle_link import network as client_network
from nivelle_link.network import ConnectionState
from nivelle_link.windows import MemoryArchiveWindow, PersonaWindow
from nivelle_protocol.settings import ConnectionProfile
from PySide6.QtWidgets import QDialog


@pytest.mark.asyncio
async def test_first_run_opens_connection_dialog(monkeypatch: pytest.MonkeyPatch, qtbot: Any) -> None:
    opened: list[bool] = []

    class RejectedConnectionDialog:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            opened.append(True)

        def exec(self) -> int:
            return int(QDialog.DialogCode.Rejected)

    monkeypatch.setattr(client_app, "load_connection_profiles", lambda: [])
    monkeypatch.setattr(client_app, "ConnectionDialog", RejectedConnectionDialog)
    application = client_app.NivelleLinkApplication()
    qtbot.addWidget(application.window)

    await application.start()

    assert opened == [True]
    assert application.window.status.text() == "오프라인"


@pytest.mark.asyncio
async def test_auto_reconnect_reports_retry_and_restores_profile(
    monkeypatch: pytest.MonkeyPatch, qtbot: Any
) -> None:
    profile = ConnectionProfile(id="lan", host="192.168.0.20")
    monkeypatch.setattr(client_app, "load_connection_profiles", lambda: [profile])
    application = client_app.NivelleLinkApplication()
    qtbot.addWidget(application.window)
    restored: list[ConnectionProfile] = []

    async def reconnect_delays():
        yield 1.0
        application.connections.active = profile

    async def connected(value: ConnectionProfile) -> None:
        restored.append(value)

    monkeypatch.setattr(application.connections, "reconnect_delays", reconnect_delays)
    monkeypatch.setattr(application, "_connected", connected)

    await application._auto_reconnect_loop()

    assert application._reconnect_attempts == 1
    assert restored == [profile]
    assert application.window.status.text() == "재연결 중… (1회)"


@pytest.mark.asyncio
async def test_reconnect_rearms_after_post_health_connection_failure(
    monkeypatch: pytest.MonkeyPatch, qtbot: Any
) -> None:
    profile = ConnectionProfile(id="lan", host="192.168.0.20")
    monkeypatch.setattr(client_app, "load_connection_profiles", lambda: [profile])
    application = client_app.NivelleLinkApplication()
    qtbot.addWidget(application.window)
    application.connections.active = None
    application.connections.state = ConnectionState.RECONNECT_WAIT
    application.connections.auto_reconnect_enabled = True
    rearmed: list[bool] = []
    monkeypatch.setattr(
        application, "_schedule_auto_reconnect", lambda: rearmed.append(True)
    )

    async def completed_attempt() -> None:
        return None

    task = asyncio.create_task(completed_attempt())
    application._auto_reconnect_task = task
    await task
    application._auto_reconnect_finished(task)

    assert application._auto_reconnect_task is None
    assert rearmed == [True]


def test_manual_disconnect_preserves_draft_and_locks_open_management_windows(
    monkeypatch: pytest.MonkeyPatch, qtbot: Any
) -> None:
    profile = ConnectionProfile(id="lan", host="192.168.0.20")
    monkeypatch.setattr(client_app, "load_connection_profiles", lambda: [profile])
    application = client_app.NivelleLinkApplication()
    qtbot.addWidget(application.window)
    persona = PersonaWindow()
    memory = MemoryArchiveWindow()
    qtbot.addWidget(persona)
    qtbot.addWidget(memory)
    application.window.persona_window = persona
    application.window.memory_window = memory
    application.connections.active = profile
    application.connections.mark_connected()
    application.client.token = "token"
    application._set_connection_state(ConnectionState.CONNECTED)
    application.window.input.setPlainText("보존할 초안")

    application._disconnect_manually()

    assert application.connections.state == ConnectionState.MANUAL_OFFLINE
    assert application.connections.auto_reconnect_enabled is False
    assert application.window.input.toPlainText() == "보존할 초안"
    assert not persona.save_button.isEnabled()
    assert not memory.create_button.isEnabled()
    assert not application.window.disconnect_action.isEnabled()

    application.connections.active = profile
    application.connections.mark_connected()
    application.client.token = "token"
    application._set_connection_state(ConnectionState.CONNECTED)
    assert persona.save_button.isEnabled()
    assert memory.create_button.isEnabled()


def test_incompatible_protocol_is_visible_and_blocks_chat_preflight(
    monkeypatch: pytest.MonkeyPatch, qtbot: Any
) -> None:
    profile = ConnectionProfile(id="lan", host="192.168.0.20")
    monkeypatch.setattr(client_app, "load_connection_profiles", lambda: [profile])
    application = client_app.NivelleLinkApplication()
    qtbot.addWidget(application.window)
    application.connections.active = profile
    application.connections.mark_connected()
    application.client.token = "token"
    application._apply_server_status(
        {"protocol_version": "2.0", "model_name": "test", "uptime_seconds": 1}
    )
    errors: list[str] = []
    monkeypatch.setattr(application.window, "show_error", errors.append)

    application._schedule_send("보내면 안 됨")

    assert application._send_task is None
    assert application.window.input.toPlainText() == "보내면 안 됨"
    assert errors and "주 버전" in errors[0]
    assert "호환성" in application.window.compact_status.text()


@pytest.mark.asyncio
async def test_transient_auth_failure_reconnects_and_preserves_token(
    monkeypatch: pytest.MonkeyPatch, qtbot: Any
) -> None:
    profile = ConnectionProfile(id="lan", host="192.168.0.20")
    monkeypatch.setattr(client_app, "load_connection_profiles", lambda: [profile])
    monkeypatch.setattr(client_app, "load_token_for_profile", lambda _profile: "expired")
    application = client_app.NivelleLinkApplication()
    qtbot.addWidget(application.window)
    application.connections.active = profile
    errors: list[str] = []
    reconnects: list[bool] = []
    status_calls = 0
    chat_connections = 0
    monkeypatch.setattr(application.window, "show_error", errors.append)
    monkeypatch.setattr(
        application, "_schedule_auto_reconnect", lambda: reconnects.append(True)
    )
    monkeypatch.setattr(application, "_ensure_connection_monitor", lambda: None)

    async def get(_path: str) -> object:
        nonlocal status_calls
        status_calls += 1
        if status_calls == 1:
            request = httpx.Request("GET", "http://192.168.0.20:8765/api/v1/status")
            response = httpx.Response(401, request=request)
            raise httpx.HTTPStatusError("unauthorized", request=request, response=response)
        return {
            "protocol_version": "1.0",
            "model_name": "test",
            "uptime_seconds": 1,
        }

    async def ensure_chat_connection() -> None:
        nonlocal chat_connections
        chat_connections += 1

    monkeypatch.setattr(application.client, "get", get)
    monkeypatch.setattr(application.client, "ensure_chat_connection", ensure_chat_connection)

    await application._connected(profile)

    assert application.connections.state == ConnectionState.RECONNECT_WAIT
    assert application.connections.active is None
    assert application.connections.auto_reconnect_enabled is True
    assert application.client.token == "expired"
    assert application.window.status.text() == "재연결 중… (0회)"
    assert reconnects == [True]
    assert errors and "자동으로 다시 연결" in errors[-1]

    application.connections.active = profile
    await application._connected(profile)

    assert application.connections.state == ConnectionState.CONNECTED
    assert application.connections.auto_reconnect_enabled is True
    assert application.client.token == "expired"
    assert application._authentication_failures == 0
    assert status_calls == 2
    assert chat_connections == 1


@pytest.mark.asyncio
async def test_repeated_auth_failure_disables_reconnect_without_keyring_deletion(
    monkeypatch: pytest.MonkeyPatch, qtbot: Any
) -> None:
    profile = ConnectionProfile(id="lan", host="192.168.0.20")
    monkeypatch.setattr(client_app, "load_connection_profiles", lambda: [profile])
    monkeypatch.setattr(client_app, "load_token_for_profile", lambda _profile: "revoked")
    application = client_app.NivelleLinkApplication()
    qtbot.addWidget(application.window)
    application.connections.active = profile
    reconnects: list[bool] = []
    errors: list[str] = []
    monkeypatch.setattr(application.window, "show_error", errors.append)
    monkeypatch.setattr(
        application, "_schedule_auto_reconnect", lambda: reconnects.append(True)
    )

    async def get(_path: str) -> object:
        request = httpx.Request("GET", "http://192.168.0.20:8765/api/v1/status")
        response = httpx.Response(401, request=request)
        raise httpx.HTTPStatusError("unauthorized", request=request, response=response)

    monkeypatch.setattr(application.client, "get", get)

    await application._connected(profile)
    application.connections.active = profile
    await application._connected(profile)

    assert application.connections.state == ConnectionState.FAILED
    assert application.connections.active is None
    assert application.connections.auto_reconnect_enabled is False
    assert application.client.token is None
    assert reconnects == [True]
    assert errors and "새 페어링" in errors[-1]


@pytest.mark.asyncio
async def test_missing_token_and_incomplete_pairing_clears_stale_active_profile(
    monkeypatch: pytest.MonkeyPatch, qtbot: Any
) -> None:
    profile = ConnectionProfile(id="lan", host="192.168.0.20")
    monkeypatch.setattr(client_app, "load_connection_profiles", lambda: [profile])
    monkeypatch.setattr(client_app, "load_token_for_profile", lambda _profile: None)
    application = client_app.NivelleLinkApplication()
    qtbot.addWidget(application.window)
    application.connections.active = profile

    async def pairing_not_completed(_profile: ConnectionProfile) -> bool:
        return False

    monkeypatch.setattr(application, "_pair_if_required", pairing_not_completed)

    await application._connected(profile)

    assert application.connections.state == ConnectionState.FAILED
    assert application.connections.active is None
    assert application.connections.auto_reconnect_enabled is False
    assert application.client.token is None


@pytest.mark.asyncio
async def test_server_restart_recovers_after_network_and_transient_auth_failures(
    monkeypatch: pytest.MonkeyPatch, qtbot: Any
) -> None:
    profile = ConnectionProfile(id="lan", host="192.168.0.20")
    monkeypatch.setattr(client_app, "load_connection_profiles", lambda: [profile])
    monkeypatch.setattr(client_app, "load_token_for_profile", lambda _profile: "saved")
    monkeypatch.setattr(client_network.random, "uniform", lambda _start, _end: 0.0)
    application = client_app.NivelleLinkApplication()
    qtbot.addWidget(application.window)
    application.connections.active = profile
    application.connections.mark_connected()
    application.connections.reconnect_backoff_seconds = 0.001
    application.client.token = "saved"
    application._schedule_chat_close = lambda: None
    application._ensure_connection_monitor = lambda: None
    errors: list[str] = []
    monkeypatch.setattr(application.window, "show_error", errors.append)
    connected_event = asyncio.Event()
    set_connection_state = application._set_connection_state

    def record_connection_state(state: ConnectionState | str) -> None:
        set_connection_state(state)
        if state == ConnectionState.CONNECTED:
            connected_event.set()

    monkeypatch.setattr(application, "_set_connection_state", record_connection_state)

    probe_calls = 0
    status_calls = 0
    chat_connections = 0

    async def probe(_profile: ConnectionProfile) -> None:
        nonlocal probe_calls
        probe_calls += 1
        if probe_calls == 1:
            request = httpx.Request("GET", "http://192.168.0.20:8765/health")
            raise httpx.ConnectError("server restarting", request=request)

    async def get(_path: str) -> object:
        nonlocal status_calls
        status_calls += 1
        if status_calls == 1:
            request = httpx.Request("GET", "http://192.168.0.20:8765/api/v1/status")
            response = httpx.Response(401, request=request)
            raise httpx.HTTPStatusError("unauthorized", request=request, response=response)
        return {
            "protocol_version": "1.0",
            "model_name": "test",
            "uptime_seconds": 1,
        }

    async def ensure_chat_connection() -> None:
        nonlocal chat_connections
        chat_connections += 1

    monkeypatch.setattr(application.connections, "_probe", probe)
    monkeypatch.setattr(application.client, "get", get)
    monkeypatch.setattr(application.client, "ensure_chat_connection", ensure_chat_connection)

    application._mark_connection_lost()

    await asyncio.wait_for(connected_event.wait(), timeout=1)
    await asyncio.sleep(0)

    assert probe_calls == 3
    assert status_calls == 2
    assert chat_connections == 1
    assert application.connections.active is profile
    assert application.connections.state == ConnectionState.CONNECTED
    assert application.connections.auto_reconnect_enabled is True
    assert application.connections.reconnect_backoff_seconds == 1.0
    assert application.client.token == "saved"
    assert application._authentication_failures == 0
    assert application._auto_reconnect_task is None
    assert application.connections.reconnect_task is None
    assert errors and "자동으로 다시 연결" in errors[-1]


@pytest.mark.asyncio
async def test_monitor_http_error_enters_reconnect_instead_of_connected(
    monkeypatch: pytest.MonkeyPatch, qtbot: Any
) -> None:
    profile = ConnectionProfile(id="lan", host="192.168.0.20")
    monkeypatch.setattr(client_app, "load_connection_profiles", lambda: [profile])
    application = client_app.NivelleLinkApplication()
    qtbot.addWidget(application.window)
    application.connections.active = profile
    application.connections.mark_connected()
    application.client.token = "token"

    async def no_wait(_delay: float) -> None:
        return None

    async def healthy() -> bool:
        return True

    async def get(_path: str) -> object:
        request = httpx.Request("GET", "http://192.168.0.20:8765/api/v1/status")
        response = httpx.Response(503, request=request)
        raise httpx.HTTPStatusError("unavailable", request=request, response=response)

    reconnects: list[bool] = []
    monkeypatch.setattr(client_app.asyncio, "sleep", no_wait)
    monkeypatch.setattr(application.connections, "check_active", healthy)
    monkeypatch.setattr(application.client, "get", get)
    monkeypatch.setattr(
        application, "_schedule_auto_reconnect", lambda: reconnects.append(True)
    )

    await application._monitor_connection()
    await asyncio.sleep(0)

    assert application.connections.state == ConnectionState.RECONNECT_WAIT
    assert application.connections.active is None
    assert reconnects == [True]


@pytest.mark.asyncio
async def test_monitor_keeps_last_status_after_one_transient_status_failure(
    monkeypatch: pytest.MonkeyPatch, qtbot: Any
) -> None:
    profile = ConnectionProfile(id="lan", host="192.168.0.20")
    monkeypatch.setattr(client_app, "load_connection_profiles", lambda: [profile])
    application = client_app.NivelleLinkApplication()
    qtbot.addWidget(application.window)
    application.connections.active = profile
    application.connections.mark_connected()
    application.client.token = "token"
    application._last_server_status = {"model_name": "last-known"}
    status_calls = 0

    async def no_wait(_delay: float) -> None:
        return None

    async def healthy() -> bool:
        return True

    async def get(_path: str) -> object:
        nonlocal status_calls
        status_calls += 1
        if status_calls == 1:
            request = httpx.Request("GET", "http://192.168.0.20:8765/api/v1/status")
            raise httpx.ConnectError("transient", request=request)
        application.connections.active = None
        return {
            "protocol_version": "1.0",
            "model_name": "recovered",
            "uptime_seconds": 1,
        }

    reconnects: list[bool] = []
    monkeypatch.setattr(client_app.asyncio, "sleep", no_wait)
    monkeypatch.setattr(application.connections, "check_active", healthy)
    monkeypatch.setattr(application.client, "get", get)
    monkeypatch.setattr(
        application, "_schedule_auto_reconnect", lambda: reconnects.append(True)
    )

    await application._monitor_connection()

    assert status_calls == 2
    assert reconnects == []
    assert application._status_failures == 0
    assert application._last_server_status["model_name"] == "recovered"


@pytest.mark.asyncio
async def test_unexpected_chat_socket_close_runs_real_reconnect_path_once(
    monkeypatch: pytest.MonkeyPatch, qtbot: Any
) -> None:
    profile = ConnectionProfile(id="lan", host="192.168.0.20")
    monkeypatch.setattr(client_app, "load_connection_profiles", lambda: [profile])
    monkeypatch.setattr(client_app, "load_token_for_profile", lambda _profile: "token")
    application = client_app.NivelleLinkApplication()
    qtbot.addWidget(application.window)
    persona = PersonaWindow()
    memory = MemoryArchiveWindow()
    qtbot.addWidget(persona)
    qtbot.addWidget(memory)
    application.window.persona_window = persona
    application.window.memory_window = memory
    application.connections.active = profile
    sockets: list[Any] = []
    live_sockets = 0
    maximum_live_sockets = 0
    status_checks = 0
    health_checks = 0

    class FakeSocket:
        def __init__(self) -> None:
            nonlocal live_sockets, maximum_live_sockets
            self.events: asyncio.Queue[str | None] = asyncio.Queue()
            self.closed = False
            live_sockets += 1
            maximum_live_sockets = max(maximum_live_sockets, live_sockets)

        def __aiter__(self) -> "FakeSocket":
            return self

        async def __anext__(self) -> str:
            value = await self.events.get()
            if value is None:
                self._mark_closed()
                raise StopAsyncIteration
            return value

        async def send(self, _payload: str) -> None:
            return None

        async def close(self) -> None:
            self._mark_closed()
            await self.events.put(None)

        def _mark_closed(self) -> None:
            nonlocal live_sockets
            if not self.closed:
                self.closed = True
                live_sockets -= 1

    async def connect(_url: str, **_kwargs: object) -> FakeSocket:
        socket = FakeSocket()
        sockets.append(socket)
        return socket

    async def status(_path: str) -> object:
        nonlocal status_checks
        status_checks += 1
        return {
            "protocol_version": "1.0",
            "model_name": "Qwen test",
            "uptime_seconds": 1,
        }

    def schedule_reconnect(on_connected: Any, *, on_attempt: Any = None) -> Any:
        nonlocal health_checks
        async def reconnect() -> None:
            nonlocal health_checks
            health_checks += 1
            if on_attempt is not None:
                on_attempt()
            application.connections.active = profile
            await on_connected(profile)

        return asyncio.create_task(reconnect())

    monkeypatch.setattr(client_network.websockets, "connect", connect)
    monkeypatch.setattr(application.client, "get", status)
    monkeypatch.setattr(application.connections, "schedule_reconnect", schedule_reconnect)

    await application._connected(profile)
    assert application.connections.state == ConnectionState.CONNECTED
    assert len(sockets) == 1 and live_sockets == 1
    application.window.input.setPlainText("재연결 중에도 보존할 초안")

    await sockets[0].events.put(None)
    for _ in range(100):
        await asyncio.sleep(0)
        if application.connections.state == ConnectionState.CONNECTED and len(sockets) == 2:
            break

    assert sockets[0].closed is True
    assert application.connections.state == ConnectionState.CONNECTED
    assert application._reconnect_attempts == 0
    assert health_checks == 1
    assert status_checks == 2
    assert len(sockets) == 2
    assert live_sockets == 1
    assert maximum_live_sockets == 1
    assert application.window.input.toPlainText() == "재연결 중에도 보존할 초안"
    assert application.window.persona_window is persona
    assert application.window.memory_window is memory

    application._cancel_connection_monitor()
    application._cancel_auto_reconnect()
    await application.client.close_chat_connection()


@pytest.mark.asyncio
async def test_shutdown_cancels_every_owned_task_and_closes_chat_once(
    monkeypatch: pytest.MonkeyPatch, qtbot: Any
) -> None:
    monkeypatch.setattr(client_app, "load_connection_profiles", lambda: [])
    application = client_app.NivelleLinkApplication()
    qtbot.addWidget(application.window)
    blocker = asyncio.Event()

    tasks = [asyncio.create_task(blocker.wait()) for _ in range(11)]
    application._startup_task = tasks[0]
    application._connection_task = tasks[1]
    application._send_task = tasks[2]
    application._history_refresh_task = tasks[3]
    application._conversation_load_task = tasks[4]
    application._monitor_task = tasks[5]
    application._auto_reconnect_task = tasks[6]
    application._admin_tasks.add(tasks[7])
    application._memory_tasks.add(tasks[8])
    application._history_tasks.add(tasks[9])
    application._persona_tasks.add(tasks[10])
    close_calls = 0

    async def close_chat_connection() -> None:
        nonlocal close_calls
        close_calls += 1

    monkeypatch.setattr(application.client, "close_chat_connection", close_chat_connection)

    await application.shutdown()

    assert all(task.done() for task in tasks)
    assert close_calls == 1
