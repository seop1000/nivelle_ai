"""Test configuration and fixtures for Nivelle Link tests."""

import asyncio
import tempfile
from collections.abc import Generator
from pathlib import Path
from uuid import uuid4

import pytest
from nivelle_link.agent.runtime import AgentRuntime
from nivelle_link.agent_controller import AgentController
from nivelle_link.network import ConnectionManager, NetworkClient
from nivelle_protocol.settings import ConnectionProfile


@pytest.fixture(scope="session")
def event_loop() -> Generator[asyncio.AbstractEventLoop, None, None]:
    """Create an event loop for the test session."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    """Create a temporary directory for test isolation."""
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


@pytest.fixture
def test_connection_profiles() -> list[ConnectionProfile]:
    """Create test connection profiles."""
    return [
        ConnectionProfile(
            id="test-local",
            host="127.0.0.1",
            port=8080,
            tls=False,
            enabled=True,
            priority=1,
        ),
        ConnectionProfile(
            id="test-remote",
            host="example.com",
            port=443,
            tls=True,
            enabled=True,
            priority=2,
        ),
    ]


@pytest.fixture
def connection_manager(test_connection_profiles) -> ConnectionManager:
    """Create a ConnectionManager for testing."""
    return ConnectionManager(test_connection_profiles)


@pytest.fixture
def network_client(connection_manager: ConnectionManager) -> NetworkClient:
    """Create a NetworkClient for testing."""
    return NetworkClient(connection_manager, token="test-token")


@pytest.fixture
def test_agent_runtime(temp_dir: Path) -> AgentRuntime:
    """Create an AgentRuntime for testing."""
    return AgentRuntime(
        data_directory=temp_dir,
        client_id=str(uuid4()),
        session_id=str(uuid4()),
        client_display_name="Test Client",
        link_version="1.0.0-test",
    )


@pytest.fixture
def test_agent_controller(
    test_agent_runtime: AgentRuntime,
) -> AgentController:
    """Create an AgentController for testing."""
    async def dummy_send(event: dict) -> None:
        pass

    def dummy_show(payload: dict) -> None:
        pass

    def dummy_update(call_id: str, status: str, message: str | None) -> None:
        pass

    return AgentController(
        data_directory=test_agent_runtime.data_directory,
        client_id=test_agent_runtime.client_id,
        session_id=test_agent_runtime.session_id,
        client_display_name=test_agent_runtime.client_display_name,
        link_version=test_agent_runtime.link_version,
        send_event=dummy_send,
        show_approval=dummy_show,
        update_status=dummy_update,
    )


class FakeConnectionManager:
    """Fake connection manager for testing without network."""

    def __init__(self, should_succeed: bool = True):
        self.should_succeed = should_succeed
        self.active = None
        self.state = "connected" if should_succeed else "failed"
        self.last_error = None
        self.consecutive_failures = 0
        self.probe_calls = 0
        self.connect_calls = 0

    async def connect(self):
        self.connect_calls += 1
        if self.should_succeed:
            from nivelle_protocol.settings import ConnectionProfile
            self.active = ConnectionProfile(
                id="fake",
                host="127.0.0.1",
                port=8080,
                tls=False,
                enabled=True,
                priority=1,
            )
            self.state = "connected"
            return self.active
        else:
            self.state = "failed"
            self.last_error = ConnectionError("Fake connection failed")
            return None

    async def check_active(self) -> bool:
        self.probe_calls += 1
        return self.should_succeed

    def base_url(self) -> str:
        if not self.active:
            raise RuntimeError("Not connected")
        return f"http://{self.active.host}:{self.active.port}"

    def mark_connected(self) -> None:
        self.state = "connected"
        self.consecutive_failures = 0

    def disconnect(self, *, manual: bool) -> None:
        self.active = None
        if manual:
            self.state = "manual_offline"
        else:
            self.state = "reconnect_wait"


class FakeNetworkClient:
    """Fake network client for testing without network."""

    def __init__(self, connection_manager: FakeConnectionManager):
        self.connections = connection_manager
        self.token = "fake-token"
        self.client_id = "fake-client-id"
        self.chat_connected = False
        self.agent_connected = False
        self.sent_events = []
        self.chat_disconnect_callback = None
        self.agent_disconnect_callback = None

    async def ensure_chat_connection(self) -> None:
        self.chat_connected = True

    async def ensure_agent_connection(self) -> None:
        self.agent_connected = True

    async def send_agent_event(self, event: dict) -> None:
        self.sent_events.append(event)

    async def close_connections(self) -> None:
        self.chat_connected = False
        self.agent_connected = False

    async def chat(self, request: dict):
        """Fake chat that yields a response."""
        yield {"type": "delta", "content": "Fake response"}
        yield {"type": "done", "request_id": request.get("request_id")}


@pytest.fixture
def fake_connection_manager() -> FakeConnectionManager:
    """Create a fake connection manager."""
    return FakeConnectionManager(should_succeed=True)


@pytest.fixture
def fake_network_client(fake_connection_manager: FakeConnectionManager) -> FakeNetworkClient:
    """Create a fake network client."""
    return FakeNetworkClient(fake_connection_manager)
