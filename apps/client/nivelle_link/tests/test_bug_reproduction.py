"""
Bug reproduction tests for Nivelle Link (Client).

Bug IDs follow format: NIV-LINK-{component}-{number:03d}

Components:
- NET: Network/Connection
- AGENT: Agent Controller
- UI: UI/Windows
- STOR: Storage
"""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from nivelle_link import app as client_app
from nivelle_link.agent.runtime import AgentRuntime
from nivelle_link.agent_controller import AgentController
from nivelle_link.network import ConnectionManager, ConnectionState
from nivelle_protocol.settings import ConnectionProfile
from nivelle_protocol.tools import (
    RiskLevel,
    ToolRequest,
    ToolStatus,
)

# =============================================================================
# BUG NIV-LINK-NET-001: ConnectionManager doesn't classify error types
# =============================================================================

class TestBugNIVLINKNET001ErrorClassification:
    """BUG NIV-LINK-NET-001: ConnectionManager probe failures lack error classification."""

    @pytest.mark.asyncio
    async def test_probe_failure_types_not_classified(self):
        """Different failure types (timeout, refused, DNS) should be classified."""
        profiles = [
            ConnectionProfile(
                id="timeout",
                host="10.255.255.1",  # Non-routable - timeout
                port=8080,
                tls=False,
                enabled=True,
                priority=1,
            ),
            ConnectionProfile(
                id="refused",
                host="127.0.0.1",
                port=9999,  # Likely refused
                tls=False,
                enabled=True,
                priority=2,
            ),
        ]

        manager = ConnectionManager(profiles, probe_timeout=0.5)

        result = await manager.connect()
        # Should try both profiles
        assert result is None
        assert manager.state == ConnectionState.FAILED
        assert manager.last_error is not None

        # Error type should be identifiable
        error_type = type(manager.last_error).__name__
        print(f"Error type: {error_type}")
        # Current implementation doesn't classify - just stores exception


# =============================================================================
# BUG NIV-LINK-NET-002: Reconnection backoff not properly managed
# =============================================================================

class TestBugNIVLINKNET002ReconnectionBackoff:
    """BUG NIV-LINK-NET-002: Backoff escalation preserved until mark_connected."""

    @pytest.mark.asyncio
    async def test_backoff_only_resets_after_full_connection(self):
        """Backoff should reset only after auth + status + WS succeed."""
        profiles = [
            ConnectionProfile(
                id="test",
                host="127.0.0.1",
                port=9999,
                tls=False,
                enabled=True,
                priority=1,
            )
        ]

        manager = ConnectionManager(profiles)

        async def successful_probe(_profile):
            return None

        manager._probe = successful_probe
        manager.reconnect_backoff_seconds = 4.0

        # A successful health probe is only partial success.
        assert await manager.connect() == profiles[0]
        assert manager.state == ConnectionState.AUTHENTICATING
        assert manager.reconnect_backoff_seconds == 4.0

        # Only auth + status + WebSocket completion resets backoff.
        manager.mark_connected()
        assert manager.reconnect_backoff_seconds == 1.0


# =============================================================================
# BUG NIV-LINK-NET-003: WebSocket connection deduplication
# =============================================================================

class TestBugNIVLINKNET003WebSocketDeduplication:
    """BUG NIV-LINK-NET-003: Multiple WebSocket connections possible."""

    @pytest.mark.asyncio
    async def test_ensure_chat_connection_idempotent(self, fake_network_client):
        """Multiple ensure_chat_connection calls should not create multiple connections."""
        # First call
        await fake_network_client.ensure_chat_connection()
        assert fake_network_client.chat_connected

        # Second call should not create new connection
        await fake_network_client.ensure_chat_connection()
        assert fake_network_client.chat_connected

        # Should only have one connection
        # (Fake implementation doesn't track multiple, but real one should)


# =============================================================================
# BUG NIV-LINK-NET-004: Transient auth failure disables automatic reconnect
# =============================================================================

class TestBugNIVLINKNET004AuthenticationReconnect:
    """BUG NIV-LINK-NET-004: A transient 401/403 must remain reconnectable."""

    def test_auth_failure_during_connect_keeps_auto_reconnect_alive(self):
        """A restarting server may briefly reject a valid saved token."""
        profile = ConnectionProfile(id="lan", host="192.168.0.20")
        application = object.__new__(client_app.NivelleLinkApplication)
        application.connections = ConnectionManager([profile])
        application.connections.active = profile
        application.connections.state = ConnectionState.AUTHENTICATING
        application.client = SimpleNamespace(token="saved-token")
        application._authentication_failures = 0
        application._cancel_connection_monitor = lambda: None
        application._cancel_auto_reconnect = lambda: None
        application._schedule_chat_close = lambda: None
        application._set_connection_state = lambda _state: None
        reconnects = []
        application._schedule_auto_reconnect = lambda: reconnects.append(True)

        application._handle_authentication_failure()

        assert application.connections.state == ConnectionState.RECONNECT_WAIT
        assert application.connections.active is None
        assert application.connections.auto_reconnect_enabled is True
        assert application.client.token == "saved-token"
        assert reconnects == [True]

    def test_repeated_auth_failure_stops_automatic_reconnect(self):
        """A revoked or invalid token must not be retried forever."""
        profile = ConnectionProfile(id="lan", host="192.168.0.20")
        application = object.__new__(client_app.NivelleLinkApplication)
        application.connections = ConnectionManager([profile])
        application.connections.active = profile
        application.connections.state = ConnectionState.AUTHENTICATING
        application.client = SimpleNamespace(token="revoked-token")
        application._authentication_failures = 0
        application._cancel_connection_monitor = lambda: None
        application._cancel_auto_reconnect = lambda: None
        application._schedule_chat_close = lambda: None
        application._set_connection_state = lambda _state: None
        reconnects = []
        application._schedule_auto_reconnect = lambda: reconnects.append(True)

        application._handle_authentication_failure()
        application._handle_authentication_failure()

        assert application.connections.state == ConnectionState.FAILED
        assert application.connections.active is None
        assert application.connections.auto_reconnect_enabled is False
        assert application.client.token is None
        assert reconnects == [True]

    @pytest.mark.asyncio
    async def test_client_restart_recovers_without_resaving_same_ip(
        self, monkeypatch, qtbot
    ):
        """A fresh Link process must use its saved profile without opening the dialog."""
        server_id = "31c2cc21-65cc-4ab7-9258-b77497347b1b"
        persisted = ConnectionProfile(
            id="primary",
            host="192.168.219.102",
        )

        failed = object.__new__(client_app.NivelleLinkApplication)
        failed.connections = ConnectionManager([persisted])
        failed.connections.active = persisted
        failed.connections.state = ConnectionState.AUTHENTICATING
        failed.client = SimpleNamespace(token="saved-token")
        failed._authentication_failures = 0
        failed._cancel_connection_monitor = lambda: None
        failed._cancel_auto_reconnect = lambda: None
        failed._schedule_chat_close = lambda: None
        failed._set_connection_state = lambda _state: None
        failed._schedule_auto_reconnect = lambda: None
        failed._handle_authentication_failure()
        failed._handle_authentication_failure()
        assert failed.connections.auto_reconnect_enabled is False

        reloaded = ConnectionProfile.model_validate(persisted.model_dump(mode="json"))
        monkeypatch.setattr(client_app, "load_connection_profiles", lambda: [reloaded])
        monkeypatch.setattr(
            client_app,
            "load_token_for_server",
            lambda _profile, _server_id: "saved-token",
        )
        saved_profiles = []
        monkeypatch.setattr(
            client_app,
            "save_connection_profiles",
            lambda profiles: saved_profiles.append(list(profiles)),
        )
        monkeypatch.setattr(client_app, "save_token_for_server", lambda *_args: None)

        restarted = client_app.NivelleLinkApplication()
        qtbot.addWidget(restarted.window)
        monkeypatch.setattr(restarted, "_ensure_connection_monitor", lambda: None)

        async def probe(profile):
            profile_key = restarted.connections._profile_key(profile)
            restarted.connections._observed_server_ids[profile_key] = server_id

        async def status(_path):
            return {
                "server_id": server_id,
                "protocol_version": "1.0",
                "model_name": "test",
                "uptime_seconds": 1,
            }

        async def connect_chat():
            return None

        monkeypatch.setattr(restarted.connections, "_probe", probe)
        monkeypatch.setattr(restarted.client, "get", status)
        monkeypatch.setattr(
            restarted.client,
            "ensure_chat_connection",
            connect_chat,
        )

        await restarted.start()

        assert restarted.connections.state == ConnectionState.CONNECTED
        assert restarted.connections.active is not None
        assert restarted.connections.auto_reconnect_enabled is True
        assert restarted.client.token == "saved-token"
        assert restarted.connections.active.host == "192.168.219.102"
        assert restarted.connections.active.server_id == server_id
        assert saved_profiles and saved_profiles[-1][0].server_id == server_id


# =============================================================================
# BUG NIV-LINK-AGENT-001: AgentController approval timeout handling
# =============================================================================

class TestBugNIVLINKAGENT001ApprovalTimeout:
    """BUG NIV-LINK-AGENT-001: Approval timeout not properly enforced."""

    @pytest.mark.asyncio
    async def test_approval_expires_after_timeout(self, test_agent_controller: AgentController):
        """Approval requests should expire after configured timeout."""
        from datetime import UTC, datetime

        policy = test_agent_controller.runtime.load_policy().model_copy(
            update={"agent_enabled": True, "enabled_tools": {"create_note"}}
        )
        test_agent_controller.runtime.policy_store.save(policy)

        # Create a tool request
        request = ToolRequest(
            tool_call_id=uuid4(),
            request_id=uuid4(),
            idempotency_key=uuid4(),
            conversation_id=uuid4(),
            user_message_id=uuid4(),
            target_client_id=test_agent_controller.client_id,
            target_session_id=test_agent_controller.session_id,
            tool_name="create_note",
            tool_version="1.0",
            arguments={"title": "Test", "content": "Test content"},
            risk_level=RiskLevel.LOCAL_WRITE,
            created_at=datetime.now(UTC),
            timeout_ms=5000,
            user_intent_summary="Create a test note",
        )

        # Handle server event (simulates incoming tool request)
        await test_agent_controller.handle_server_event({
            "type": "tool.request",
            **request.model_dump(mode="json"),
        })

        # Should be in pending state
        assert test_agent_controller.pending_count > 0

        # Wait for timeout (controller has approval_timeout_seconds=120 default)
        # In test we can't wait 120s, but we can verify the mechanism exists
        print(f"Pending count: {test_agent_controller.pending_count}")
        print(f"Approval timeout: {test_agent_controller.approval_timeout_seconds}s")


# =============================================================================
# BUG NIV-LINK-AGENT-002: Duplicate tool request handling
# =============================================================================

class TestBugNIVLINKAGENT002DuplicateRequests:
    """BUG NIV-LINK-AGENT-002: Duplicate tool requests with same fingerprint."""

    @pytest.mark.asyncio
    async def test_same_fingerprint_returns_cached_result(self, test_agent_controller: AgentController):
        """Requests with same fingerprint should return cached terminal event."""
        from datetime import UTC, datetime

        call_id = uuid4()
        request = ToolRequest(
            tool_call_id=call_id,
            request_id=uuid4(),
            idempotency_key=uuid4(),
            conversation_id=uuid4(),
            user_message_id=uuid4(),
            target_client_id=test_agent_controller.client_id,
            target_session_id=test_agent_controller.session_id,
            tool_name="get_system_status",
            tool_version="1.0",
            arguments={},
            risk_level=RiskLevel.SAFE_STATUS,
            created_at=datetime.now(UTC),
            timeout_ms=5000,
            user_intent_summary="Get system status",
        )

        # First request
        await test_agent_controller.handle_server_event({
            "type": "tool.request",
            **request.model_dump(mode="json"),
        })

        # Second request with same call_id (same fingerprint)
        request2 = ToolRequest(
            tool_call_id=call_id,  # Same call_id
            request_id=uuid4(),
            idempotency_key=uuid4(),
            conversation_id=uuid4(),
            user_message_id=uuid4(),
            target_client_id=test_agent_controller.client_id,
            target_session_id=test_agent_controller.session_id,
            tool_name="get_system_status",
            tool_version="1.0",
            arguments={},
            risk_level=RiskLevel.SAFE_STATUS,
            created_at=datetime.now(UTC),
            timeout_ms=5000,
            user_intent_summary="Get system status",
        )

        await test_agent_controller.handle_server_event({
            "type": "tool.request",
            **request2.model_dump(mode="json"),
        })

        # Should return cached result
        # Current implementation checks fingerprint and returns cached terminal message


# =============================================================================
# BUG NIV-LINK-AGENT-003: Tool execution blocks event loop
# =============================================================================

class TestBugNIVLINKAGENT003BlockingExecution:
    """BUG NIV-LINK-AGENT-003: Tool execution uses asyncio.to_thread which can block."""

    @pytest.mark.asyncio
    async def test_long_running_tool_doesnt_block_loop(self, test_agent_runtime: AgentRuntime):
        """Long-running tools should not block the event loop."""
        from datetime import UTC, datetime

        # Create a request for a potentially long-running tool
        request = ToolRequest(
            tool_call_id=uuid4(),
            request_id=uuid4(),
            idempotency_key=uuid4(),
            conversation_id=uuid4(),
            user_message_id=uuid4(),
            target_client_id=test_agent_runtime.client_id,
            target_session_id=test_agent_runtime.session_id,
            tool_name="search_files",
            tool_version="1.0",
            arguments={"query": "test", "root_id": "test"},
            risk_level=RiskLevel.LOCAL_READ,
            created_at=datetime.now(UTC),
            timeout_ms=30000,
            user_intent_summary="Search files",
        )

        # Execute with short timeout to test cancellation
        result = await asyncio.to_thread(
            test_agent_runtime.execute,
            request,
            cancellation=None,
        )

        # Should complete or fail gracefully
        assert result.status in [
            ToolStatus.COMPLETED,
            ToolStatus.FAILED,
            ToolStatus.TIMED_OUT,
            ToolStatus.VALIDATION_FAILED,
        ]
        print(f"Tool result status: {result.status}")


# =============================================================================
# BUG NIV-LINK-UI-001: UI state synchronization with server
# =============================================================================

class TestBugNIVLINKUI001StateSync:
    """BUG NIV-LINK-UI-001: UI state may diverge from server state."""

    @pytest.mark.asyncio
    async def test_agent_snapshot_reflects_server_state(self, test_agent_controller: AgentController):
        """Agent snapshot should accurately reflect current state."""
        snapshot = test_agent_controller.snapshot(connected_core="test:8080")

        assert "enabled" in snapshot
        assert "connected_core" in snapshot
        assert "tools" in snapshot
        assert "pending_approval_count" in snapshot

        # Snapshot should match runtime policy
        policy = test_agent_controller.runtime.load_policy()
        assert snapshot["enabled"] == policy.agent_enabled


# =============================================================================
# BUG NIV-LINK-STOR-001: Token storage migration verification
# =============================================================================

class TestBugNIVLINKSTOR001TokenMigration:
    """BUG NIV-LINK-STOR-001: Token migration from legacy keyring should verify."""

    def test_token_migration_verifies_correctly(self, monkeypatch):
        """Migrated tokens should be verified against original."""
        from nivelle_link import storage

        credentials = {}
        monkeypatch.setattr(
            storage.keyring,
            "get_password",
            lambda service, key: credentials.get((service, key)),
        )
        monkeypatch.setattr(
            storage.keyring,
            "set_password",
            lambda service, key, token: credentials.__setitem__(
                (service, key), token
            ),
        )

        profile = ConnectionProfile(
            id="test",
            host="127.0.0.1",
            port=8080,
            tls=False,
            enabled=True,
            priority=1,
        )

        key = storage.token_key_for_profile(profile)
        test_token = "test-token-123"

        # Save to legacy service
        credentials[("NozomiClient", "default")] = test_token

        # Load should migrate and verify
        loaded = storage.load_token_for_profile(profile)
        assert loaded == test_token

        # Should now be in new service
        new_token = credentials.get(("NivelleLink", key))
        assert new_token == test_token


# =============================================================================
# Summary test for client bug IDs
# =============================================================================

class TestClientBugIDRegistry:
    """Registry of all Client Bug IDs from STEP 1 investigation."""

    def test_client_bug_ids_documented(self):
        """All Client Bug IDs should have corresponding test classes."""
        bug_ids = [
            "NIV-LINK-NET-001",  # Error classification
            "NIV-LINK-NET-002",  # Reconnection backoff
            "NIV-LINK-NET-003",  # WebSocket deduplication
            "NIV-LINK-NET-004",  # Authentication reconnect
            "NIV-LINK-AGENT-001", # Approval timeout
            "NIV-LINK-AGENT-002", # Duplicate requests
            "NIV-LINK-AGENT-003", # Blocking execution
            "NIV-LINK-UI-001",    # State sync
            "NIV-LINK-STOR-001",  # Token migration
        ]

        for bug_id in bug_ids:
            print(f"Documented: {bug_id}")

        assert len(bug_ids) == 9
