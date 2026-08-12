from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
APPLY_SCRIPT = ROOT / "scripts" / "apply_update.ps1"
ROLLBACK_SCRIPT = ROOT / "scripts" / "rollback_update.ps1"
UPDATE_CMD = ROOT / "Nivelle-Update.cmd"
ROLLBACK_CMD = ROOT / "Nivelle-Rollback.cmd"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _installation(root: Path) -> None:
    (root / "apps" / "server").mkdir(parents=True)
    (root / "apps" / "client").mkdir(parents=True)
    (root / "packages").mkdir()
    (root / "pyproject.toml").write_text(
        '[project]\nversion = "0.1.0"\n', encoding="utf-8"
    )
    (root / "nivelle.py").write_text("old launcher\n", encoding="utf-8")
    (root / "VERSION").write_text("0.1.0\n", encoding="utf-8")
    (root / "changed.py").write_text("old\n", encoding="utf-8")
    (root / "obsolete.py").write_text("remove me\n", encoding="utf-8")
    (root / "user-note.txt").write_text("keep me\n", encoding="utf-8")
    (root / "runtime").mkdir()
    (root / "runtime" / "model.gguf").write_text("model", encoding="utf-8")
    (root / ".venv").mkdir()
    (root / ".venv" / "pyvenv.cfg").write_text("keep", encoding="utf-8")


def _package(root: Path, install: Path) -> Path:
    payload = root / "payload"
    payload.mkdir(parents=True)
    (payload / "VERSION").write_text("0.2.0\n", encoding="utf-8")
    (payload / "changed.py").write_text("new\n", encoding="utf-8")
    (payload / "added.py").write_text("added\n", encoding="utf-8")

    files: list[dict[str, Any]] = []
    for relative in ("VERSION", "changed.py", "added.py"):
        source = payload / relative
        installed = install / relative
        files.append(
            {
                "path": relative,
                "sha256": _sha256(source),
                "size": source.stat().st_size,
                "base_sha256": _sha256(installed) if installed.exists() else None,
            }
        )
    manifest = {
        "format_version": 1,
        "product": "Nivelle",
        "from_version": "0.1.0",
        "to_version": "0.2.0",
        "files": files,
        "deletions": [
            {"path": "obsolete.py", "base_sha256": _sha256(install / "obsolete.py")}
        ],
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return root


def _zip_package(package: Path, destination: Path) -> Path:
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for source in sorted(package.rglob("*")):
            if source.is_file():
                archive.write(source, source.relative_to(package).as_posix())
    return destination


def _powershell(script: Path, state: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    def quote(value: object) -> str:
        return "'" + str(value).replace("'", "''") + "'"

    rendered_arguments = [
        argument if argument.startswith("-") else quote(argument) for argument in arguments
    ]
    command = (
        f"$env:LOCALAPPDATA = {quote(state)}; & {quote(script)} "
        + " ".join(rendered_arguments)
    )
    return subprocess.run(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )


@pytest.mark.skipif(not hasattr(subprocess, "CREATE_NEW_CONSOLE"), reason="Windows updater")
def test_apply_and_latest_rollback_preserve_runtime_and_user_files(tmp_path: Path) -> None:
    install = tmp_path / "install"
    package = tmp_path / "package"
    state = tmp_path / "localappdata"
    _installation(install)
    _package(package, install)
    package_zip = _zip_package(package, tmp_path / "Nivelle-Update-0.2.0.zip")

    applied = _powershell(
        APPLY_SCRIPT,
        state,
        "-PackagePath",
        str(package_zip),
        "-TargetRoot",
        str(install),
    )
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert (install / "VERSION").read_text(encoding="utf-8").strip() == "0.2.0"
    assert (install / "changed.py").read_text(encoding="utf-8") == "new\n"
    assert (install / "added.py").is_file()
    assert not (install / "obsolete.py").exists()
    assert (install / "runtime" / "model.gguf").read_text(encoding="utf-8") == "model"
    assert (install / ".venv" / "pyvenv.cfg").read_text(encoding="utf-8") == "keep"
    assert (install / "user-note.txt").read_text(encoding="utf-8") == "keep me\n"

    backups = list((state / "Nivelle" / "Updater" / "backups").iterdir())
    assert len(backups) == 1
    backed_up = backups[0] / "files"
    assert (backed_up / "changed.py").is_file()
    assert (backed_up / "obsolete.py").is_file()
    assert not (backed_up / "added.py").exists()

    rolled_back = _powershell(
        ROLLBACK_SCRIPT, state, "-Latest", "-TargetRoot", str(install)
    )
    assert rolled_back.returncode == 0, rolled_back.stdout + rolled_back.stderr
    assert (install / "VERSION").read_text(encoding="utf-8").strip() == "0.1.0"
    assert (install / "changed.py").read_text(encoding="utf-8") == "old\n"
    assert (install / "obsolete.py").read_text(encoding="utf-8") == "remove me\n"
    assert not (install / "added.py").exists()


@pytest.mark.skipif(not hasattr(subprocess, "CREATE_NEW_CONSOLE"), reason="Windows updater")
def test_apply_refuses_modified_base_file_without_mutation(tmp_path: Path) -> None:
    install = tmp_path / "install"
    package = tmp_path / "package"
    state = tmp_path / "localappdata"
    _installation(install)
    _package(package, install)
    (install / "changed.py").write_text("user edit\n", encoding="utf-8")

    result = _powershell(
        APPLY_SCRIPT,
        state,
        "-PackagePath",
        str(package),
        "-TargetRoot",
        str(install),
    )
    assert result.returncode == 1
    assert (install / "VERSION").read_text(encoding="utf-8").strip() == "0.1.0"
    assert (install / "changed.py").read_text(encoding="utf-8") == "user edit\n"
    assert (install / "obsolete.py").is_file()
    assert not (install / "added.py").exists()


@pytest.mark.skipif(not hasattr(subprocess, "CREATE_NEW_CONSOLE"), reason="Windows updater")
def test_apply_always_refuses_protected_runtime_path(tmp_path: Path) -> None:
    install = tmp_path / "install"
    package = tmp_path / "package"
    state = tmp_path / "localappdata"
    _installation(install)
    _package(package, install)
    protected = package / "payload" / "runtime" / "bad.txt"
    protected.parent.mkdir()
    protected.write_text("bad", encoding="utf-8")
    manifest_path = package / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"].append(
        {
            "path": "runtime/bad.txt",
            "sha256": _sha256(protected),
            "size": protected.stat().st_size,
            "base_sha256": None,
        }
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = _powershell(
        APPLY_SCRIPT,
        state,
        "-PackagePath",
        str(package),
        "-TargetRoot",
        str(install),
    )
    assert result.returncode == 1
    assert not (install / "runtime" / "bad.txt").exists()
    assert (install / "VERSION").read_text(encoding="utf-8").strip() == "0.1.0"


@pytest.mark.skipif(not hasattr(subprocess, "CREATE_NEW_CONSOLE"), reason="Windows updater")
def test_rollback_refuses_post_update_manual_edit(tmp_path: Path) -> None:
    install = tmp_path / "install"
    package = tmp_path / "package"
    state = tmp_path / "localappdata"
    _installation(install)
    _package(package, install)
    applied = _powershell(
        APPLY_SCRIPT,
        state,
        "-PackagePath",
        str(package),
        "-TargetRoot",
        str(install),
    )
    assert applied.returncode == 0, applied.stdout + applied.stderr
    (install / "changed.py").write_text("manual after update\n", encoding="utf-8")

    rolled_back = _powershell(
        ROLLBACK_SCRIPT, state, "-Latest", "-TargetRoot", str(install)
    )
    assert rolled_back.returncode == 1
    assert (install / "VERSION").read_text(encoding="utf-8").strip() == "0.2.0"
    assert (install / "changed.py").read_text(encoding="utf-8") == "manual after update\n"
    assert (install / "added.py").is_file()


@pytest.mark.skipif(not hasattr(subprocess, "CREATE_NEW_CONSOLE"), reason="Windows updater")
def test_latest_rollback_is_scoped_to_target_installation(tmp_path: Path) -> None:
    state = tmp_path / "localappdata"
    first_install = tmp_path / "first-install"
    second_install = tmp_path / "second-install"
    first_package = tmp_path / "first-package"
    second_package = tmp_path / "second-package"
    _installation(first_install)
    _installation(second_install)
    _package(first_package, first_install)
    _package(second_package, second_install)

    for install, package in (
        (first_install, first_package),
        (second_install, second_package),
    ):
        applied = _powershell(
            APPLY_SCRIPT,
            state,
            "-PackagePath",
            str(package),
            "-TargetRoot",
            str(install),
        )
        assert applied.returncode == 0, applied.stdout + applied.stderr

    rolled_back = _powershell(
        ROLLBACK_SCRIPT, state, "-Latest", "-TargetRoot", str(first_install)
    )
    assert rolled_back.returncode == 0, rolled_back.stdout + rolled_back.stderr
    assert (first_install / "VERSION").read_text(encoding="utf-8").strip() == "0.1.0"
    assert (second_install / "VERSION").read_text(encoding="utf-8").strip() == "0.2.0"


@pytest.mark.skipif(not hasattr(subprocess, "CREATE_NEW_CONSOLE"), reason="Windows updater")
def test_mid_apply_locked_delete_restores_only_mutated_files(tmp_path: Path) -> None:
    install = tmp_path / "install"
    package = tmp_path / "package"
    state = tmp_path / "localappdata"
    _installation(install)
    _package(package, install)
    locked_path = str(install / "obsolete.py").replace("'", "''")
    lock_command = (
        "$stream = [IO.File]::Open('"
        + locked_path
        + "', [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read); "
        + "[Console]::Out.WriteLine('READY'); [Console]::Out.Flush(); Start-Sleep -Seconds 30"
    )
    locker = subprocess.Popen(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-Command", lock_command],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        assert locker.stdout is not None
        assert locker.stdout.readline().strip() == "READY"
        result = _powershell(
            APPLY_SCRIPT,
            state,
            "-PackagePath",
            str(package),
            "-TargetRoot",
            str(install),
        )
    finally:
        locker.terminate()
        locker.wait(timeout=10)

    assert result.returncode == 1
    assert (install / "VERSION").read_text(encoding="utf-8").strip() == "0.1.0"
    assert (install / "changed.py").read_text(encoding="utf-8") == "old\n"
    assert (install / "obsolete.py").read_text(encoding="utf-8") == "remove me\n"
    assert not (install / "added.py").exists()
    backup = next((state / "Nivelle" / "Updater" / "backups").iterdir())
    metadata = json.loads((backup / "backup.json").read_text(encoding="utf-8"))
    assert metadata["status"] == "failed-restored"


@pytest.mark.skipif(not hasattr(subprocess, "CREATE_NEW_CONSOLE"), reason="Windows updater")
def test_reapplying_same_update_is_rejected_without_a_second_backup(
    tmp_path: Path,
) -> None:
    install = tmp_path / "install"
    package = tmp_path / "package"
    state = tmp_path / "localappdata"
    _installation(install)
    _package(package, install)

    first = _powershell(
        APPLY_SCRIPT,
        state,
        "-PackagePath",
        str(package),
        "-TargetRoot",
        str(install),
    )
    assert first.returncode == 0, first.stdout + first.stderr
    backups_root = state / "Nivelle" / "Updater" / "backups"
    assert len(list(backups_root.iterdir())) == 1

    second = _powershell(
        APPLY_SCRIPT,
        state,
        "-PackagePath",
        str(package),
        "-TargetRoot",
        str(install),
    )
    assert second.returncode == 1
    assert (install / "VERSION").read_text(encoding="utf-8").strip() == "0.2.0"
    assert (install / "changed.py").read_text(encoding="utf-8") == "new\n"
    assert len(list(backups_root.iterdir())) == 1


@pytest.mark.skipif(not hasattr(subprocess, "CREATE_NEW_CONSOLE"), reason="Windows updater")
@pytest.mark.parametrize("lock_directory", [".nivelle", ".nozomi"])
def test_apply_refuses_current_or_legacy_shared_run_lock_without_mutation(
    tmp_path: Path, lock_directory: str
) -> None:
    install = tmp_path / "install"
    package = tmp_path / "package"
    state = tmp_path / "localappdata"
    _installation(install)
    _package(package, install)
    lock_path = install / lock_directory / "run.lock"
    lock_path.parent.mkdir()
    escaped_lock = str(lock_path).replace("'", "''")
    lock_command = (
        "$stream = [IO.File]::Open('"
        + escaped_lock
        + "', [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, "
        + "[IO.FileShare]::ReadWrite); [Console]::Out.WriteLine('READY'); "
        + "[Console]::Out.Flush(); Start-Sleep -Seconds 30"
    )
    locker = subprocess.Popen(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-Command", lock_command],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        assert locker.stdout is not None
        assert locker.stdout.readline().strip() == "READY"
        result = _powershell(
            APPLY_SCRIPT,
            state,
            "-PackagePath",
            str(package),
            "-TargetRoot",
            str(install),
        )
    finally:
        locker.terminate()
        locker.wait(timeout=10)

    assert result.returncode == 1
    assert (install / "VERSION").read_text(encoding="utf-8").strip() == "0.1.0"
    assert (install / "changed.py").read_text(encoding="utf-8") == "old\n"
    assert (install / "obsolete.py").is_file()
    assert not (install / "added.py").exists()
    backups_root = state / "Nivelle" / "Updater" / "backups"
    assert not backups_root.exists() or not list(backups_root.iterdir())


@pytest.mark.skipif(not hasattr(subprocess, "CREATE_NEW_CONSOLE"), reason="Windows updater")
def test_self_contained_nivelle_update_cmd_bootstraps_installation(
    tmp_path: Path,
) -> None:
    install = tmp_path / "기존 서버 설치본"
    package = tmp_path / "압축 해제한 업데이트"
    state = tmp_path / "local app data"
    _installation(install)
    (install / "Nivelle-Rollback.cmd").write_text(
        "@echo legacy rollback launcher\n", encoding="utf-8"
    )
    _package(package, install)
    shutil.copy2(APPLY_SCRIPT, package / "apply_update.ps1")
    shutil.copy2(UPDATE_CMD, package / "Nivelle-Update.cmd")
    extra_payload = {
        "Nivelle-Rollback.cmd": ROLLBACK_CMD,
        "scripts/rollback_update.ps1": ROLLBACK_SCRIPT,
    }
    manifest_path = package / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for relative, source in extra_payload.items():
        destination = package / "payload" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        manifest["files"].append(
            {
                "path": relative,
                "sha256": _sha256(destination),
                "size": destination.stat().st_size,
                "base_sha256": _sha256(install / relative)
                if (install / relative).is_file()
                else None,
            }
        )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    cmd = shutil.which("cmd.exe")
    assert cmd is not None
    environment = os.environ.copy()
    environment["LOCALAPPDATA"] = str(state)
    environment["NIVELLE_NO_PAUSE"] = "1"

    command = f'"{package / "Nivelle-Update.cmd"}" -TargetRoot "{install}"'
    result = subprocess.run(
        command,
        shell=True,
        executable=cmd,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        env=environment,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (install / "VERSION").read_text(encoding="utf-8").strip() == "0.2.0"
    assert (install / "changed.py").read_text(encoding="utf-8") == "new\n"
    assert (install / "added.py").is_file()

    rollback_command = f'"{install / "Nivelle-Rollback.cmd"}"'
    rolled_back = subprocess.run(
        rollback_command,
        shell=True,
        executable=cmd,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        env=environment,
    )
    assert rolled_back.returncode == 0, rolled_back.stdout + rolled_back.stderr
    assert (install / "VERSION").read_text(encoding="utf-8").strip() == "0.1.0"
    assert (install / "changed.py").read_text(encoding="utf-8") == "old\n"
    assert not (install / "added.py").exists()
    assert (install / "Nivelle-Rollback.cmd").is_file()

    reapplied = subprocess.run(
        command,
        shell=True,
        executable=cmd,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        env=environment,
    )
    assert reapplied.returncode == 0, reapplied.stdout + reapplied.stderr
    assert (install / "VERSION").read_text(encoding="utf-8").strip() == "0.2.0"
