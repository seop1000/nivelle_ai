import asyncio
import json
from typing import Any
from uuid import uuid4

import httpx
import pytest
from nivelle_link import network
from nivelle_link.network import (
    ConnectionManager,
    ConnectionState,
    NetworkClient,
    ServerIdentityMismatchError,
)
from nivelle_protocol.settings import ConnectionProfile


def test_profiles_are_available_for_priority_selection() -> None:
    manager = ConnectionManager(
        [
            ConnectionProfile(id="two", host="b", priority=2),
            ConnectionProfile(id="one", host="a", priority=1),
        ]
    )
    assert sorted(manager.profiles, key=lambda item: item.priority)[0].id == "one"


@pytest.mark.asyncio
async def test_health_discovers_server_identity_and_rejects_pinned_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_id = "31c2cc21-65cc-4ab7-9258-b77497347b1b"

    class FakeAsyncClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get(self, url: str) -> httpx.Response:
            return httpx.Response(
                200,
                json={"status": "ok", "server_id": observed_id},
                request=httpx.Request("GET", url),
            )

    monkeypatch.setattr(network.httpx, "AsyncClient", FakeAsyncClient)
    unpinned = ConnectionProfile(id="new", host="192.168.0.20")
    manager = ConnectionManager([unpinned])

    assert await manager.connect() is unpinned
    assert manager.server_id_for(unpinned) == observed_id

    pinned = ConnectionProfile(
        id="pinned",
        host="192.168.0.20",
        server_id="74965be5-dce5-411c-9767-756f964a8e5c",
    )
    mismatch = ConnectionManager([pinned])

    assert await mismatch.connect() is None
    assert mismatch.active is None
    assert mismatch.state == ConnectionState.FAILED
    assert mismatch.auto_reconnect_enabled is False
    assert isinstance(mismatch.last_error, ServerIdentityMismatchError)


@pytest.mark.asyncio
async def test_identity_mismatch_on_one_address_fails_over_to_another_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server_id = "31c2cc21-65cc-4ab7-9258-b77497347b1b"

    class FakeAsyncClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get(self, url: str) -> httpx.Response:
            return httpx.Response(
                200,
                json={"status": "ok", "server_id": server_id},
                request=httpx.Request("GET", url),
            )

    monkeypatch.setattr(network.httpx, "AsyncClient", FakeAsyncClient)
    wrong = ConnectionProfile(
        id="old-lan",
        host="192.168.0.20",
        priority=1,
        server_id="74965be5-dce5-411c-9767-756f964a8e5c",
    )
    alternate = ConnectionProfile(
        id="vpn",
        type="vpn",
        host="core.vpn",
        priority=2,
        server_id=server_id,
    )
    manager = ConnectionManager([wrong, alternate])

    assert await manager.connect() is alternate
    assert manager.active is alternate
    assert manager.auto_reconnect_enabled is True


@pytest.mark.asyncio
async def test_active_health_probe_records_latency_and_marks_failed_profile_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    should_fail = False
    timeouts: list[float] = []

    class FakeAsyncClient:
        def __init__(self, **kwargs: object) -> None:
            timeouts.append(float(kwargs["timeout"]))

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get(self, url: str) -> httpx.Response:
            request = httpx.Request("GET", url)
            if should_fail:
                raise httpx.ConnectError("offline", request=request)
            return httpx.Response(200, json={"status": "ok"}, request=request)

    monkeypatch.setattr(network.httpx, "AsyncClient", FakeAsyncClient)
    profile = ConnectionProfile(id="server", host="192.168.0.20")
    manager = ConnectionManager(
        [profile], probe_timeout=4.25, health_interval=12.5
    )

    assert await manager.connect() is profile
    assert manager.last_checked_at is not None
    assert manager.last_latency_ms is not None
    assert manager.last_latency_ms >= 0
    assert timeouts == [4.25]
    assert manager.health_interval == 12.5

    should_fail = True
    assert await manager.check_active() is False
    assert manager.active is profile
    assert manager.consecutive_failures == 1
    assert await manager.check_active() is False
    assert manager.active is None
    assert manager.state == ConnectionState.RECONNECT_WAIT
    assert isinstance(manager.last_error, httpx.ConnectError)
    assert timeouts == [4.25, 4.25, 4.25]


def test_manual_disconnect_suppresses_automatic_reconnect() -> None:
    profile = ConnectionProfile(id="server", host="192.168.0.20")
    manager = ConnectionManager([profile])
    manager.active = profile
    manager.mark_connected()

    manager.disconnect(manual=True)

    assert manager.active is None
    assert manager.state == ConnectionState.MANUAL_OFFLINE
    assert manager.auto_reconnect_enabled is False


@pytest.mark.asyncio
async def test_reconnect_backoff_is_exponential_and_stops_after_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = ConnectionProfile(id="server", host="192.168.0.20")
    manager = ConnectionManager([profile])
    waits: list[float] = []
    attempts = 0

    async def sleep(delay: float) -> None:
        waits.append(delay)

    async def connect() -> ConnectionProfile | None:
        nonlocal attempts
        attempts += 1
        if attempts == 3:
            manager.active = profile
            return profile
        return None

    monkeypatch.setattr(network.asyncio, "sleep", sleep)
    monkeypatch.setattr(network.random, "uniform", lambda _start, _end: 0.0)
    monkeypatch.setattr(manager, "connect", connect)
    yielded = [delay async for delay in manager.reconnect_delays()]

    assert yielded == [1.0, 2.0, 4.0]
    assert waits == yielded
    assert attempts == 3
    assert manager.active is profile


@pytest.mark.asyncio
async def test_reconnect_backoff_persists_until_full_connection_is_confirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = ConnectionProfile(id="server", host="192.168.0.20")
    manager = ConnectionManager([profile])

    async def sleep(_delay: float) -> None:
        return None

    async def health_connects() -> ConnectionProfile:
        manager.active = profile
        return profile

    monkeypatch.setattr(network.asyncio, "sleep", sleep)
    monkeypatch.setattr(network.random, "uniform", lambda _start, _end: 0.0)
    monkeypatch.setattr(manager, "connect", health_connects)

    first = [delay async for delay in manager.reconnect_delays()]
    manager.disconnect(manual=False)  # authenticated status/WebSocket failed
    second = [delay async for delay in manager.reconnect_delays()]

    assert first == [1.0]
    assert second == [2.0]
    assert manager.reconnect_backoff_seconds == 4.0

    manager.mark_connected()
    assert manager.reconnect_backoff_seconds == 1.0


@pytest.mark.asyncio
async def test_scheduled_reconnect_escalates_after_post_health_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = ConnectionProfile(id="server", host="192.168.0.20")
    manager = ConnectionManager([profile])
    manager.reconnect_backoff_seconds = 0.001

    async def health_connects() -> ConnectionProfile:
        manager.active = profile
        manager.state = ConnectionState.AUTHENTICATING
        return profile

    async def authentication_fails(_profile: ConnectionProfile) -> None:
        manager.disconnect(manual=False)

    async def fully_connects(_profile: ConnectionProfile) -> None:
        manager.mark_connected()

    monkeypatch.setattr(network.random, "uniform", lambda _start, _end: 0.0)
    monkeypatch.setattr(manager, "connect", health_connects)

    failed_attempt = manager.schedule_reconnect(authentication_fails)
    assert failed_attempt is not None
    await failed_attempt

    assert manager.active is None
    assert manager.reconnect_backoff_seconds == 0.002

    successful_attempt = manager.schedule_reconnect(fully_connects)
    assert successful_attempt is not None
    await successful_attempt

    assert manager.state == ConnectionState.CONNECTED
    assert manager.reconnect_backoff_seconds == 1.0


@pytest.mark.asyncio
async def test_network_client_supports_authenticated_admin_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[
        tuple[
            str,
            str,
            dict[str, str],
            dict[str, Any] | None,
            dict[str, str | int | bool] | None,
        ]
    ] = []

    class FakeAsyncClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def request(
            self,
            method: str,
            url: str,
            *,
            headers: dict[str, str],
            json: dict[str, Any] | None,
            params: dict[str, str | int | bool] | None = None,
        ) -> httpx.Response:
            requests.append((method, url, headers, json, params))
            return httpx.Response(
                200,
                json={"method": method},
                request=httpx.Request(method, url),
            )

    monkeypatch.setattr(network.httpx, "AsyncClient", FakeAsyncClient)
    manager = ConnectionManager([ConnectionProfile(id="server", host="192.168.0.20")])
    manager.active = manager.profiles[0]
    client = network.NetworkClient(manager, "admin-token")

    assert await client.get("/api/v1/status") == {"method": "GET"}
    assert await client.post("/api/v1/settings/validate", {"section": "server"}) == {
        "method": "POST"
    }
    assert await client.put("/api/v1/settings/server", {"port": 9000}) == {"method": "PUT"}
    assert await client.get("/api/v1/memories/search", {"q": "한국어", "limit": 20}) == {
        "method": "GET"
    }
    assert await client.patch("/api/v1/memories/memory-1", {"active": False}) == {
        "method": "PATCH"
    }
    assert await client.delete("/api/v1/memories/memory-1") == {"method": "DELETE"}

    assert [item[0] for item in requests] == ["GET", "POST", "PUT", "GET", "PATCH", "DELETE"]
    assert all(item[2] == {"Authorization": "Bearer admin-token"} for item in requests)
    assert requests[1][3] == {"section": "server"}
    assert requests[2][3] == {"port": 9000}
    assert requests[3][4] == {"q": "한국어", "limit": 20}
    assert requests[4][3] == {"active": False}


@pytest.mark.asyncio
async def test_pair_remembers_authenticated_client_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeAsyncClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, url: str, *, json: dict[str, Any]) -> httpx.Response:
            assert url.endswith("/api/v1/pairing/complete")
            assert json == {"code": "123456", "device_name": "Link PC"}
            return httpx.Response(
                200,
                json={"client_id": "client-1", "token": "token-1"},
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(network.httpx, "AsyncClient", FakeAsyncClient)
    manager = ConnectionManager([ConnectionProfile(id="server", host="192.168.0.20")])
    manager.active = manager.profiles[0]
    client = NetworkClient(manager)

    assert await client.pair("123456", "Link PC") == "token-1"
    assert client.client_id == "client-1"


@pytest.mark.asyncio
async def test_network_client_reuses_one_agent_websocket_and_dispatches_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sockets: list[Any] = []
    received: list[dict[str, Any]] = []
    delivered = asyncio.Event()

    class FakeAgentSocket:
        def __init__(self) -> None:
            self.events: asyncio.Queue[str | None] = asyncio.Queue()
            self.sent: list[dict[str, Any]] = []
            self.close_count = 0

        def __aiter__(self) -> "FakeAgentSocket":
            return self

        async def __anext__(self) -> str:
            value = await self.events.get()
            if value is None:
                raise StopAsyncIteration
            return value

        async def send(self, payload: str) -> None:
            self.sent.append(json.loads(payload))

        async def close(self) -> None:
            self.close_count += 1
            await self.events.put(None)

    async def connect(
        url: str, *, additional_headers: dict[str, str]
    ) -> FakeAgentSocket:
        assert url == "ws://192.168.0.20:8765/ws/v1/agent"
        assert additional_headers == {"Authorization": "Bearer token"}
        socket = FakeAgentSocket()
        sockets.append(socket)
        return socket

    async def handle(event: dict[str, Any]) -> None:
        received.append(event)
        delivered.set()

    monkeypatch.setattr(network.websockets, "connect", connect)
    manager = ConnectionManager([ConnectionProfile(id="server", host="192.168.0.20")])
    manager.active = manager.profiles[0]
    client = NetworkClient(manager, "token")
    client.agent_event_callback = handle

    await client.ensure_agent_connection()
    await client.ensure_agent_connection()
    await sockets[0].events.put(json.dumps({"type": "tool.request", "tool_call_id": "1"}))
    await asyncio.wait_for(delivered.wait(), timeout=1)
    await client.send_agent_event({"type": "client.capabilities", "session_id": "s1"})

    assert len(sockets) == 1
    assert received == [{"type": "tool.request", "tool_call_id": "1"}]
    assert sockets[0].sent == [{"type": "client.capabilities", "session_id": "s1"}]
    assert client.agent_connected is True

    await client.close_agent_connection()
    assert sockets[0].close_count == 1
    assert client.agent_connected is False


@pytest.mark.asyncio
async def test_agent_reconnect_cancels_handlers_from_the_replaced_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sockets: list[Any] = []
    handler_started = asyncio.Event()
    handler_cancelled = asyncio.Event()

    class FakeAgentSocket:
        def __init__(self) -> None:
            self.events: asyncio.Queue[str | None] = asyncio.Queue()
            self.sent: list[dict[str, Any]] = []

        def __aiter__(self) -> "FakeAgentSocket":
            return self

        async def __anext__(self) -> str:
            value = await self.events.get()
            if value is None:
                raise StopAsyncIteration
            return value

        async def send(self, payload: str) -> None:
            self.sent.append(json.loads(payload))

        async def close(self) -> None:
            await self.events.put(None)

    async def connect(
        _url: str, *, additional_headers: dict[str, str]
    ) -> FakeAgentSocket:
        assert additional_headers["Authorization"].startswith("Bearer token-")
        socket = FakeAgentSocket()
        sockets.append(socket)
        return socket

    async def handle(_event: dict[str, Any]) -> None:
        handler_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            handler_cancelled.set()
            raise

    monkeypatch.setattr(network.websockets, "connect", connect)
    manager = ConnectionManager([ConnectionProfile(id="server", host="192.168.0.20")])
    manager.active = manager.profiles[0]
    client = NetworkClient(manager, "token-1")
    client.agent_event_callback = handle

    await client.ensure_agent_connection()
    await sockets[0].events.put(json.dumps({"type": "tool.request"}))
    await asyncio.wait_for(handler_started.wait(), timeout=1)

    client.token = "token-2"
    await client.ensure_agent_connection()

    await asyncio.wait_for(handler_cancelled.wait(), timeout=1)
    assert len(sockets) == 2
    assert sockets[1].sent == []
    assert not client._agent_handler_tasks
    await client.close_agent_connection()


@pytest.mark.asyncio
async def test_agent_channel_ignores_malformed_frames_and_keeps_dispatching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delivered = asyncio.Event()

    class FakeAgentSocket:
        def __init__(self) -> None:
            self.events: asyncio.Queue[str | None] = asyncio.Queue()

        def __aiter__(self) -> "FakeAgentSocket":
            return self

        async def __anext__(self) -> str:
            value = await self.events.get()
            if value is None:
                raise StopAsyncIteration
            return value

        async def send(self, _payload: str) -> None:
            return None

        async def close(self) -> None:
            await self.events.put(None)

    socket = FakeAgentSocket()

    async def connect(
        _url: str, *, additional_headers: dict[str, str]
    ) -> FakeAgentSocket:
        del additional_headers
        return socket

    async def handle(event: dict[str, Any]) -> None:
        assert event == {"type": "tool.request", "tool_call_id": "valid"}
        delivered.set()

    monkeypatch.setattr(network.websockets, "connect", connect)
    manager = ConnectionManager([ConnectionProfile(id="server", host="192.168.0.20")])
    manager.active = manager.profiles[0]
    client = NetworkClient(manager, "token")
    client.agent_event_callback = handle

    await client.ensure_agent_connection()
    await socket.events.put("{")
    await socket.events.put(
        json.dumps({"type": "tool.request", "tool_call_id": "valid"})
    )

    await asyncio.wait_for(delivered.wait(), timeout=1)
    assert client.agent_connected is True
    await client.close_agent_connection()


@pytest.mark.asyncio
async def test_unexpected_agent_disconnect_cancels_handlers_before_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler_started = asyncio.Event()
    handler_cancelled = asyncio.Event()
    disconnected = asyncio.Event()

    class FakeAgentSocket:
        def __init__(self) -> None:
            self.events: asyncio.Queue[str | None] = asyncio.Queue()

        def __aiter__(self) -> "FakeAgentSocket":
            return self

        async def __anext__(self) -> str:
            value = await self.events.get()
            if value is None:
                raise StopAsyncIteration
            return value

        async def send(self, _payload: str) -> None:
            return None

        async def close(self) -> None:
            await self.events.put(None)

    socket = FakeAgentSocket()

    async def connect(
        _url: str, *, additional_headers: dict[str, str]
    ) -> FakeAgentSocket:
        del additional_headers
        return socket

    async def handle(_event: dict[str, Any]) -> None:
        handler_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            handler_cancelled.set()
            raise

    def on_disconnect() -> None:
        assert handler_cancelled.is_set()
        disconnected.set()

    monkeypatch.setattr(network.websockets, "connect", connect)
    manager = ConnectionManager([ConnectionProfile(id="server", host="192.168.0.20")])
    manager.active = manager.profiles[0]
    client = NetworkClient(manager, "token")
    client.agent_event_callback = handle
    client.agent_disconnect_callback = on_disconnect

    await client.ensure_agent_connection()
    await socket.events.put(json.dumps({"type": "tool.request"}))
    await asyncio.wait_for(handler_started.wait(), timeout=1)
    await socket.events.put(None)

    await asyncio.wait_for(disconnected.wait(), timeout=1)
    assert client.agent_connected is False
    assert not client._agent_handler_tasks


@pytest.mark.asyncio
async def test_network_client_reuses_one_authoritative_chat_websocket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sockets: list[Any] = []

    class FakeChatSocket:
        def __init__(self) -> None:
            self.events: asyncio.Queue[str | None] = asyncio.Queue()
            self.sent: list[dict[str, Any]] = []
            self.close_count = 0

        def __aiter__(self) -> "FakeChatSocket":
            return self

        async def __anext__(self) -> str:
            value = await self.events.get()
            if value is None:
                raise StopAsyncIteration
            return value

        async def send(self, payload: str) -> None:
            request = json.loads(payload)
            self.sent.append(request)
            await self.events.put(
                json.dumps(
                    {
                        "type": "chat.accepted",
                        "request_id": request["request_id"],
                        "payload": {"conversation_id": str(uuid4())},
                    }
                )
            )
            await self.events.put(
                json.dumps(
                    {
                        "type": "assistant.completed",
                        "request_id": request["request_id"],
                        "payload": {"finish_reason": "stop"},
                    }
                )
            )

        async def close(self) -> None:
            self.close_count += 1
            await self.events.put(None)

    async def connect(
        url: str, *, additional_headers: dict[str, str]
    ) -> FakeChatSocket:
        assert url == "ws://192.168.0.20:8765/ws/v1/chat"
        assert additional_headers == {"Authorization": "Bearer token"}
        socket = FakeChatSocket()
        sockets.append(socket)
        return socket

    monkeypatch.setattr(network.websockets, "connect", connect)
    manager = ConnectionManager(
        [ConnectionProfile(id="server", host="192.168.0.20")]
    )
    manager.active = manager.profiles[0]
    client = NetworkClient(manager, "token")

    client_message_ids: list[str] = []
    for content in ("첫 요청", "둘째 요청"):
        request_id = str(uuid4())
        client_message_id = str(uuid4())
        client_message_ids.append(client_message_id)
        events = [
            event
            async for event in client.chat(
                {
                    "request_id": request_id,
                    "client_message_id": client_message_id,
                    "content": content,
                }
            )
        ]
        assert [event["type"] for event in events] == [
            "chat.accepted",
            "assistant.completed",
        ]

    assert len(sockets) == 1
    assert [request["content"] for request in sockets[0].sent] == [
        "첫 요청",
        "둘째 요청",
    ]
    assert client.chat_connected is True
    with pytest.raises(RuntimeError, match="request_id cannot be reused"):
        _ = [
            event
            async for event in client.chat(
                {
                    "request_id": sockets[0].sent[0]["request_id"],
                    "client_message_id": str(uuid4()),
                    "content": "재사용 금지",
                }
            )
        ]
    with pytest.raises(RuntimeError, match="client_message_id cannot be reused"):
        _ = [
            event
            async for event in client.chat(
                {
                    "request_id": str(uuid4()),
                    "client_message_id": client_message_ids[0],
                    "content": "재사용 금지",
                }
            )
        ]
    await client.close_chat_connection()
    assert sockets[0].close_count == 1
    assert client.chat_connected is False
