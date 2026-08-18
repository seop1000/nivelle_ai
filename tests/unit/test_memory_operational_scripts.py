import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest
from nivelle_core.database import Database
from nivelle_core.memory_repository import MemoryRepository
from nivelle_protocol.memory import MemoryCreate

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.asyncio
async def test_runtime_audit_and_powershell_backup_scripts(tmp_path: Path) -> None:
    data_dir = tmp_path / "server-data"
    database_path = data_dir / "database" / "nivelle.db"
    database = Database(database_path)
    await database.initialize()
    memory = await MemoryRepository(database).create(
        MemoryCreate(content="사용자의 기본 호칭은 히냥이이다")
    )

    audit = await asyncio.to_thread(
        subprocess.run,
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "audit_runtime_memories.py"),
            "--database",
            str(database_path),
            "--json",
            "--strict",
        ],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert audit.returncode == 0, audit.stderr
    report = json.loads(audit.stdout)
    assert report["integrity_check"] == "ok"
    assert report["schema_version"] == 9
    assert report["memory_counts"]["active"] == 1
    assert report["issues"] == []
    assert memory.id not in audit.stdout  # healthy audits expose no per-memory content/ID

    destination = tmp_path / "manual-backup"
    backup = await asyncio.to_thread(
        subprocess.run,
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(PROJECT_ROOT / "scripts" / "backup_nivelle_data.ps1"),
            "-DataDir",
            str(data_dir),
            "-Destination",
            str(destination),
        ],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert backup.returncode == 0, backup.stderr
    assert (destination / "nivelle.db").stat().st_size > 0
    manifest = json.loads((destination / "backup-manifest.json").read_text("utf-8-sig"))
    assert manifest["integrity_check"] == "ok"
    assert len(manifest["database_sha256"]) == 64


@pytest.mark.asyncio
async def test_runtime_audit_reports_capability_memory_conflicts_by_id_only(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "server-data"
    database_path = data_dir / "database" / "nivelle.db"
    database = Database(database_path)
    await database.initialize()
    repository = MemoryRepository(database)
    fallback = await repository.create(
        MemoryCreate(content="실패 시 Qwen3.5-4B를 대체 모델로 사용한다")
    )
    vector = await repository.create(
        MemoryCreate(content="Qwen3-Embedding 및 sqlite-vec 벡터 검색이 활성화되어 있다")
    )
    config_dir = data_dir / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "models.yaml").write_text(
        "mode: managed\nfallback_enabled: false\nmodels: []\n", "utf-8"
    )
    (config_dir / "memory.yaml").write_text(
        "search_backend: sqlite\nembedding_provider: null\n", "utf-8"
    )

    audit = await asyncio.to_thread(
        subprocess.run,
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "audit_runtime_memories.py"),
            "--data-dir",
            str(data_dir),
            "--json",
            "--strict",
        ],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert audit.returncode == 2
    report = json.loads(audit.stdout)
    assert {
        (item["memory_id"], item["reason"])
        for item in report["runtime_conflicts"]
    } == {
        (fallback.id, "claims_unconfigured_fallback_model"),
        (vector.id, "claims_unavailable_embedding"),
        (vector.id, "claims_unavailable_sqlite_vec"),
    }
    assert "Qwen3.5-4B" not in audit.stdout
    assert "Qwen3-Embedding" not in audit.stdout
