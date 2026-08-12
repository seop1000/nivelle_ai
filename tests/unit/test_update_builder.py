from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BUILD_SCRIPT = PROJECT_ROOT / "scripts" / "build_update.ps1"
POWERSHELL = shutil.which("powershell")
LEGACY_PORTABLE = PROJECT_ROOT / "dist" / "Nozomi-Windows-x64-0.3.1.zip"


def _write(root: Path, relative_path: str, content: bytes | str) -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


def _sha256(content: bytes | str) -> str:
    if isinstance(content, str):
        content = content.encode()
    return hashlib.sha256(content).hexdigest()


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is required")
def test_builder_creates_safe_deterministic_delta(tmp_path: Path) -> None:
    assert POWERSHELL is not None
    base = tmp_path / "base"
    current = tmp_path / "current"

    _write(base, "same.txt", "same")
    _write(base, "changed.txt", "old")
    _write(base, "removed.txt", "remove me")
    _write(base, "config/examples/default.yaml", "old: true\n")
    _write(base, "config/private.yaml", "user: old\n")
    _write(base, ".env", "SECRET=old\n")
    _write(base, ".nozomi/run.lock", "123")
    _write(base, ".nivelle/run.lock", "123")
    _write(base, "runtime/models/model.gguf", b"model")
    _write(base, "data/nivelle.db", b"database")

    _write(current, "VERSION", "2.0.0\n")
    _write(current, "same.txt", "same")
    _write(current, "changed.txt", "new")
    _write(current, "added.txt", "added")
    _write(current, "config/examples/default.yaml", "old: false\n")
    _write(current, "config/private.yaml", "user: new\n")
    _write(current, ".env", "SECRET=new\n")
    _write(current, ".nozomi/run.lock", "456")
    _write(current, ".nivelle/run.lock", "456")
    _write(current, "runtime/models/model.gguf", b"new model")
    _write(current, "data/nivelle.db", b"new database")
    _write(current, "Nozomi-Windows-x64-0.2.1/runtime/model.gguf", b"extracted model")
    _write(current, "Nozomi-Update-0.1.0-to-0.2.1/payload/old.py", "old package")
    _write(current, "Nivelle-Windows-x64-0.4.0/runtime/model.gguf", b"extracted model")
    _write(current, "Nivelle-Update-0.4.0-to-0.4.1/payload/old.py", "old package")
    _write(current, "Nivelle-Update.cmd", "@echo off\r\n")
    _write(current, "scripts/apply_update.ps1", "param()\n")

    base_zip = tmp_path / "base.zip"
    with zipfile.ZipFile(base_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(item for item in base.rglob("*") if item.is_file()):
            archive.write(path, path.relative_to(base).as_posix())

    outputs = [tmp_path / "update-one.zip", tmp_path / "update-two.zip"]
    for output, base_source in zip(outputs, (base, base_zip), strict=True):
        subprocess.run(
            [
                POWERSHELL,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(BUILD_SCRIPT),
                "-BasePath",
                str(base_source),
                "-ProjectRoot",
                str(current),
                "-FromVersion",
                "1.0.0",
                "-ToVersion",
                "2.0.0",
                "-OutputPath",
                str(output),
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    assert outputs[0].read_bytes() == outputs[1].read_bytes()
    for output in outputs:
        sidecar = output.with_name(f"{output.name}.sha256")
        expected_hash = hashlib.sha256(output.read_bytes()).hexdigest()
        assert sidecar.read_text(encoding="utf-8") == f"{expected_hash} *{output.name}\n"

    with zipfile.ZipFile(outputs[0]) as archive:
        names = archive.namelist()
        assert names[0] == "manifest.json"
        assert set(names[1:3]) == {"apply_update.ps1", "Nivelle-Update.cmd"}
        manifest = json.loads(archive.read("manifest.json"))

        assert manifest["format_version"] == 1
        assert manifest["product"] == "Nivelle"
        assert manifest["from_version"] == "1.0.0"
        assert manifest["to_version"] == "2.0.0"
        assert manifest["min_updater_version"] == "1.0"
        assert manifest["architecture"] == "windows-x64"
        assert manifest["dependency_hash"] is None

        files = {item["path"]: item for item in manifest["files"]}
        assert list(files) == sorted(files)
        assert "same.txt" not in files
        assert files["changed.txt"] == {
            "path": "changed.txt",
            "sha256": _sha256("new"),
            "size": 3,
            "base_sha256": _sha256("old"),
        }
        assert files["added.txt"]["base_sha256"] is None
        assert files["VERSION"]["base_sha256"] is None
        assert files["config/examples/default.yaml"]["base_sha256"] == _sha256(
            (base / "config/examples/default.yaml").read_bytes()
        )

        deletion = {item["path"]: item for item in manifest["deletions"]}
        assert deletion == {
            "removed.txt": {
                "path": "removed.txt",
                "base_sha256": _sha256("remove me"),
            }
        }

        excluded = {
            ".env",
            ".nozomi/run.lock",
            ".nivelle/run.lock",
            "config/private.yaml",
            "data/nivelle.db",
            "runtime/models/model.gguf",
            "Nozomi-Windows-x64-0.2.1/runtime/model.gguf",
            "Nozomi-Update-0.1.0-to-0.2.1/payload/old.py",
            "Nivelle-Windows-x64-0.4.0/runtime/model.gguf",
            "Nivelle-Update-0.4.0-to-0.4.1/payload/old.py",
        }
        assert not (excluded & set(files))
        assert not (excluded & set(deletion))

        for path, item in files.items():
            payload = archive.read(f"payload/{path}")
            assert len(payload) == item["size"]
            assert hashlib.sha256(payload).hexdigest() == item["sha256"]


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is required")
def test_031_to_040_uses_exact_legacy_bridge_asset_and_manifest(tmp_path: Path) -> None:
    assert POWERSHELL is not None
    base = tmp_path / "base"
    current = tmp_path / "current"
    _write(base, "VERSION", "0.3.1\n")
    _write(base, "old.txt", "old")
    _write(current, "VERSION", "0.4.0\n")
    _write(current, "old.txt", "new")
    _write(current, "Nivelle-Update.cmd", "@echo off\r\n")
    _write(current, "Nozomi-Update.cmd", "@echo off\r\n")
    _write(current, "scripts/apply_update.ps1", "param()\n")

    subprocess.run(
        [
            POWERSHELL,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(BUILD_SCRIPT),
            "-BasePath",
            str(base),
            "-ProjectRoot",
            str(current),
        ],
        check=True,
        capture_output=True,
    )

    output = current / "dist" / "Nozomi-Update-0.3.1-to-0.4.0.zip"
    assert output.is_file()
    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())
        root_names = {name for name in names if "/" not in name}
        manifest = json.loads(archive.read("manifest.json"))
    assert manifest["product"] == "Nozomi"
    assert root_names == {"manifest.json", "Nozomi-Update.cmd", "apply_update.ps1"}
    assert "payload/Nivelle-Update.cmd" in names


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is required")
@pytest.mark.skipif(
    not LEGACY_PORTABLE.is_file(),
    reason="The actual 0.3.1 portable artifact is required for this release-path test",
)
def test_legacy_031_apply_script_accepts_generated_transition_package(
    tmp_path: Path,
) -> None:
    """Exercise the updater script shipped inside the actual 0.3.1 portable."""

    assert POWERSHELL is not None
    install = tmp_path / "legacy-install"
    package = tmp_path / "Nozomi-Update-0.3.1-to-0.4.0.zip"
    state_root = tmp_path / "state"
    with zipfile.ZipFile(LEGACY_PORTABLE) as archive:
        archive.extractall(install)

    subprocess.run(
        [
            POWERSHELL,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(BUILD_SCRIPT),
            "-BasePath",
            str(LEGACY_PORTABLE),
            "-ProjectRoot",
            str(PROJECT_ROOT),
            "-FromVersion",
            "0.3.1",
            "-ToVersion",
            "0.4.0",
            "-OutputPath",
            str(package),
        ],
        check=True,
        capture_output=True,
    )

    environment = {
        **os.environ,
        "LOCALAPPDATA": str(state_root),
        "NOZOMI_NO_PAUSE": "1",
        "NIVELLE_NO_PAUSE": "1",
    }
    subprocess.run(
        [
            POWERSHELL,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(install / "scripts" / "apply_update.ps1"),
            "-PackagePath",
            str(package),
            "-TargetRoot",
            str(install),
        ],
        check=True,
        capture_output=True,
        env=environment,
    )

    assert (install / "VERSION").read_text(encoding="utf-8").strip() == "0.4.0"
    assert (install / "Nivelle-Update.cmd").is_file()
    assert not (install / "Nozomi-Server.exe").exists()
