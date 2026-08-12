import asyncio
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml
from nivelle_protocol.configuration import ResolvedSetting, resolve_endpoint
from nivelle_protocol.settings import (
    AgentSettings,
    InferenceSettings,
    MemorySettings,
    ModelsSettings,
    ServerSettings,
)
from pydantic import BaseModel, ValidationError

from .database import Database
from .repositories import now

MODELS: dict[str, type[BaseModel]] = {
    "server": ServerSettings,
    "models": ModelsSettings,
    "inference": InferenceSettings,
    "memory": MemorySettings,
    "agent": AgentSettings,
}

RESTART_FIELDS: dict[str, set[str]] = {
    "server": {"host", "advertised_host", "port", "log_level", "mock_mode"},
    "models": {"llama_server_path", "provider_endpoint", "models"},
    "inference": {
        "context_size",
        "gpu_layers",
        "threads",
        "batch_size",
        "micro_batch_size",
        "concurrent_requests",
    },
}


class IncompleteSettingsError(ValueError):
    def __init__(self, section: str, missing: set[str]) -> None:
        self.section = section
        self.missing = sorted(missing)
        super().__init__(f"{section} 설정에 필수 필드가 누락되었습니다: {', '.join(self.missing)}")


class ConfigService:
    def __init__(
        self,
        directory: Path,
        db: Database,
        *,
        provider_endpoint_override: str | None = None,
        environment: dict[str, str] | None = None,
    ) -> None:
        self.directory, self.db = directory, db
        self.provider_endpoint_override = provider_endpoint_override
        self.environment = environment if environment is not None else dict(os.environ)
        self.resolved_sources: dict[str, ResolvedSetting[str]] = {}
        self._locks = {section: asyncio.Lock() for section in MODELS}

    def load(self, section: str) -> BaseModel:
        model = self._model(section)
        path = self.directory / f"{section}.yaml"
        raw = yaml.safe_load(path.read_text("utf-8")) or {} if path.exists() else {}
        if not isinstance(raw, dict):
            raise TypeError(f"{section} config root must be an object")
        if section == "models":
            local_endpoint = raw.get("provider_endpoint", raw.get("external_url"))
            resolved = resolve_endpoint(
                "provider_endpoint",
                cli_value=self.provider_endpoint_override,
                environment=self.environment,
                environment_name="NIVELLE_PROVIDER_ENDPOINT",
                local_value=str(local_endpoint) if local_endpoint is not None else None,
                safe_default="http://127.0.0.1:8080",
            )
            raw = dict(raw)
            raw.pop("external_url", None)
            raw["provider_endpoint"] = resolved.value
            self.resolved_sources["provider_endpoint"] = resolved
        return model.model_validate(raw)

    async def save(self, section: str, value: dict[str, Any], client_id: str | None) -> BaseModel:
        validated = self.validate(section, value, require_complete=True)
        async with self._locks[section]:
            previous_model = self.load(section)
            previous = previous_model.model_dump(mode="json")
            current = validated.model_dump(mode="json")
            apply_status = self._apply_status(section, previous, current)
            await self._atomic_write(section, validated)
            try:
                await self.db.execute(
                    "INSERT INTO settings_revisions(section,previous_json,new_json,created_at,client_id,apply_status) VALUES(?,?,?,?,?,?)",
                    (
                        section,
                        json.dumps(previous, ensure_ascii=False),
                        validated.model_dump_json(),
                        now(),
                        client_id,
                        apply_status,
                    ),
                )
            except Exception:
                await self._atomic_write(section, previous_model)
                raise
            return validated

    def validate(
        self, section: str, value: dict[str, Any], *, require_complete: bool
    ) -> BaseModel:
        model = self._model(section)
        # Nivelle Link 0.3.1 sends the complete legacy server shape.  The new
        # optional field must not turn that compatible payload into a 422.
        if section == "server" and "advertised_host" not in value:
            value = dict(value)
            current_server = ServerSettings.model_validate(self.load("server"))
            value["advertised_host"] = current_server.advertised_host
        if require_complete:
            missing = set(model.model_fields) - set(value)
            if missing:
                raise IncompleteSettingsError(section, missing)
        return model.model_validate(value)

    async def rollback(self, revision_id: int, client_id: str | None) -> BaseModel:
        row = await self.db.fetchone(
            "SELECT section,previous_json FROM settings_revisions WHERE id=?", (revision_id,)
        )
        if not row:
            raise KeyError(revision_id)
        return await self.save(str(row["section"]), json.loads(row["previous_json"]), client_id)

    async def _atomic_write(self, section: str, value: BaseModel) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path, temporary = (
            self.directory / f"{section}.yaml",
            self.directory / f".{section}.{uuid4().hex}.yaml.tmp",
        )
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                yaml.safe_dump(
                    value.model_dump(mode="json"), handle, allow_unicode=True, sort_keys=False
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _apply_status(section: str, previous: dict[str, Any], current: dict[str, Any]) -> str:
        restart_fields = RESTART_FIELDS.get(section, set())
        return (
            "pending_restart"
            if any(previous.get(field) != current.get(field) for field in restart_fields)
            else "applied"
        )

    @staticmethod
    def _model(section: str) -> type[BaseModel]:
        if section not in MODELS:
            raise KeyError(section)
        return MODELS[section]


__all__ = ["ConfigService", "IncompleteSettingsError", "ValidationError"]
