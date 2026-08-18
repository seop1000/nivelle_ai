import asyncio
import json as json_module
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from enum import StrEnum
from time import perf_counter
from typing import Any
from uuid import UUID

import httpx
import websockets
from nivelle_protocol.settings import ConnectionProfile


class ServerIdentityMismatchError(OSError):
    """The endpoint is not the Core installation pinned by this profile."""


class ConnectionState(StrEnum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    AUTHENTICATING = "authenticating"
    CONNECTED = "connected"
    DISCONNECTING = "disconnecting"
    WAITING_RETRY = "reconnect_wait"
    RECONNECT_WAIT = "reconnect_wait"
    FAILED = "failed"
    MANUAL_OFFLINE = "manual_offline"


class ConnectionManager:
    def __init__(
        self,
        profiles: list[ConnectionProfile],
        *,
        failures_before_offline: int = 2,
        probe_timeout: float = 2.0,
        health_interval: float = 10.0,
        status_interval: float = 30.0,
    ) -> None:
        self.profiles = profiles
        self.active: ConnectionProfile | None = None
        self.state = ConnectionState.DISCONNECTED
        self.last_error: Exception | None = None
        self.last_latency_ms: float | None = None
        self.last_checked_at: datetime | None = None
        self.last_attempt_at: datetime | None = None
        self.consecutive_failures = 0
        self.failures_before_offline = max(1, failures_before_offline)
        self.probe_timeout = max(0.1, probe_timeout)
        self.health_interval = max(0.1, health_interval)
        self.status_interval = max(self.health_interval, status_interval)
        self.auto_reconnect_enabled = True
        # Persist across reconnect coroutines. A successful /health probe is
        # only a partial connection; reset this delay only after authenticated
        # status and the authoritative WebSocket both succeed.
        self.reconnect_backoff_seconds = 1.0
        self._connection_task: asyncio.Task[ConnectionProfile | None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._generation = 0
        self._shutdown_started = False
        self._shutdown_event = asyncio.Event()
        self._close_lock = asyncio.Lock()
        self._observed_server_ids: dict[str, str] = {}

    @property
    def connection_task(self) -> asyncio.Task[ConnectionProfile | None] | None:
        return self._connection_task

    @property
    def reconnect_task(self) -> asyncio.Task[None] | None:
        return self._reconnect_task

    @property
    def shutdown_started(self) -> bool:
        return self._shutdown_started

    def set_profiles(self, profiles: list[ConnectionProfile]) -> None:
        if self._shutdown_started:
            return
        self._generation += 1
        self.profiles = profiles
        self.active = None
        self.state = ConnectionState.DISCONNECTED
        self.last_error = None
        self.last_latency_ms = None
        self.last_checked_at = None
        self.last_attempt_at = None
        self.consecutive_failures = 0
        self.auto_reconnect_enabled = True
        self.reconnect_backoff_seconds = 1.0
        self._observed_server_ids.clear()

    @staticmethod
    def _profile_key(profile: ConnectionProfile) -> str:
        host = profile.host.strip().lower().strip("[]")
        return f"{'https' if profile.tls else 'http'}://{host}:{profile.port}"

    def server_id_for(self, profile: ConnectionProfile) -> str | None:
        return self._observed_server_ids.get(self._profile_key(profile)) or profile.server_id

    async def _probe(self, profile: ConnectionProfile) -> None:
        scheme = "https" if profile.tls else "http"
        profile_key = self._profile_key(profile)
        self._observed_server_ids.pop(profile_key, None)
        started = perf_counter()
        try:
            async with httpx.AsyncClient(timeout=self.probe_timeout) as client:
                response = await client.get(
                    f"{scheme}://{_url_host(profile.host)}:{profile.port}/health"
                )
                response.raise_for_status()
                try:
                    payload = response.json()
                except ValueError:
                    payload = None
                raw_server_id = payload.get("server_id") if isinstance(payload, dict) else None
                if raw_server_id is None:
                    if profile.server_id is not None:
                        raise ServerIdentityMismatchError(
                            "고정된 서버 ID를 상태 응답에서 확인할 수 없습니다."
                        )
                else:
                    try:
                        observed_server_id = str(UUID(str(raw_server_id)))
                    except ValueError as exc:
                        raise ServerIdentityMismatchError(
                            "서버가 올바르지 않은 서버 ID를 반환했습니다."
                        ) from exc
                    if (
                        profile.server_id is not None
                        and observed_server_id != profile.server_id
                    ):
                        raise ServerIdentityMismatchError(
                            "저장된 프로필과 다른 Nivelle Core 서버가 응답했습니다."
                        )
                    self._observed_server_ids[profile_key] = observed_server_id
        finally:
            self.last_attempt_at = datetime.now(UTC)
            self.last_latency_ms = (perf_counter() - started) * 1000
        self.last_checked_at = self.last_attempt_at

    async def connect(self) -> ConnectionProfile | None:
        if self._shutdown_started or self.state in {
            ConnectionState.MANUAL_OFFLINE,
            ConnectionState.DISCONNECTING,
        }:
            return None
        task = self._connection_task
        if task is None or task.done():
            generation = self._generation
            task = asyncio.create_task(self._connect_once(generation))
            self._connection_task = task
            task.add_done_callback(self._connection_finished)
        return await asyncio.shield(task)

    def _connection_finished(
        self, task: asyncio.Task[ConnectionProfile | None]
    ) -> None:
        if task is self._connection_task:
            self._connection_task = None
        if not task.cancelled():
            task.exception()

    async def _connect_once(self, generation: int) -> ConnectionProfile | None:
        if generation != self._generation or self._shutdown_started:
            return None
        self.state = ConnectionState.CONNECTING
        self.active = None
        self.last_error = None
        identity_mismatch: ServerIdentityMismatchError | None = None
        retryable_failure = False
        for profile in sorted((p for p in self.profiles if p.enabled), key=lambda p: p.priority):
            try:
                await self._probe(profile)
                if generation != self._generation or self._shutdown_started:
                    return None
                self.active = profile
                self.state = ConnectionState.AUTHENTICATING
                self.consecutive_failures = 0
                return profile
            except ServerIdentityMismatchError as exc:
                if generation != self._generation or self._shutdown_started:
                    return None
                self.last_error = exc
                identity_mismatch = exc
                continue
            except (httpx.HTTPError, OSError) as exc:
                if generation != self._generation or self._shutdown_started:
                    return None
                self.last_error = exc
                retryable_failure = True
                continue
        if generation == self._generation and not self._shutdown_started:
            self.state = ConnectionState.FAILED
            if identity_mismatch is not None and not retryable_failure:
                self.last_error = identity_mismatch
                self.auto_reconnect_enabled = False
        return None

    async def check_active(self) -> bool:
        """Probe the active Gateway without switching connection profiles."""

        profile = self.active
        if profile is None:
            return False
        try:
            await self._probe(profile)
            self.last_error = None
            self.consecutive_failures = 0
            return True
        except ServerIdentityMismatchError as exc:
            self.last_error = exc
            self.active = None
            self.state = ConnectionState.RECONNECT_WAIT
            return False
        except (httpx.HTTPError, OSError) as exc:
            self.last_error = exc
            self.consecutive_failures += 1
            if (
                self.active is profile
                and self.consecutive_failures >= self.failures_before_offline
            ):
                self.active = None
                self.state = ConnectionState.RECONNECT_WAIT
            return False

    async def reconnect_delays(self) -> AsyncIterator[float]:
        while (
            self.active is None
            and self.auto_reconnect_enabled
            and self.state != ConnectionState.MANUAL_OFFLINE
        ):
            self.state = ConnectionState.RECONNECT_WAIT
            delay = self.reconnect_backoff_seconds
            wait_seconds = delay + random.uniform(0, delay * 0.15)
            yield wait_seconds
            await asyncio.sleep(wait_seconds)
            if await self.connect():
                # Preserve escalation until `mark_connected` confirms that
                # auth, status, and the WebSocket also succeeded.
                self.reconnect_backoff_seconds = min(delay * 2, 30)
                return
            self.reconnect_backoff_seconds = min(delay * 2, 30)

    def mark_connected(self) -> None:
        if self._shutdown_started:
            return
        if self.active is None:
            raise RuntimeError("활성 연결 없이 연결 완료 상태로 바꿀 수 없습니다.")
        self.state = ConnectionState.CONNECTED
        self.consecutive_failures = 0
        self.auto_reconnect_enabled = True
        self.reconnect_backoff_seconds = 1.0

    def disconnect(self, *, manual: bool) -> None:
        self._generation += 1
        self.active = None
        self.consecutive_failures = 0
        if manual:
            self.auto_reconnect_enabled = False
            self.state = ConnectionState.MANUAL_OFFLINE
        else:
            self.state = ConnectionState.RECONNECT_WAIT

    def schedule_reconnect(
        self,
        on_connected: Callable[[ConnectionProfile], Awaitable[None]],
        *,
        on_attempt: Callable[[], None] | None = None,
    ) -> asyncio.Task[None] | None:
        """Create at most one retry scheduler for the current generation."""
        if (
            self._shutdown_started
            or self.state in {ConnectionState.DISCONNECTING, ConnectionState.MANUAL_OFFLINE}
            or not self.auto_reconnect_enabled
        ):
            return None
        task = self._reconnect_task
        if task is not None and not task.done():
            return task
        generation = self._generation
        task = asyncio.create_task(
            self._reconnect_loop(generation, on_connected, on_attempt)
        )
        self._reconnect_task = task
        task.add_done_callback(self._reconnect_finished)
        return task

    def _reconnect_finished(self, task: asyncio.Task[None]) -> None:
        if task is self._reconnect_task:
            self._reconnect_task = None
        if not task.cancelled():
            task.exception()

    async def _reconnect_loop(
        self,
        generation: int,
        on_connected: Callable[[ConnectionProfile], Awaitable[None]],
        on_attempt: Callable[[], None] | None,
    ) -> None:
        while (
            generation == self._generation
            and not self._shutdown_started
            and self.active is None
            and self.auto_reconnect_enabled
        ):
            self.state = ConnectionState.WAITING_RETRY
            delay = self.reconnect_backoff_seconds
            wait_seconds = delay + random.uniform(0, delay * 0.15)
            try:
                await asyncio.wait_for(self._shutdown_event.wait(), timeout=wait_seconds)
                return
            except TimeoutError:
                pass
            if generation != self._generation or self._shutdown_started:
                return
            if on_attempt is not None:
                on_attempt()
            profile = await self.connect()
            if profile is not None:
                # Preserve escalation unless authenticated status and the
                # authoritative WebSocket call `mark_connected()` successfully.
                self.reconnect_backoff_seconds = min(delay * 2, 30)
                await on_connected(profile)
                return
            self.reconnect_backoff_seconds = min(delay * 2, 30)

    async def shutdown(self) -> None:
        """Idempotently stop and reap every manager-owned task."""
        async with self._close_lock:
            if not self._shutdown_started:
                self._shutdown_started = True
                self.state = ConnectionState.DISCONNECTING
                self.auto_reconnect_enabled = False
                self._generation += 1
                self._shutdown_event.set()
            current = asyncio.current_task()
            tasks = [
                task
                for task in (self._connection_task, self._reconnect_task)
                if task is not None and task is not current and not task.done()
            ]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self.active = None
            self.state = ConnectionState.DISCONNECTED

    def base_url(self) -> str:
        if not self.active:
            raise RuntimeError("서버에 연결되어 있지 않습니다.")
        return (
            f"{'https' if self.active.tls else 'http'}://"
            f"{_url_host(self.active.host)}:{self.active.port}"
        )


def _url_host(host: str) -> str:
    value = host.strip()
    if ":" in value and not (value.startswith("[") and value.endswith("]")):
        return f"[{value}]"
    return value


class NetworkClient:
    def __init__(self, connections: ConnectionManager, token: str | None = None) -> None:
        self.connections, self.token = connections, token
        self.client_id: str | None = None
        self._chat_socket: Any | None = None
        self._chat_reader_task: asyncio.Task[None] | None = None
        self._chat_endpoint: str | None = None
        self._chat_token: str | None = None
        self._chat_connection_lock = asyncio.Lock()
        self._chat_send_lock = asyncio.Lock()
        self._chat_queues: dict[
            str, asyncio.Queue[dict[str, Any] | BaseException]
        ] = {}
        self._used_chat_request_ids: set[str] = set()
        self._used_client_message_ids: set[str] = set()
        self.chat_disconnect_callback: Callable[[], None] | None = None
        self._agent_socket: Any | None = None
        self._agent_reader_task: asyncio.Task[None] | None = None
        self._agent_endpoint: str | None = None
        self._agent_token: str | None = None
        self._agent_connection_lock = asyncio.Lock()
        self._agent_send_lock = asyncio.Lock()
        self._agent_handler_tasks: set[asyncio.Task[None]] = set()
        self.agent_event_callback: (
            Callable[[dict[str, Any]], Awaitable[None]] | None
        ) = None
        self.agent_disconnect_callback: Callable[[], None] | None = None
        self._shutdown_started = False

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, str | int | bool] | None = None,
    ) -> Any:
        if self._shutdown_started:
            raise RuntimeError("network client is shut down")
        request_options: dict[str, Any] = {
            "headers": self.headers,
            "json": json,
        }
        if params is not None:
            request_options["params"] = params
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.request(
                method,
                self.connections.base_url() + path,
                **request_options,
            )
            response.raise_for_status()
            if response.status_code == 204 or not response.content:
                return None
            return response.json()

    async def get(
        self, path: str, params: dict[str, str | int | bool] | None = None
    ) -> Any:
        return await self.request("GET", path, params=params)

    async def post(self, path: str, value: dict[str, Any] | None = None) -> Any:
        return await self.request("POST", path, json=value)

    async def put(self, path: str, value: dict[str, Any]) -> Any:
        return await self.request("PUT", path, json=value)

    async def patch(self, path: str, value: dict[str, Any]) -> Any:
        return await self.request("PATCH", path, json=value)

    async def delete(self, path: str) -> Any:
        return await self.request("DELETE", path)

    async def pair(self, code: str, name: str) -> str:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                self.connections.base_url() + "/api/v1/pairing/complete",
                json={"code": code, "device_name": name},
            )
            response.raise_for_status()
            body = response.json()
            self.client_id = str(body["client_id"])
            self.token = str(body["token"])
            return self.token

    def _chat_url(self) -> str:
        return (
            self.connections.base_url()
            .replace("http://", "ws://")
            .replace("https://", "wss://")
            + "/ws/v1/chat"
        )

    def _agent_url(self) -> str:
        return (
            self.connections.base_url()
            .replace("http://", "ws://")
            .replace("https://", "wss://")
            + "/ws/v1/agent"
        )

    @property
    def chat_connected(self) -> bool:
        task = self._chat_reader_task
        return self._chat_socket is not None and task is not None and not task.done()

    async def ensure_chat_connection(self) -> None:
        """Create the one authoritative chat WebSocket for this client session."""
        if self._shutdown_started:
            raise RuntimeError("network client is shut down")
        endpoint = self._chat_url()
        async with self._chat_connection_lock:
            if (
                self.chat_connected
                and self._chat_endpoint == endpoint
                and self._chat_token == self.token
            ):
                return
            await self._close_chat_connection_locked(
                ConnectionError("chat connection replaced")
            )
            try:
                socket = await websockets.connect(
                    endpoint, additional_headers=self.headers
                )
            except Exception as exc:
                raise ConnectionError("채팅 WebSocket에 연결하지 못했습니다.") from exc
            self._chat_socket = socket
            self._chat_endpoint = endpoint
            self._chat_token = self.token
            self._chat_reader_task = asyncio.create_task(self._read_chat_events(socket))

    async def close_chat_connection(self) -> None:
        async with self._chat_connection_lock:
            await self._close_chat_connection_locked(
                ConnectionError("chat connection closed")
            )

    @property
    def agent_connected(self) -> bool:
        task = self._agent_reader_task
        return self._agent_socket is not None and task is not None and not task.done()

    async def ensure_agent_connection(self) -> None:
        """Create the one authenticated Agent channel for this Link session."""

        if self._shutdown_started:
            raise RuntimeError("network client is shut down")

        endpoint = self._agent_url()
        async with self._agent_connection_lock:
            if (
                self.agent_connected
                and self._agent_endpoint == endpoint
                and self._agent_token == self.token
            ):
                return
            await self._close_agent_connection_locked()
            try:
                socket = await websockets.connect(
                    endpoint, additional_headers=self.headers
                )
            except Exception as exc:
                raise ConnectionError("Agent WebSocket에 연결하지 못했습니다.") from exc
            self._agent_socket = socket
            self._agent_endpoint = endpoint
            self._agent_token = self.token
            self._agent_reader_task = asyncio.create_task(
                self._read_agent_events(socket)
            )

    async def send_agent_event(self, event: dict[str, Any]) -> None:
        if not self.agent_connected:
            raise ConnectionError("Agent connection is unavailable")
        socket = self._agent_socket
        if socket is None:
            raise ConnectionError("Agent connection is unavailable")
        async with self._agent_send_lock:
            await socket.send(json_module.dumps(event, ensure_ascii=False))

    async def close_agent_connection(self) -> None:
        async with self._agent_connection_lock:
            await self._close_agent_connection_locked()

    async def close_connections(self) -> None:
        """Close both authoritative sockets used by this client session."""

        await asyncio.gather(
            self.close_agent_connection(),
            self.close_chat_connection(),
            return_exceptions=False,
        )

    async def shutdown(self) -> None:
        if self._shutdown_started:
            return
        self._shutdown_started = True
        self.chat_disconnect_callback = None
        self.agent_disconnect_callback = None
        await self.close_connections()

    async def _close_agent_connection_locked(self) -> None:
        socket = self._agent_socket
        reader = self._agent_reader_task
        self._agent_socket = None
        self._agent_reader_task = None
        self._agent_endpoint = None
        self._agent_token = None
        if socket is not None:
            try:
                await socket.close()
            except Exception:
                pass
        if reader is not None and reader is not asyncio.current_task() and not reader.done():
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        await self._cancel_agent_handler_tasks()

    async def _cancel_agent_handler_tasks(self) -> None:
        current = asyncio.current_task()
        tasks = [
            task
            for task in tuple(self._agent_handler_tasks)
            if task is not current and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _read_agent_events(self, socket: Any) -> None:
        unexpected = False
        try:
            async for payload in socket:
                try:
                    event = json_module.loads(payload)
                except (json_module.JSONDecodeError, TypeError, UnicodeDecodeError):
                    # An untrusted malformed frame is ignored without invoking
                    # any handler and without tearing down the healthy channel.
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("type") == "ping":
                    async with self._agent_send_lock:
                        await socket.send(
                            json_module.dumps(
                                {
                                    "type": "pong",
                                    "protocol_version": event.get("protocol_version"),
                                }
                            )
                        )
                    continue
                callback = self.agent_event_callback
                if callback is None:
                    continue
                task = asyncio.create_task(
                    self._dispatch_agent_event(callback, event)
                )
                self._agent_handler_tasks.add(task)
                task.add_done_callback(self._agent_handler_finished)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            if self._agent_socket is socket:
                unexpected = True
                self._agent_socket = None
                self._agent_reader_task = None
                self._agent_endpoint = None
                self._agent_token = None
            if unexpected and not self._shutdown_started:
                await self._cancel_agent_handler_tasks()
                if self.agent_disconnect_callback is not None:
                    self.agent_disconnect_callback()

    @staticmethod
    async def _dispatch_agent_event(
        callback: Callable[[dict[str, Any]], Awaitable[None]],
        event: dict[str, Any],
    ) -> None:
        await callback(event)

    def _agent_handler_finished(self, task: asyncio.Task[None]) -> None:
        self._agent_handler_tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def _close_chat_connection_locked(self, reason: BaseException) -> None:
        socket = self._chat_socket
        reader = self._chat_reader_task
        self._chat_socket = None
        self._chat_reader_task = None
        self._chat_endpoint = None
        self._chat_token = None
        if socket is not None:
            try:
                await socket.close()
            except Exception:
                pass
        if reader is not None and reader is not asyncio.current_task() and not reader.done():
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        for queue in tuple(self._chat_queues.values()):
            queue.put_nowait(reason)

    async def _read_chat_events(self, socket: Any) -> None:
        failure: BaseException = ConnectionError("chat connection ended")
        unexpected = False
        try:
            async for payload in socket:
                event = json_module.loads(payload)
                if not isinstance(event, dict):
                    continue
                request_id = event.get("request_id")
                queue = self._chat_queues.get(str(request_id)) if request_id else None
                if queue is not None:
                    queue.put_nowait(event)
                elif request_id is None:
                    # Protocol errors produced before request validation have no
                    # correlation ID. Normally only one UI request is active.
                    for pending in tuple(self._chat_queues.values()):
                        pending.put_nowait(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure = ConnectionError("chat connection was interrupted")
            failure.__cause__ = exc
        finally:
            if self._chat_socket is socket:
                unexpected = True
                self._chat_socket = None
                self._chat_reader_task = None
                self._chat_endpoint = None
                self._chat_token = None
                for queue in tuple(self._chat_queues.values()):
                    queue.put_nowait(failure)
            if (
                unexpected
                and not self._shutdown_started
                and self.chat_disconnect_callback is not None
            ):
                self.chat_disconnect_callback()

    async def chat(self, request: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        request_id = str(request.get("request_id") or "")
        if not request_id:
            raise ValueError("chat request_id is required")
        client_message_id = str(request.get("client_message_id") or "")
        if not client_message_id:
            raise ValueError("chat client_message_id is required")
        if request_id in self._used_chat_request_ids:
            raise RuntimeError("the same chat request_id cannot be reused")
        if client_message_id in self._used_client_message_ids:
            raise RuntimeError("the same client_message_id cannot be reused")
        self._used_chat_request_ids.add(request_id)
        self._used_client_message_ids.add(client_message_id)
        await self.ensure_chat_connection()
        if request_id in self._chat_queues:
            raise RuntimeError("the same chat request is already pending")
        queue: asyncio.Queue[dict[str, Any] | BaseException] = asyncio.Queue()
        self._chat_queues[request_id] = queue
        try:
            socket = self._chat_socket
            if socket is None:
                raise ConnectionError("chat connection is unavailable")
            async with self._chat_send_lock:
                await socket.send(json_module.dumps(request, ensure_ascii=False))
            while True:
                event_or_error = await queue.get()
                if isinstance(event_or_error, BaseException):
                    raise ConnectionError("chat response was interrupted") from event_or_error
                event = event_or_error
                yield event
                if event.get("type") in {"assistant.completed", "error", "chat.cancelled"}:
                    return
        finally:
            self._chat_queues.pop(request_id, None)
