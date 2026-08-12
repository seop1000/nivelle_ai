from __future__ import annotations

import hashlib
import importlib.util
import sys
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BUILDER_PATH = PROJECT_ROOT / "scripts" / "build_portable.py"


def _load_builder() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_portable", BUILDER_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write(root: Path, relative: str, content: str | bytes = "test") -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8", newline="\n")


def _minimal_project(root: Path) -> None:
    required = {
        "VERSION": "1.2.3\n",
        "pyproject.toml": (
            '[project]\ndynamic = ["version"]\n'
            '[tool.hatch.version]\npath = "VERSION"\n'
        ),
        "packages/nivelle_protocol/version.py": (
            "def _load_app_version():\n    return '1.2.3'\n"
            "APP_VERSION = _load_app_version()\n"
        ),
    }
    for relative in (
        "nivelle.py",
        "nivelle_runtime.py",
        "Nivelle-Core.exe",
        "Nivelle-Link.exe",
        "Nivelle-Local.exe",
        "Nivelle-Updater.exe",
        "Nivelle-Update.cmd",
        "Nivelle-Rollback.cmd",
        "apps/server/nivelle_core/app.py",
        "apps/client/nivelle_link/main.py",
        "config/examples/server.yaml",
        "packages/nivelle_protocol/persona.py",
        "scripts/apply_update.ps1",
        "scripts/bootstrap_python.ps1",
        "scripts/build_executables.ps1",
        "scripts/build_update.ps1",
        "scripts/nivelle_executable_launcher.py",
        "scripts/rollback_update.ps1",
        "scripts/run_locked.ps1",
        "scripts/update_from_github.ps1",
        "scripts/verify_update.ps1",
    ):
        required[relative] = "payload"
    for relative, content in required.items():
        _write(root, relative, content)


def test_portable_builder_is_deterministic_and_protects_user_data(tmp_path: Path) -> None:
    builder = _load_builder()
    root = tmp_path / "project"
    _minimal_project(root)
    _write(root, ".env", "SECRET=value")
    _write(root, "runtime/models/qwen.gguf", b"model")
    _write(root, ".venv/secret.txt")
    _write(root, ".nozomi/run.lock")
    _write(root, ".nivelle/run.lock")
    _write(root, "config/user.yaml", "private: true")
    _write(root, "data/nivelle.db", b"database")
    _write(root, "logs/nivelle.log")
    _write(root, "Nozomi-Windows-x64-0.2.1/runtime/model.gguf", b"extracted model")
    _write(root, "Nozomi-Update-0.1.0-to-0.2.1/payload/source.py", "old package")
    _write(root, "Nivelle-Windows-x64-0.4.0/runtime/model.gguf", b"extracted model")
    _write(root, "Nivelle-Update-0.4.0-to-0.4.1/payload/source.py", "old package")
    _write(root, "Nozomi-Server.exe", b"legacy binary")
    _write(root, "source.py", "print('included')\n")

    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"
    builder.build_portable(root, first)
    builder.build_portable(root, second)

    assert first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(first) as archive:
        names = set(archive.namelist())
        assert "source.py" in names
        assert "config/examples/server.yaml" in names
        assert not {
            ".env",
            "runtime/models/qwen.gguf",
            ".venv/secret.txt",
            ".nozomi/run.lock",
            ".nivelle/run.lock",
            "config/user.yaml",
            "data/nivelle.db",
            "logs/nivelle.log",
            "Nozomi-Windows-x64-0.2.1/runtime/model.gguf",
            "Nozomi-Update-0.1.0-to-0.2.1/payload/source.py",
            "Nivelle-Windows-x64-0.4.0/runtime/model.gguf",
            "Nivelle-Update-0.4.0-to-0.4.1/payload/source.py",
            "Nozomi-Server.exe",
        } & names

    digest = hashlib.sha256(first.read_bytes()).hexdigest()
    assert (tmp_path / "first.zip.sha256").read_text(encoding="utf-8").strip() == (
        f"{digest} *first.zip"
    )


def test_safe_relative_path_rejects_windows_and_traversal_paths() -> None:
    builder = _load_builder()

    for path in ("../escape", "C:/drive", "folder/CON.txt", "name:stream", "/rooted"):
        with pytest.raises(builder.PortableBuildError):
            builder.safe_relative_path(path)
