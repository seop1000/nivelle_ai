import asyncio
import hashlib
import re
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from ipaddress import ip_address
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import unquote
from uuid import UUID, uuid4

import aiosqlite
import httpx
from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import JSONResponse
from nivelle_protocol.chat import (
    AssistantCompletedPayload,
    AssistantContextPayload,
    ChatRequest,
    CompletedAssistantMessage,
    RetrievalContext,
    ServerEvent,
)
from nivelle_protocol.network import GatewayNetworkRuntime
from nivelle_protocol.pairing import PairingComplete
from nivelle_protocol.server_status import GenerationMetrics, ServerStatus
from nivelle_protocol.settings import (
    AgentSettings,
    InferenceSettings,
    MemorySettings,
    ModelsSettings,
)
from nivelle_protocol.tools import TOOL_REGISTRY, TOOL_VERSION
from nivelle_protocol.version import (
    APP_VERSION,
    PROTOCOL_VERSION,
    is_protocol_compatible,
    protocol_compatibility,
    runtime_identity,
)
from pydantic import ValidationError

from .admin_control import CoreAdminControl
from .agent_gateway import AgentGateway, AgentGatewayError, AgentSessionHandle
from .audio_analysis import (
    MAX_UPLOAD_BYTES,
    SUPPORTED_EXTENSIONS,
    AudioAnalysisManager,
    audio_capabilities,
)
from .auth import PairingService
from .backend_status import probe_openai_backend
from .config import ConfigService, IncompleteSettingsError
from .database import Database
from .llm import LlamaCppServerProvider, LLMProvider, MockLLMProvider, PromptMessage
from .memory_api import create_memory_router
from .memory_repository import MemoryRepository
from .memory_retriever import MemoryRetriever
from .model_runtime import (
    ModelCapabilities,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ModelRouter,
    OpenAICompatibleModelProvider,
    StreamingLLMModelProvider,
)
from .paths import server_data_dir
from .persona import (
    PersonaRecoveryError,
    PersonaStorageError,
    PromptContextBuilder,
    PromptTooLargeError,
)
from .repositories import ConversationRepository
from .telemetry import TelemetryProvider
from .tool_execution import execute_tool_proposals, planning_failure
from .tool_orchestrator import ToolOrchestrationLimits, ToolOrchestrator
from .tool_repository import ToolRepository

_QUANTIZATION_PATTERN = re.compile(r"(?i)(Q\d(?:_[A-Z0-9]+)+)")


def _model_quantization(model_name: str | None) -> str | None:
    if not model_name:
        return None
    match = _QUANTIZATION_PATTERN.search(model_name)
    return match.group(1).upper() if match is not None else None


def _validated_persisted_assistant_message_id(
    message: dict[str, object], accepted_assistant_message_id: str | None
) -> str:
    """Keep the durable assistant row bound to the ID announced at acceptance."""

    if accepted_assistant_message_id is None:
        raise RuntimeError("assistant completion has no accepted assistant_message_id")
    persisted_message_id = str(message.get("id") or "")
    if persisted_message_id != accepted_assistant_message_id:
        raise RuntimeError(
            "persisted assistant message id does not match accepted "
            "assistant_message_id"
        )
    return persisted_message_id


class Services:
    def __init__(
        self,
        root: Path,
        *,
        provider_endpoint_override: str | None = None,
        network_runtime: GatewayNetworkRuntime | None = None,
    ) -> None:
        self.root = root
        canonical_database = root / "database" / "nivelle.db"
        legacy_database = root / "database" / "nozomi.db"
        # Explicit legacy data-dir overrides remain usable for one transition
        # release. Default-root migration has already created ``nivelle.db``.
        database_path = (
            legacy_database
            if not canonical_database.exists() and legacy_database.exists()
            else canonical_database
        )
        self.db = Database(database_path)
        self.conversations = ConversationRepository(self.db)
        self.memories = MemoryRepository(self.db)
        self.pairing = PairingService(self.db)
        self.config = ConfigService(
            root / "config",
            self.db,
            provider_endpoint_override=provider_endpoint_override,
        )
        self.persona = PromptContextBuilder(root / "config" / "persona")
        agent = AgentSettings.model_validate(self.config.load("agent"))
        self.tool_repository = ToolRepository(self.db)
        self.tool_orchestrator = ToolOrchestrator(
            self.tool_repository,
            ToolOrchestrationLimits(
                max_parallel_calls_per_client=agent.max_parallel_calls_per_client,
                max_calls_per_turn=max(agent.max_calls_per_turn, 1),
                idempotency_retention_days=agent.audit_retention_days,
            ),
        )
        self.agent_gateway = AgentGateway(self.tool_orchestrator)
        self.audio_analysis = AudioAnalysisManager(root / "audio-analysis")
        self.telemetry = TelemetryProvider()
        self.active_generations = 0
        self.active_conversation_generations: set[str] = set()
        self.inflight_request_ids: set[str] = set()
        self.inflight_message_ids: set[str] = set()
        self.inflight_retry_targets: set[str] = set()
        self.message_idempotency_lock = asyncio.Lock()
        self.last_llm_error: str | None = None
        self.last_request_metrics: GenerationMetrics | None = None
        self.network_runtime = network_runtime
        self.server_id: str | None = None
        self.runtime_loop: asyncio.AbstractEventLoop | None = None
        self.client_websockets: dict[str, set[WebSocket]] = {}
        self.core_admin = CoreAdminControl(
            self.db,
            self.pairing,
            self.agent_gateway,
            server_id=lambda: self.server_id,
            network_status=lambda: (
                self.network_runtime.status_dict()
                if self.network_runtime is not None
                else None
            ),
            disconnect_client=self.disconnect_client,
        )

    def register_client_websocket(self, client_id: str, websocket: WebSocket) -> None:
        self.client_websockets.setdefault(client_id, set()).add(websocket)

    def unregister_client_websocket(self, client_id: str, websocket: WebSocket) -> None:
        sockets = self.client_websockets.get(client_id)
        if sockets is None:
            return
        sockets.discard(websocket)
        if not sockets:
            self.client_websockets.pop(client_id, None)

    async def disconnect_client(self, client_id: str) -> None:
        """Terminate every live transport after local token revocation."""

        sockets = tuple(self.client_websockets.pop(client_id, ()))
        try:
            await self.agent_gateway.disconnect_client(client_id)
        finally:
            for websocket in sockets:
                try:
                    await websocket.close(4403, "Core에서 인증이 해제되었습니다.")
                except RuntimeError:
                    pass

    def provider(self) -> MockLLMProvider | LlamaCppServerProvider:
        models = ModelsSettings.model_validate(self.config.load("models"))
        inference = InferenceSettings.model_validate(self.config.load("inference"))
        if models.mode == "mock":
            return MockLLMProvider()
        primary = next(
            (model for model in models.models if model.enabled and model.role == "primary"),
            None,
        )
        endpoint = (
            primary.endpoint
            if primary is not None and primary.endpoint
            else models.provider_endpoint
        )
        return LlamaCppServerProvider(
            endpoint,
            inference,
            model_id=primary.id if primary is not None else None,
        )

    def model_router(
        self,
        provider: LLMProvider,
        models: ModelsSettings,
        inference: InferenceSettings,
        *,
        on_metrics: Any = None,
    ) -> ModelRouter:
        """Adapt the configured provider without moving persona or tool policy into it."""

        primary_entry = next(
            (model for model in models.models if model.enabled and model.role == "primary"),
            None,
        )
        primary_model_id = (
            primary_entry.id
            if primary_entry is not None
            else "mock"
            if models.mode == "mock"
            else "external"
        )
        capabilities = ModelCapabilities(
            tool_calling=callable(getattr(provider, "plan_tools", None)),
            context_length=inference.context_size,
        )
        if type(provider) is LlamaCppServerProvider:
            primary_endpoint = (
                primary_entry.endpoint
                if primary_entry is not None and primary_entry.endpoint
                else provider.base_url
            )
            primary_runtime: ModelProvider = OpenAICompatibleModelProvider(
                provider,
                provider_id="openai-compatible",
                model_id=primary_model_id,
                endpoint=primary_endpoint,
                timeout=inference.request_timeout,
                on_metrics=on_metrics,
                capabilities=capabilities,
            )
        else:
            primary_runtime = StreamingLLMModelProvider(
                provider,
                provider_id="mock" if models.mode == "mock" else "custom",
                model_id=primary_model_id,
                endpoint=None,
                timeout=inference.request_timeout,
                on_metrics=on_metrics,
                capabilities=capabilities,
            )

        fallback_runtime = None
        fallback_entry = next(
            (model for model in models.models if model.enabled and model.role == "fallback"),
            None,
        )
        if models.mode != "mock" and models.fallback_enabled and fallback_entry is not None:
            fallback_endpoint = fallback_entry.endpoint or models.provider_endpoint
            fallback_provider = LlamaCppServerProvider(
                fallback_endpoint,
                inference,
                model_id=fallback_entry.id,
            )
            fallback_runtime = OpenAICompatibleModelProvider(
                fallback_provider,
                provider_id="openai-compatible",
                model_id=fallback_entry.id,
                endpoint=fallback_endpoint,
                timeout=inference.request_timeout,
                on_metrics=on_metrics,
                capabilities=ModelCapabilities(
                    tool_calling=True,
                    context_length=inference.context_size,
                ),
            )
        return ModelRouter(primary_runtime, fallback_runtime)


async def build_runtime_prompt_context(
    services: Services,
    chat_request: ChatRequest,
    models: ModelsSettings,
) -> dict[str, object]:
    """Build truthful per-request facts; client fields never authorize actions."""
    active_model = next(
        (model for model in models.models if model.enabled and model.role == "primary"),
        None,
    )
    fallback_model = (
        next(
            (model for model in models.models if model.enabled and model.role == "fallback"),
            None,
        )
        if models.fallback_enabled
        else None
    )
    model_name: str | None
    if models.mode == "mock":
        llm_state = "mock"
        model_name = "Mock LLM"
    else:
        primary_endpoint = (
            active_model.endpoint
            if active_model is not None and active_model.endpoint
            else models.provider_endpoint
        )
        backend = await probe_openai_backend(primary_endpoint)
        llm_state = str(backend["state"])
        loaded_model = backend.get("loaded_model")
        model_name = loaded_model if isinstance(loaded_model, str) else None

    context: dict[str, object] = {
        "gateway_state": "running",
        "llm_state": llm_state,
        "loaded_model": model_name,
        "configured_model": active_model.name if active_model is not None else None,
        "fallback_model": fallback_model.name if fallback_model is not None else None,
        "server_app_version": APP_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "memory_database_state": "ready",
        "memory_search_backend": "sqlite_hybrid",
        "embedding_state": "unavailable",
        "local_timestamp": datetime.now(UTC).astimezone().isoformat(timespec="seconds"),
        "assistant_activity": "preparing_response",
    }
    client_context = chat_request.runtime_context
    if client_context is not None:
        context.update(
            {
                "connection_profile_id": client_context.profile_id,
                "connection_type": client_context.connection_type,
                "server_host": client_context.host,
                "server_port": client_context.port,
                "server_address": f"{client_context.host}:{client_context.port}",
                "tls_enabled": client_context.tls,
                "client_app_version": client_context.client_version,
                "round_trip_latency_ms": client_context.latency_ms,
            }
        )
    return context


def create_app(
    data_dir: Path | None = None,
    *,
    provider_endpoint_override: str | None = None,
    network_runtime: GatewayNetworkRuntime | None = None,
) -> FastAPI:
    services = Services(
        data_dir or server_data_dir(),
        provider_endpoint_override=provider_endpoint_override,
        network_runtime=network_runtime,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> Any:
        await services.db.initialize()
        services.server_id = await services.db.load_or_create_server_id()
        recovery = await services.conversations.recover_interrupted_generations()
        if any(recovery.values()):
            print(
                "component=nivelle-core event=generation_recovery "
                f"assistant_messages={recovery['assistant_messages']} "
                f"orphan_user_messages={recovery['orphan_user_messages']}"
            )
        if await services.pairing.pairing_required():
            services.pairing.generate_code()
            print(
                "Nivelle Link pairing is required. Retrieve the one-time code from "
                "/api/v1/pairing/local-code on the Core PC."
            )
        services.runtime_loop = asyncio.get_running_loop()
        try:
            yield
        finally:
            await services.audio_analysis.shutdown()
            services.runtime_loop = None

    app = FastAPI(title="Nivelle Core Gateway", version=APP_VERSION, lifespan=lifespan)
    app.state.services = services

    async def authenticated(authorization: Annotated[str | None, Header()] = None) -> str:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "인증이 필요합니다.")
        client_id = await services.pairing.verify(authorization[7:])
        if not client_id:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "인증 토큰이 올바르지 않습니다.")
        return client_id

    async def administrator(authorization: Annotated[str | None, Header()] = None) -> str:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "인증이 필요합니다.")
        client_id = await services.pairing.verify_admin(authorization[7:])
        if not client_id:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "관리자 권한이 필요합니다.")
        return client_id

    app.include_router(create_memory_router(services.memories, authenticated))

    @app.exception_handler(ValidationError)
    async def validation_error(_: Request, exc: ValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "CONFIG_VALIDATION_FAILED",
                    "message": "설정값이 올바르지 않습니다.",
                    "details": {
                        "errors": exc.errors(include_url=False, include_context=False)
                    },
                    "request_id": None,
                    "retryable": False,
                }
            },
        )

    @app.exception_handler(IncompleteSettingsError)
    async def incomplete_settings(_: Request, exc: IncompleteSettingsError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "CONFIG_VALIDATION_FAILED",
                    "message": str(exc),
                    "details": {"section": exc.section, "missing": exc.missing},
                    "request_id": None,
                    "retryable": False,
                }
            },
        )

    def current_server_id() -> str:
        if services.server_id is None:
            raise HTTPException(503, "서버 identity가 아직 준비되지 않았습니다.")
        return services.server_id

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "server_id": current_server_id()}

    @app.get("/api/v1/pairing/status")
    async def pairing_status() -> dict[str, Any]:
        return {
            "pairing_required": await services.pairing.pairing_required(),
            "pairing_available": services.pairing.pairing_available(),
            "expires_at": services.pairing.expires_at,
        }

    @app.post("/api/v1/pairing/code")
    async def issue_pairing_code(
        _client_id: str = Depends(administrator),
    ) -> dict[str, object]:
        code = services.pairing.generate_code()
        return {"code": code, "expires_at": services.pairing.expires_at}

    @app.get("/api/v1/pairing/local-code")
    async def local_pairing_code(request: Request) -> dict[str, object]:
        host = request.client.host if request.client else ""
        try:
            loopback_client = ip_address(host).is_loopback
        except ValueError:
            loopback_client = host == "testclient"
        if not loopback_client:
            raise HTTPException(403, "페어링 코드는 서버 PC에서만 확인할 수 있습니다.")
        required = await services.pairing.pairing_required()
        if not services.pairing.pairing_available():
            services.pairing.generate_code()
        return {
            "pairing_required": required,
            "pairing_available": True,
            "code": services.pairing.code,
            "expires_at": (
                services.pairing.expires_at.isoformat()
                if services.pairing.expires_at is not None
                else None
            ),
        }

    @app.post("/api/v1/pairing/complete")
    async def pairing_complete(body: PairingComplete, request: Request) -> dict[str, str]:
        host = request.client.host if request.client else ""
        try:
            private_client = ip_address(host).is_private
        except ValueError:
            private_client = host == "testclient"
        if not private_client:
            raise HTTPException(403, "페어링은 사설 네트워크에서만 가능합니다.")
        try:
            client_id, token = await services.pairing.complete(body.code, body.device_name)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"client_id": client_id, "token": token}

    @app.get("/api/v1/status", response_model=ServerStatus)
    async def server_status(client_id: str = Depends(authenticated)) -> ServerStatus:
        models = ModelsSettings.model_validate(services.config.load("models"))
        active_memory_row = await services.db.fetchone(
            """
            SELECT COUNT(*) AS count
            FROM memories
            WHERE explicitly_saved=1 AND active=1
            """
        )
        active_memory_count = int(active_memory_row["count"]) if active_memory_row else 0
        inactive_memory_row = await services.db.fetchone(
            """
            SELECT COUNT(*) AS count
            FROM memories
            WHERE explicitly_saved=1 AND active=0
            """
        )
        inactive_memory_count = (
            int(inactive_memory_row["count"]) if inactive_memory_row else 0
        )
        active_model = next(
            (model for model in models.models if model.enabled and model.role == "primary"),
            None,
        )
        if models.mode == "mock":
            backend = {
                "state": "mock",
                "reachable": True,
                "available": True,
                "url": None,
                "status_code": None,
                "details": None,
                "loaded_model": "Mock LLM",
                "configured_model": "Mock LLM",
                "engine": "mock",
                "quantization": None,
                "models_error": None,
                "error": None,
            }
        else:
            primary_endpoint = (
                active_model.endpoint
                if active_model is not None and active_model.endpoint
                else models.provider_endpoint
            )
            backend = await probe_openai_backend(primary_endpoint)
            loaded_model_value = backend.get("loaded_model")
            loaded_model = (
                loaded_model_value if isinstance(loaded_model_value, str) else None
            )
            backend.update(
                {
                    "configured_model": (
                        active_model.name if active_model is not None else None
                    ),
                    "engine": "llama.cpp",
                    "quantization": _model_quantization(loaded_model),
                }
            )
        last_request_metrics = (
            services.last_request_metrics.model_dump(mode="json")
            if services.last_request_metrics is not None
            else None
        )
        process_metrics = services.telemetry.sample()
        process_metrics["last_request"] = last_request_metrics
        memory_database = {
            "state": "ready",
            "backend": "sqlite",
            "search_backend": "sqlite_hybrid",
            "active_count": active_memory_count,
            "inactive_count": inactive_memory_count,
        }
        embedding_model = {
            "state": "unavailable",
            "provider": None,
            "reason": "not_configured",
        }
        agent_settings = AgentSettings.model_validate(services.config.load("agent"))
        agent_snapshot = await services.agent_gateway.snapshot()
        tool_status_rows = await services.db.fetchall(
            "SELECT status,COUNT(*) AS count FROM tool_calls GROUP BY status"
        )
        tool_status_counts = {
            str(row["status"]): int(row["count"]) for row in tool_status_rows
        }
        recent_tool_failures = await services.db.fetchall(
            """
            SELECT tool_name,error_code,updated_at
            FROM tool_calls
            WHERE status IN (
                'validation_failed','denied','failed','cancelled','timed_out',
                'client_disconnected'
            )
            ORDER BY updated_at DESC LIMIT 10
            """
        )
        selected_target = next(
            (
                session.client_id
                for session in agent_snapshot.sessions
                if session.client_id == client_id
            ),
            None,
        )
        agent_status = {
            "enabled": agent_settings.enabled,
            "registry": {
                "version": TOOL_VERSION,
                "tool_count": len(TOOL_REGISTRY),
            },
            "clients": [
                {
                    "client_id": session.client_id,
                    "session_id": session.session_id,
                    "tool_count": len(session.enabled_tools),
                    "enabled_tools": list(session.enabled_tools),
                    "protocol_status": "compatible",
                    "app_version": session.app_version,
                    "platform": session.platform,
                }
                for session in agent_snapshot.sessions
            ],
            "statistics": {
                "pending": len(agent_snapshot.pending_calls),
                "running": tool_status_counts.get("running", 0),
                "completed": tool_status_counts.get("completed", 0),
                "failed": sum(
                    tool_status_counts.get(name, 0)
                    for name in (
                        "validation_failed",
                        "denied",
                        "failed",
                        "cancelled",
                        "timed_out",
                        "client_disconnected",
                    )
                ),
            },
            "recent_failures": [
                {
                    "tool_name": str(row["tool_name"]),
                    "error_code": str(row["error_code"] or "execution_failed"),
                    "updated_at": str(row["updated_at"]),
                }
                for row in recent_tool_failures
            ],
            "selected_target_client": selected_target,
        }
        runtime = runtime_identity("nivelle-core").model_dump(mode="json")
        return ServerStatus.model_validate({
            "version": APP_VERSION,
            "app_version": APP_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "server_id": current_server_id(),
            "client_id": client_id,
            "runtime": runtime,
            "version_info": runtime,
            "gateway": "running",
            "pairing_required": await services.pairing.pairing_required(),
            "model_name": backend.get("loaded_model"),
            "configured_model_name": (
                "Mock LLM"
                if models.mode == "mock"
                else active_model.name
                if active_model is not None
                else None
            ),
            "model_role": active_model.role if active_model is not None else None,
            "model_mode": models.mode,
            "llama_server": backend,
            "assistant_state": (
                "generating"
                if services.active_generations
                else "idle"
                if backend["available"]
                else "backend_unavailable"
            ),
            "active_generations": services.active_generations,
            "last_error": services.last_llm_error,
            "last_request_metrics": last_request_metrics,
            "memory_database": memory_database,
            "embedding_model": embedding_model,
            "components": {
                "gateway": {
                    "state": "running",
                    "uptime_seconds": services.telemetry.uptime,
                },
                "llm": backend,
                "memory_database": memory_database,
                "embedding": embedding_model,
            },
            "network": (
                services.network_runtime.status_dict()
                if services.network_runtime is not None
                else None
            ),
            "uptime_seconds": services.telemetry.uptime,
            "metrics": process_metrics,
            "agent": agent_status,
        })

    @app.get("/api/v1/conversations")
    async def conversations(_: str = Depends(authenticated)) -> list[dict[str, object]]:
        return await services.conversations.list_all()

    @app.get("/api/v1/conversations/{conversation_id}/messages")
    async def messages(
        conversation_id: str, _: str = Depends(authenticated)
    ) -> list[dict[str, object]]:
        return await services.conversations.messages(conversation_id)

    @app.get("/api/v1/conversations/{conversation_id}/tool-calls")
    async def conversation_tool_calls(
        conversation_id: str, _: str = Depends(authenticated)
    ) -> list[dict[str, object]]:
        """Return metadata-only cards; raw arguments and results are never stored."""

        rows = await services.db.fetchall(
            """
            SELECT tool_call_id,request_id,target_client_id,target_session_id,
                   tool_name,tool_version,risk_level,arguments_summary,status,
                   approval_mode,result_summary,error_code,created_at,updated_at,
                   duration_ms
            FROM tool_calls
            WHERE conversation_id=?
            ORDER BY created_at,tool_call_id
            """,
            (conversation_id,),
        )
        return [
            {
                "tool_call_id": str(row["tool_call_id"]),
                "request_id": str(row["request_id"]),
                "target_client_id": str(row["target_client_id"]),
                "target_session_id": str(row["target_session_id"]),
                "tool_name": str(row["tool_name"]),
                "tool_version": str(row["tool_version"]),
                "risk_level": str(row["risk_level"]),
                "arguments_summary": str(row["arguments_summary"] or ""),
                "status": str(row["status"]),
                "approval_mode": str(row["approval_mode"]),
                "result_summary": str(row["result_summary"] or ""),
                "error_code": (
                    str(row["error_code"]) if row["error_code"] is not None else None
                ),
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
                "duration_ms": row["duration_ms"],
            }
            for row in rows
        ]

    @app.get("/api/v1/persona")
    async def get_persona(_: str = Depends(administrator)) -> dict[str, Any]:
        return services.persona.load().model_dump(mode="json")

    @app.put("/api/v1/persona")
    async def save_persona(
        body: dict[str, Any], _: str = Depends(administrator)
    ) -> dict[str, Any]:
        try:
            return (await services.persona.save(body)).model_dump(mode="json")
        except PersonaStorageError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        except PersonaRecoveryError as exc:
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc)) from exc

    @app.get("/api/v1/settings")
    async def all_settings(_: str = Depends(administrator)) -> dict[str, Any]:
        return {
            name: services.config.load(name).model_dump(mode="json")
            for name in ("server", "models", "inference", "memory", "agent")
        }

    @app.get("/api/v1/settings/revisions")
    async def revisions(_: str = Depends(administrator)) -> list[dict[str, object]]:
        return [
            dict(row)
            for row in await services.db.fetchall(
                "SELECT * FROM settings_revisions ORDER BY id DESC"
            )
        ]

    @app.get("/api/v1/settings/{section}")
    async def get_settings(section: str, _: str = Depends(administrator)) -> dict[str, Any]:
        try:
            return services.config.load(section).model_dump(mode="json")
        except KeyError as exc:
            raise HTTPException(404, "설정 섹션이 없습니다.") from exc

    @app.put("/api/v1/settings/{section}")
    async def put_settings(
        section: str, body: dict[str, Any], client_id: str = Depends(administrator)
    ) -> dict[str, Any]:
        try:
            return (await services.config.save(section, body, client_id)).model_dump(mode="json")
        except KeyError as exc:
            raise HTTPException(404, "설정 섹션이 없습니다.") from exc

    @app.post("/api/v1/settings/rollback/{revision_id}")
    async def rollback(
        revision_id: int, client_id: str = Depends(administrator)
    ) -> dict[str, Any]:
        try:
            return (await services.config.rollback(revision_id, client_id)).model_dump(mode="json")
        except KeyError as exc:
            raise HTTPException(404, "설정 변경 이력이 없습니다.") from exc

    @app.post("/api/v1/settings/validate")
    async def validate(body: dict[str, Any], _: str = Depends(administrator)) -> dict[str, bool]:
        section = str(body.get("section", ""))
        value = body.get("value")
        if not isinstance(value, dict):
            raise HTTPException(422, "설정 value는 JSON 객체여야 합니다.")
        try:
            services.config.validate(section, value, require_complete=True)
        except KeyError as exc:
            raise HTTPException(404, "설정 섹션이 없습니다.") from exc
        return {"valid": True}

    @app.get("/api/v1/audio-analysis/capabilities")
    async def audio_analysis_capabilities(
        _: str = Depends(administrator),
    ) -> dict[str, Any]:
        return audio_capabilities()

    @app.post("/api/v1/audio-analysis/jobs", status_code=202)
    async def create_audio_analysis_job(
        request: Request,
        _: str = Depends(administrator),
    ) -> dict[str, Any]:
        raw_name = request.headers.get("x-nivelle-filename", "audio.wav")
        try:
            filename = unquote(raw_name, encoding="utf-8", errors="strict").strip()
        except (UnicodeDecodeError, ValueError) as exc:
            raise HTTPException(400, "오디오 파일 이름이 올바르지 않습니다.") from exc
        if (
            not filename
            or len(filename) > 255
            or Path(filename).name != filename
            or re.search(r'[<>:"/\\|?*\x00-\x1f]', filename) is not None
        ):
            raise HTTPException(400, "오디오 파일 이름이 올바르지 않습니다.")
        suffix = Path(filename).suffix.casefold()
        if suffix not in SUPPORTED_EXTENSIONS:
            raise HTTPException(415, "지원하지 않는 오디오 파일 형식입니다.")
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared_size = int(content_length)
            except ValueError as exc:
                raise HTTPException(400, "Content-Length가 올바르지 않습니다.") from exc
            if declared_size < 0:
                raise HTTPException(400, "Content-Length가 올바르지 않습니다.")
            if declared_size == 0:
                raise HTTPException(400, "빈 오디오 파일은 분석할 수 없습니다.")
            if declared_size > MAX_UPLOAD_BYTES:
                raise HTTPException(413, "오디오 파일은 256 MiB 이하여야 합니다.")

        upload_directory = services.audio_analysis.upload_directory
        upload_directory.mkdir(parents=True, exist_ok=True)
        upload_path = upload_directory / f"{uuid4().hex}{suffix}"
        digest = hashlib.sha256()
        size_bytes = 0
        try:
            with upload_path.open("xb") as stream:
                async for chunk in request.stream():
                    size_bytes += len(chunk)
                    if size_bytes > MAX_UPLOAD_BYTES:
                        raise HTTPException(413, "오디오 파일은 256 MiB 이하여야 합니다.")
                    digest.update(chunk)
                    stream.write(chunk)
                stream.flush()
            if size_bytes < 1:
                raise HTTPException(400, "빈 오디오 파일은 분석할 수 없습니다.")
            return services.audio_analysis.start(
                upload_path,
                filename=filename,
                content_hash=digest.hexdigest(),
                size_bytes=size_bytes,
            )
        except BaseException:
            upload_path.unlink(missing_ok=True)
            raise

    @app.get("/api/v1/audio-analysis/jobs/{job_id}")
    async def get_audio_analysis_job(
        job_id: str,
        _: str = Depends(administrator),
    ) -> dict[str, Any]:
        value = services.audio_analysis.get(job_id)
        if value is None:
            raise HTTPException(404, "오디오 분석 작업을 찾을 수 없습니다.")
        return value

    @app.delete("/api/v1/audio-analysis/jobs/{job_id}")
    async def cancel_audio_analysis_job(
        job_id: str,
        _: str = Depends(administrator),
    ) -> dict[str, Any]:
        value = services.audio_analysis.cancel(job_id)
        if value is None:
            raise HTTPException(404, "오디오 분석 작업을 찾을 수 없습니다.")
        return value

    @app.websocket("/ws/v1/agent")
    async def agent_channel(websocket: WebSocket) -> None:
        auth = websocket.headers.get("authorization", "")
        client_id = (
            await services.pairing.verify(auth[7:])
            if auth.startswith("Bearer ")
            else None
        )
        if not client_id:
            await websocket.close(4401, "인증이 필요합니다.")
            return
        agent_settings = AgentSettings.model_validate(services.config.load("agent"))
        if not agent_settings.enabled:
            await websocket.close(4403, "Agent 오케스트레이션이 비활성화되어 있습니다.")
            return
        await websocket.accept()
        services.register_client_websocket(client_id, websocket)
        handle: AgentSessionHandle | None = None
        try:
            capabilities = await websocket.receive_json()
            handle = await services.agent_gateway.register(
                client_id,
                capabilities,
                websocket.send_json,
            )
            while True:
                payload = await websocket.receive_json()
                await services.agent_gateway.handle_message(handle, payload)
        except WebSocketDisconnect:
            pass
        except (AgentGatewayError, ValidationError, ValueError, TypeError):
            try:
                await websocket.close(4400, "Agent 프로토콜 요청이 올바르지 않습니다.")
            except RuntimeError:
                pass
        finally:
            if handle is not None:
                await services.agent_gateway.disconnect(handle)
            services.unregister_client_websocket(client_id, websocket)

    @app.websocket("/ws/v1/chat")
    async def chat(websocket: WebSocket) -> None:
        auth = websocket.headers.get("authorization", "")
        client_id = await services.pairing.verify(auth[7:]) if auth.startswith("Bearer ") else None
        if not client_id:
            await websocket.close(4401, "인증이 필요합니다.")
            return
        await websocket.accept()
        services.register_client_websocket(client_id, websocket)
        tasks: dict[str, asyncio.Task[None]] = {}
        seen_request_ids: set[str] = set()

        async def send_invalid_request(request_id_value: object = None) -> None:
            request_id: UUID | None = None
            if request_id_value is not None:
                try:
                    request_id = UUID(str(request_id_value))
                except (AttributeError, ValueError):
                    pass
            await websocket.send_json(
                ServerEvent(
                    type="error",
                    request_id=request_id,
                    payload={
                        "code": "INVALID_REQUEST",
                        "message": "채팅 요청 형식이 올바르지 않습니다.",
                        "details": {},
                        "retryable": False,
                    },
                ).model_dump(mode="json")
            )

        async def generate(chat_request: ChatRequest) -> None:
            rid = str(chat_request.request_id)
            client_message_id = str(
                chat_request.client_message_id or chat_request.request_id
            )
            counted_generation = False
            claimed_conversation_id: str | None = None
            claimed_request_id = False
            claimed_message_id = False
            claimed_retry_target: str | None = None
            assistant_message_id: str | None = None
            assistant_persisted_completed = False
            completion_send_attempted = False
            chunks: list[str] = []
            generation_metrics: GenerationMetrics | None = None
            model_response: ModelResponse | None = None
            accepted_at: float | None = None
            first_delta_at: float | None = None

            def capture_generation_metrics(value: GenerationMetrics) -> None:
                nonlocal generation_metrics
                generation_metrics = value

            def observed_generation_metrics(
                base: GenerationMetrics | None,
                *,
                finish_reason: str,
                interrupted: bool,
            ) -> GenerationMetrics:
                """Overlay timings the Gateway can measure from chat acceptance."""

                finished_at = time.perf_counter()
                first_latency_ms = (
                    max((first_delta_at - accepted_at) * 1000, 0.0)
                    if accepted_at is not None and first_delta_at is not None
                    else None
                )
                total_latency_ms = (
                    max((finished_at - accepted_at) * 1000, 0.0)
                    if accepted_at is not None
                    else None
                )
                current = base or GenerationMetrics()
                tokens_per_second = current.tokens_per_second
                if (
                    tokens_per_second is None
                    and current.completion_tokens is not None
                    and first_latency_ms is not None
                    and total_latency_ms is not None
                ):
                    generation_seconds = (total_latency_ms - first_latency_ms) / 1000
                    if generation_seconds > 0:
                        tokens_per_second = (
                            current.completion_tokens / generation_seconds
                        )
                return current.model_copy(
                    update={
                        "first_token_latency_ms": first_latency_ms,
                        "total_latency_ms": total_latency_ms,
                        "tokens_per_second": tokens_per_second,
                        "finish_reason": finish_reason,
                        "interrupted": interrupted,
                        "request_id": rid,
                    }
                )

            async def emit_durable_completion(message: dict[str, object]) -> None:
                """Report the DB truth if cancellation races a completion commit."""

                nonlocal completion_send_attempted
                if completion_send_attempted:
                    return
                canonical_message_id = _validated_persisted_assistant_message_id(
                    message, assistant_message_id
                )
                completion_send_attempted = True
                metrics = services.last_request_metrics
                if (
                    metrics is None
                    or metrics.interrupted
                    or metrics.request_id != rid
                ):
                    metrics = observed_generation_metrics(
                        generation_metrics,
                        finish_reason="stop",
                        interrupted=False,
                    )
                    services.last_request_metrics = metrics
                await websocket.send_json(
                    ServerEvent(
                        type="assistant.completed",
                        request_id=chat_request.request_id,
                        payload=AssistantCompletedPayload(
                            conversation_id=UUID(str(conversation_id)),
                            client_message_id=UUID(client_message_id),
                            message_id=UUID(canonical_message_id),
                            assistant_message_id=UUID(canonical_message_id),
                            message=CompletedAssistantMessage.model_validate(message),
                            finish_reason=metrics.finish_reason,
                            metrics=metrics.model_dump(mode="json"),
                        ).model_dump(mode="json"),
                    ).model_dump(mode="json")
                )

            try:
                async with services.message_idempotency_lock:
                    existing_request = await services.conversations.find_user_by_request_id(rid)
                    if existing_request is not None:
                        await websocket.send_json(
                            ServerEvent(
                                type="error",
                                request_id=chat_request.request_id,
                                payload={
                                    "code": "DUPLICATE_REQUEST",
                                    "message": "같은 요청 ID는 다시 사용할 수 없습니다.",
                                    "details": {
                                        "state": "completed",
                                        "request_id": rid,
                                        "existing_message_id": existing_request.get("id"),
                                        "conversation_id": existing_request.get(
                                            "conversation_id"
                                        ),
                                    },
                                    "retryable": False,
                                },
                            ).model_dump(mode="json")
                        )
                        return
                    if rid in services.inflight_request_ids:
                        await websocket.send_json(
                            ServerEvent(
                                type="error",
                                request_id=chat_request.request_id,
                                payload={
                                    "code": "DUPLICATE_REQUEST",
                                    "message": "같은 요청 ID가 이미 처리 중입니다.",
                                    "details": {"state": "inflight", "request_id": rid},
                                    "retryable": False,
                                },
                            ).model_dump(mode="json")
                        )
                        return
                    services.inflight_request_ids.add(rid)
                    claimed_request_id = True
                    retry_of = (
                        str(chat_request.retry_of_client_message_id)
                        if chat_request.retry_of_client_message_id is not None
                        else None
                    )
                    if retry_of is not None:
                        target = await services.conversations.retry_target(retry_of)
                        if target is None:
                            await websocket.send_json(
                                ServerEvent(
                                    type="error",
                                    request_id=chat_request.request_id,
                                    payload={
                                        "code": "RETRY_TARGET_NOT_FOUND",
                                        "message": "재시도할 원래 메시지를 찾을 수 없습니다.",
                                        "details": {
                                            "retry_of_client_message_id": retry_of
                                        },
                                        "retryable": False,
                                    },
                                ).model_dump(mode="json")
                            )
                            return
                        existing_retry = await services.conversations.find_retry_by_target(
                            retry_of
                        )
                        if existing_retry is not None:
                            await websocket.send_json(
                                ServerEvent(
                                    type="error",
                                    request_id=chat_request.request_id,
                                    payload={
                                        "code": "RETRY_ALREADY_CREATED",
                                        "message": "이 중단 메시지의 재시도가 이미 접수되었습니다.",
                                        "details": {
                                            "retry_of_client_message_id": retry_of,
                                            "retry_client_message_id": existing_retry.get(
                                                "client_message_id"
                                            ),
                                            "conversation_id": existing_retry.get(
                                                "conversation_id"
                                            ),
                                        },
                                        "retryable": False,
                                    },
                                ).model_dump(mode="json")
                            )
                            return
                        target_user, target_assistant = target
                        target_conversation_id = str(target_user["conversation_id"])
                        target_state = (
                            str(target_assistant["state"])
                            if target_assistant is not None
                            else str(target_user["state"])
                        )
                        if target_state != "interrupted":
                            await websocket.send_json(
                                ServerEvent(
                                    type="error",
                                    request_id=chat_request.request_id,
                                    payload={
                                        "code": "RETRY_TARGET_NOT_INTERRUPTED",
                                        "message": (
                                            "완료되었거나 아직 생성 중인 메시지는 "
                                            "재시도할 수 없습니다."
                                        ),
                                        "details": {
                                            "retry_of_client_message_id": retry_of,
                                            "state": target_state,
                                            "conversation_id": target_conversation_id,
                                        },
                                        "retryable": False,
                                    },
                                ).model_dump(mode="json")
                            )
                            return
                        if (
                            chat_request.conversation_id is not None
                            and str(chat_request.conversation_id)
                            != target_conversation_id
                        ):
                            await websocket.send_json(
                                ServerEvent(
                                    type="error",
                                    request_id=chat_request.request_id,
                                    payload={
                                        "code": "RETRY_CONVERSATION_MISMATCH",
                                        "message": "원래 메시지와 같은 대화에서만 재시도할 수 있습니다.",
                                        "details": {
                                            "retry_of_client_message_id": retry_of,
                                            "conversation_id": target_conversation_id,
                                        },
                                        "retryable": False,
                                    },
                                ).model_dump(mode="json")
                            )
                            return
                        if retry_of in services.inflight_retry_targets:
                            await websocket.send_json(
                                ServerEvent(
                                    type="error",
                                    request_id=chat_request.request_id,
                                    payload={
                                        "code": "RETRY_ALREADY_CREATED",
                                        "message": "이 중단 메시지의 재시도가 이미 진행 중입니다.",
                                        "details": {
                                            "retry_of_client_message_id": retry_of,
                                            "state": "inflight",
                                            "conversation_id": target_conversation_id,
                                        },
                                        "retryable": False,
                                    },
                                ).model_dump(mode="json")
                            )
                            return
                        services.inflight_retry_targets.add(retry_of)
                        claimed_retry_target = retry_of
                        chat_request.conversation_id = UUID(target_conversation_id)

                    existing = await services.conversations.find_user_by_client_message_id(
                        client_message_id
                    )
                    if (
                        existing is not None
                        or client_message_id in services.inflight_message_ids
                    ):
                        details: dict[str, object] = {
                            "client_message_id": client_message_id,
                            "state": "already_received",
                        }
                        if existing is not None:
                            details.update(
                                {
                                    "conversation_id": existing["conversation_id"],
                                    "user_message_id": existing["id"],
                                }
                            )
                        await websocket.send_json(
                            ServerEvent(
                                type="error",
                                request_id=chat_request.request_id,
                                payload={
                                    "code": "DUPLICATE_MESSAGE",
                                    "message": "이미 접수된 메시지입니다. 자동으로 다시 보내지 않습니다.",
                                    "details": details,
                                    "retryable": False,
                                },
                            ).model_dump(mode="json")
                        )
                        return
                    services.inflight_message_ids.add(client_message_id)
                    claimed_message_id = True

                if chat_request.conversation_id:
                    conversation_id = str(chat_request.conversation_id)
                    if conversation_id in services.active_conversation_generations:
                        await websocket.send_json(
                            ServerEvent(
                                type="error",
                                request_id=chat_request.request_id,
                                payload={
                                    "code": "CONVERSATION_BUSY",
                                    "message": "이 대화에서 응답을 생성하고 있습니다.",
                                    "details": {"conversation_id": conversation_id},
                                    "retryable": True,
                                },
                            ).model_dump(mode="json")
                        )
                        return
                    services.active_conversation_generations.add(conversation_id)
                    claimed_conversation_id = conversation_id
                    saved_messages = await services.conversations.completed_messages(
                        conversation_id
                    )
                    if saved_messages is None:
                        await websocket.send_json(
                            ServerEvent(
                                type="error",
                                request_id=chat_request.request_id,
                                payload={
                                    "code": "CONVERSATION_NOT_FOUND",
                                    "message": "대화를 찾을 수 없거나 보관된 대화입니다.",
                                    "details": {"conversation_id": conversation_id},
                                    "retryable": False,
                                },
                            ).model_dump(mode="json")
                        )
                        return
                    history = [
                        PromptMessage(str(message["role"]), str(message["content"]))
                        for message in saved_messages
                    ]
                else:
                    conversation_id = None
                    history = []

                inference_settings = InferenceSettings.model_validate(
                    services.config.load("inference")
                )
                memory_settings = MemorySettings.model_validate(services.config.load("memory"))
                retrieval_settings = memory_settings.memory_retrieval.model_copy(
                    update={
                        "enabled": (
                            memory_settings.enabled
                            and memory_settings.memory_retrieval.enabled
                        )
                    }
                )
                retrieval = await MemoryRetriever(
                    services.memories, retrieval_settings
                ).retrieve(
                    chat_request.content,
                    recent_messages=[
                        message.content for message in history if message.role == "user"
                    ],
                    conversation_id=conversation_id,
                    explicitly_attached_memory_ids=(),
                    include_debug_metadata=True,
                )
                models = ModelsSettings.model_validate(services.config.load("models"))
                agent_settings = AgentSettings.model_validate(
                    services.config.load("agent")
                )
                active_tool_capabilities = (
                    await services.agent_gateway.active_capabilities(client_id)
                    if agent_settings.enabled and agent_settings.max_calls_per_turn > 0
                    else None
                )
                tool_definitions = (
                    TOOL_REGISTRY.model_tool_definitions(
                        active_tool_capabilities.tools
                    )
                    if active_tool_capabilities is not None
                    else []
                )
                runtime_context = await build_runtime_prompt_context(
                    services, chat_request, models
                )
                effective_max_output_tokens = services.persona.generation_token_budget(
                    inference_settings.max_output_tokens
                )
                effective_inference_settings = inference_settings.model_copy(
                    update={"max_output_tokens": effective_max_output_tokens}
                )
                try:
                    prompt = services.persona.build(
                        history,
                        chat_request.content,
                        [memory.content for memory in retrieval.selected],
                        runtime_context=runtime_context,
                        tool_definitions=tool_definitions,
                        context_size=effective_inference_settings.context_size,
                        max_output_tokens=effective_inference_settings.max_output_tokens,
                    )
                except PromptTooLargeError as exc:
                    await websocket.send_json(
                        ServerEvent(
                            type="error",
                            request_id=chat_request.request_id,
                            payload={
                                "code": "PROMPT_TOO_LARGE",
                                "message": "현재 요청이 모델의 컨텍스트 크기를 초과합니다.",
                                "details": {
                                    "fixed_characters": exc.fixed_characters,
                                    "input_character_budget": exc.input_character_budget,
                                },
                                "retryable": False,
                            },
                        ).model_dump(mode="json")
                    )
                    return

                if conversation_id is None:
                    conversation = await services.conversations.create(chat_request.content)
                    conversation_id = conversation["id"]
                    services.active_conversation_generations.add(conversation_id)
                    claimed_conversation_id = conversation_id

                # Capture history before storing the current turn so the current
                # user message is added to the prompt exactly once by persona.build.
                try:
                    user_message, assistant_message = (
                        await services.conversations.allocate_turn(
                            conversation_id,
                            chat_request.content,
                            user_metadata={
                                "request_id": rid,
                                "client_message_id": client_message_id,
                                "retry_of_client_message_id": (
                                    str(chat_request.retry_of_client_message_id)
                                    if chat_request.retry_of_client_message_id is not None
                                    else None
                                ),
                            },
                            client_message_id=client_message_id,
                            request_id=rid,
                            retry_of_client_message_id=(
                                str(chat_request.retry_of_client_message_id)
                                if chat_request.retry_of_client_message_id is not None
                                else None
                            ),
                            assistant_metadata={
                                "request_id": rid,
                                "in_reply_to_client_message_id": client_message_id,
                            },
                        )
                    )
                except aiosqlite.IntegrityError:
                    duplicate_user = (
                        await services.conversations.find_user_by_client_message_id(
                            client_message_id
                        )
                    )
                    duplicate_request = (
                        await services.conversations.find_user_by_request_id(rid)
                    )
                    duplicate_retry = (
                        await services.conversations.find_retry_by_target(
                            claimed_retry_target
                        )
                        if claimed_retry_target is not None
                        else None
                    )
                    if (
                        duplicate_user is None
                        and duplicate_request is None
                        and duplicate_retry is None
                    ):
                        raise
                    existing_message = (
                        duplicate_request or duplicate_user or duplicate_retry or {}
                    )
                    await websocket.send_json(
                        ServerEvent(
                            type="error",
                            request_id=chat_request.request_id,
                            payload={
                                "code": (
                                    "DUPLICATE_REQUEST"
                                    if duplicate_request is not None
                                    else "DUPLICATE_MESSAGE"
                                    if duplicate_user is not None
                                    else "RETRY_ALREADY_CREATED"
                                ),
                                "message": (
                                    "같은 요청 ID는 다시 사용할 수 없습니다."
                                    if duplicate_request is not None
                                    else "이미 접수된 메시지입니다."
                                    if duplicate_user is not None
                                    else "이 중단 메시지의 재시도가 이미 접수되었습니다."
                                ),
                                "details": {
                                    "request_id": rid,
                                    "client_message_id": client_message_id,
                                    "retry_of_client_message_id": claimed_retry_target,
                                    "existing_message_id": existing_message.get("id"),
                                },
                                "retryable": False,
                            },
                        ).model_dump(mode="json")
                    )
                    return
                assistant_message_id = str(assistant_message["id"])
                accepted_at = time.perf_counter()
                await websocket.send_json(
                    ServerEvent(
                        type="chat.accepted",
                        request_id=chat_request.request_id,
                        payload={
                            "conversation_id": conversation_id,
                            "user_message_id": user_message["id"],
                            "assistant_message_id": assistant_message_id,
                            "client_message_id": client_message_id,
                        },
                    ).model_dump(mode="json")
                )
                await websocket.send_json(
                    ServerEvent(
                        type="assistant.context",
                        request_id=chat_request.request_id,
                        payload=AssistantContextPayload(
                            conversation_id=UUID(conversation_id),
                            user_message_id=UUID(str(user_message["id"])),
                            assistant_message_id=UUID(assistant_message_id),
                            client_message_id=UUID(client_message_id),
                            query=chat_request.content,
                            retrieval=RetrievalContext(
                                backend=retrieval.backend,
                                top_k=retrieval.top_k,
                                candidate_count=retrieval.candidate_count,
                            ),
                            memories=list(retrieval.memories),
                        ).model_dump(mode="json"),
                    ).model_dump(mode="json")
                )
                services.active_generations += 1
                counted_generation = True
                services.last_llm_error = None
                provider = services.provider()
                if isinstance(provider, LlamaCppServerProvider):
                    # Services.provider creates a fresh provider per request, so
                    # applying Persona's concise budget cannot affect another turn.
                    provider.inference = effective_inference_settings
                model_router = services.model_router(
                    provider,
                    models,
                    effective_inference_settings,
                    on_metrics=capture_generation_metrics,
                )
                tool_results: list[dict[str, Any]] = []
                planner = getattr(provider, "plan_tools", None)
                if (
                    tool_definitions
                    and active_tool_capabilities is not None
                    and callable(planner)
                ):
                    try:
                        proposals = await planner(
                            prompt,
                            tool_definitions,
                            max_calls=agent_settings.max_calls_per_turn,
                        )
                    except (httpx.HTTPError, ValidationError, ValueError, TypeError):
                        proposals = []
                        tool_results = [planning_failure()]
                    if proposals:
                        tool_results = await execute_tool_proposals(
                            proposals,
                            request_id=chat_request.request_id,
                            conversation_id=UUID(str(conversation_id)),
                            user_message_id=UUID(str(user_message["id"])),
                            assistant_message_id=UUID(assistant_message_id),
                            capabilities=active_tool_capabilities,
                            settings=agent_settings,
                            orchestrator=services.tool_orchestrator,
                            gateway=services.agent_gateway,
                        )
                if tool_results:
                    try:
                        prompt = services.persona.build(
                            history,
                            chat_request.content,
                            [memory.content for memory in retrieval.selected],
                            runtime_context=runtime_context,
                            tool_definitions=tool_definitions,
                            tool_results=tool_results,
                            context_size=effective_inference_settings.context_size,
                            max_output_tokens=effective_inference_settings.max_output_tokens,
                        )
                    except PromptTooLargeError:
                        compact_results = [
                            {
                                "source_tool": item.get("source_tool"),
                                "trusted": False,
                                "result_id": item.get("result_id"),
                                "status": item.get("status"),
                                "safe_summary": item.get("safe_summary"),
                                "truncated": True,
                                "error_code": item.get("error_code"),
                            }
                            for item in tool_results
                        ]
                        prompt = services.persona.build(
                            history,
                            chat_request.content,
                            [memory.content for memory in retrieval.selected],
                            runtime_context=runtime_context,
                            tool_definitions=tool_definitions,
                            tool_results=compact_results,
                            context_size=effective_inference_settings.context_size,
                            max_output_tokens=effective_inference_settings.max_output_tokens,
                        )
                model_request = ModelRequest(
                    messages=tuple(prompt),
                    request_id=rid,
                    conversation_id=conversation_id,
                )
                async for runtime_event in model_router.stream(model_request):
                    if runtime_event.type == "final":
                        model_response = runtime_event.response
                        continue
                    chunk = runtime_event.delta
                    if first_delta_at is None:
                        first_delta_at = time.perf_counter()
                    chunks.append(chunk)
                    await websocket.send_json(
                        ServerEvent(
                            type="assistant.delta",
                            request_id=chat_request.request_id,
                            payload={
                                "assistant_message_id": assistant_message_id,
                                "sequence": len(chunks),
                                "delta": chunk,
                            },
                        ).model_dump(mode="json")
                    )
                completed_metrics = observed_generation_metrics(
                    generation_metrics,
                    finish_reason=(
                        generation_metrics.finish_reason
                        if generation_metrics is not None
                        and generation_metrics.finish_reason is not None
                        else "stop"
                    ),
                    interrupted=False,
                ).model_copy(
                    update={
                        "model": (
                            generation_metrics.model
                            if generation_metrics is not None
                            and generation_metrics.model is not None
                            else model_response.model_id
                            if model_response is not None
                            else "Mock LLM"
                            if models.mode == "mock"
                            else runtime_context.get("loaded_model")
                            if isinstance(runtime_context.get("loaded_model"), str)
                            else None
                        )
                    }
                )
                services.last_request_metrics = completed_metrics
                message = await services.conversations.update_message(
                    assistant_message_id,
                    content="".join(chunks).rstrip(),
                    state="completed",
                    prompt_tokens=completed_metrics.prompt_tokens,
                    completion_tokens=completed_metrics.completion_tokens,
                    expected_state="generating",
                )
                if message is None:
                    raise RuntimeError("allocated assistant message disappeared")
                assistant_persisted_completed = True
                await emit_durable_completion(message)
            except asyncio.CancelledError:
                if assistant_persisted_completed:
                    # A cancellation while delivering the completion event cannot
                    # undo a durable completed assistant message.
                    raise
                interrupted_metrics = observed_generation_metrics(
                    generation_metrics,
                    finish_reason="cancelled",
                    interrupted=True,
                )
                persisted_message: dict[str, object] | None = None
                if assistant_message_id is not None:
                    persisted_message = await services.conversations.update_message(
                        assistant_message_id,
                        content="".join(chunks).rstrip(),
                        state="interrupted",
                        prompt_tokens=interrupted_metrics.prompt_tokens,
                        completion_tokens=interrupted_metrics.completion_tokens,
                        expected_state="generating",
                    )
                if (
                    persisted_message is not None
                    and persisted_message.get("state") == "completed"
                ):
                    assistant_persisted_completed = True
                    try:
                        await emit_durable_completion(persisted_message)
                    except (RuntimeError, WebSocketDisconnect):
                        pass
                    raise
                services.last_request_metrics = interrupted_metrics
                try:
                    await websocket.send_json(
                        ServerEvent(
                            type="chat.cancelled",
                            request_id=chat_request.request_id,
                            payload={
                                "client_message_id": client_message_id,
                                "assistant_message_id": assistant_message_id,
                                "state": "interrupted",
                                "metrics": interrupted_metrics.model_dump(mode="json"),
                            },
                        ).model_dump(mode="json")
                    )
                except (RuntimeError, WebSocketDisconnect):
                    pass
                raise
            except Exception as exc:
                if assistant_persisted_completed:
                    # Delivery acknowledgement can fail after the database commit.
                    # The durable message is still complete and must never regress
                    # to interrupted merely because this socket disappeared.
                    return
                interrupted_metrics = observed_generation_metrics(
                    generation_metrics,
                    finish_reason="error",
                    interrupted=True,
                )
                persisted_message = None
                if assistant_message_id is not None:
                    persisted_message = await services.conversations.update_message(
                        assistant_message_id,
                        content="".join(chunks).rstrip(),
                        state="interrupted",
                        prompt_tokens=interrupted_metrics.prompt_tokens,
                        completion_tokens=interrupted_metrics.completion_tokens,
                        expected_state="generating",
                    )
                if (
                    persisted_message is not None
                    and persisted_message.get("state") == "completed"
                ):
                    assistant_persisted_completed = True
                    services.last_llm_error = None
                    try:
                        await emit_durable_completion(persisted_message)
                    except (RuntimeError, WebSocketDisconnect):
                        pass
                    return
                services.last_llm_error = type(exc).__name__
                services.last_request_metrics = interrupted_metrics
                try:
                    await websocket.send_json(
                        ServerEvent(
                            type="error",
                            request_id=chat_request.request_id,
                            payload={
                                "code": "LLM_STREAM_INTERRUPTED",
                                "message": "응답 생성이 중단되었습니다.",
                                "details": {
                                    "reason": type(exc).__name__,
                                    "client_message_id": client_message_id,
                                    "assistant_message_id": assistant_message_id,
                                    "state": "interrupted",
                                    "metrics": interrupted_metrics.model_dump(mode="json"),
                                },
                                "retryable": True,
                            },
                        ).model_dump(mode="json")
                    )
                except (RuntimeError, WebSocketDisconnect):
                    pass
            finally:
                if counted_generation:
                    services.active_generations = max(services.active_generations - 1, 0)
                if claimed_conversation_id is not None:
                    services.active_conversation_generations.discard(
                        claimed_conversation_id
                    )
                if claimed_request_id:
                    services.inflight_request_ids.discard(rid)
                if claimed_message_id:
                    services.inflight_message_ids.discard(client_message_id)
                if claimed_retry_target is not None:
                    services.inflight_retry_targets.discard(claimed_retry_target)
                tasks.pop(rid, None)

        try:
            while True:
                data = await websocket.receive_json()
                if not isinstance(data, dict):
                    await send_invalid_request()
                    continue
                remote_protocol = str(data.get("protocol_version", ""))
                if not is_protocol_compatible(remote_protocol):
                    compatibility = protocol_compatibility(remote_protocol)
                    await websocket.send_json(
                        ServerEvent(
                            type="error",
                            payload={
                                "code": "PROTOCOL_VERSION_MISMATCH",
                                "message": "클라이언트와 서버 프로토콜 주 버전이 호환되지 않습니다.",
                                "details": compatibility.model_dump(mode="json"),
                                "retryable": False,
                            },
                        ).model_dump(mode="json")
                    )
                    continue
                if data.get("type") == "chat.request":
                    try:
                        request = ChatRequest.model_validate(data)
                    except ValidationError:
                        await send_invalid_request(data.get("request_id"))
                        continue
                    request_key = str(request.request_id)
                    current_task = tasks.get(request_key)
                    if request_key in seen_request_ids:
                        await websocket.send_json(
                            ServerEvent(
                                type="error",
                                request_id=request.request_id,
                                payload={
                                    "code": "DUPLICATE_REQUEST",
                                    "message": "같은 요청 ID는 다시 사용할 수 없습니다.",
                                    "details": {
                                        "state": (
                                            "active"
                                            if current_task is not None
                                            and not current_task.done()
                                            else "completed"
                                        )
                                    },
                                    "retryable": False,
                                },
                            ).model_dump(mode="json")
                        )
                        continue
                    seen_request_ids.add(request_key)
                    tasks[request_key] = asyncio.create_task(generate(request))
                elif data.get("type") == "chat.cancel" and (
                    task := tasks.get(str(data.get("request_id")))
                ):
                    task.cancel()
                elif data.get("type") == "ping":
                    await websocket.send_json(
                        {"type": "pong", "protocol_version": PROTOCOL_VERSION}
                    )
                else:
                    await websocket.send_json(
                        {
                            "type": "error",
                            "payload": {
                                "code": "INTERNAL_ERROR",
                                "message": "알 수 없는 이벤트입니다.",
                            },
                        }
                    )
        except WebSocketDisconnect:
            pending_tasks = list(tasks.values())
            for task in pending_tasks:
                task.cancel()
            if pending_tasks:
                await asyncio.gather(*pending_tasks, return_exceptions=True)
        finally:
            services.unregister_client_websocket(client_id, websocket)

    return app
