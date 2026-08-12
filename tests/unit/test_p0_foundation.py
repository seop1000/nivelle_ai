from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from nivelle_link.network import ConnectionManager, ConnectionState
from nivelle_link.storage import connection_profile_from_endpoint, resolve_connection_profiles
from nivelle_protocol.settings import ConnectionProfile, ModelsSettings


def test_gateway_endpoint_is_distinct_from_provider_endpoint(tmp_path: Path) -> None:
    profiles, source = resolve_connection_profiles(
        cli_endpoint="https://gateway.example:9443",
        environment={"NIVELLE_GATEWAY_ENDPOINT": "https://ignored.example"},
        path=tmp_path / "missing.yaml",
    )
    models = ModelsSettings(provider_endpoint="http://provider.example:8080")

    assert profiles == [connection_profile_from_endpoint("https://gateway.example:9443")]
    assert source is not None and source.source.value == "cli"
    assert models.provider_endpoint == "http://provider.example:8080"
    assert "provider_endpoint" not in profiles[0].model_dump()


def test_legacy_provider_name_migrates_but_conflicts_are_rejected() -> None:
    migrated = ModelsSettings.model_validate({"external_url": "http://127.0.0.1:8080"})
    assert migrated.model_dump()["provider_endpoint"] == "http://127.0.0.1:8080"
    assert "external_url" not in migrated.model_dump()
    with pytest.raises(ValueError, match="conflict"):
        ModelsSettings.model_validate(
            {
                "provider_endpoint": "http://127.0.0.1:8080",
                "external_url": "http://127.0.0.1:8081",
            }
        )


@pytest.mark.asyncio
async def test_connecting_deduplicates_the_connection_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ConnectionManager([ConnectionProfile(id="gateway", host="127.0.0.1")])
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def probe(_profile: ConnectionProfile) -> None:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()

    monkeypatch.setattr(manager, "_probe", probe)
    first = asyncio.create_task(manager.connect())
    await entered.wait()
    second = asyncio.create_task(manager.connect())
    await asyncio.sleep(0)
    assert manager.connection_task is not None
    release.set()
    assert await first == await second
    assert calls == 1
    await manager.shutdown()


@pytest.mark.asyncio
async def test_shutdown_is_idempotent_and_blocks_future_tasks() -> None:
    manager = ConnectionManager([ConnectionProfile(id="gateway", host="127.0.0.1")])
    await asyncio.gather(manager.shutdown(), manager.shutdown())
    assert manager.state == ConnectionState.DISCONNECTED
    assert await manager.connect() is None
    assert manager.schedule_reconnect(asyncio.sleep) is None  # type: ignore[arg-type]
    assert manager.connection_task is None
    assert manager.reconnect_task is None


def test_link_runtime_has_no_model_provider_connection_code() -> None:
    link_root = Path(__file__).resolve().parents[2] / "apps" / "client" / "nivelle_link"
    active_source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (link_root / "network.py", link_root / "storage.py")
    )
    assert "NIVELLE_PROVIDER_ENDPOINT" not in active_source
    assert "llama-server" not in active_source
