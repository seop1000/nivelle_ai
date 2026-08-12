from __future__ import annotations

import base64
import importlib.util
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LAUNCHER_PATH = PROJECT_ROOT / "scripts" / "nivelle_executable_launcher.py"


def _load_launcher() -> object:
    spec = importlib.util.spec_from_file_location("nivelle_executable_launcher", LAUNCHER_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_executable_names_select_expected_roles() -> None:
    launcher = _load_launcher()

    assert launcher.executable_role(Path("Nivelle-Core.exe")) == "server"  # type: ignore[attr-defined]
    assert launcher.executable_role(Path("nivelle-link.EXE")) == "client"  # type: ignore[attr-defined]
    assert launcher.executable_role(Path("Nivelle-Local.exe")) == "all"  # type: ignore[attr-defined]
    assert launcher.executable_role(Path("Nivelle-Updater.exe")) == "updater"  # type: ignore[attr-defined]


def test_legacy_031_executable_names_remain_a_bridge() -> None:
    launcher = _load_launcher()

    assert launcher.executable_role(Path("Nozomi-Server.exe")) == "server"  # type: ignore[attr-defined]
    assert launcher.executable_role(Path("Nozomi-Client.exe")) == "client"  # type: ignore[attr-defined]
    assert launcher.executable_role(Path("Nozomi-Local.exe")) == "all"  # type: ignore[attr-defined]
    assert launcher.executable_role(Path("Nozomi-Updater.exe")) == "updater"  # type: ignore[attr-defined]


def test_unknown_name_requires_explicit_source_role() -> None:
    launcher = _load_launcher()

    assert launcher.executable_role(Path("launcher.py"), "client") == "client"  # type: ignore[attr-defined]
    with pytest.raises(launcher.LauncherError):  # type: ignore[attr-defined]
        launcher.executable_role(Path("launcher.exe"))  # type: ignore[attr-defined]


def test_external_install_root_and_command_are_preserved(tmp_path: Path) -> None:
    launcher = _load_launcher()
    root = tmp_path / "Nivelle 설치 폴더"
    for relative in launcher.REQUIRED_INSTALL_PATHS:  # type: ignore[attr-defined]
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("test", encoding="utf-8")
    (root / "nivelle.py").write_text("test", encoding="utf-8")

    resolved = launcher.validate_install_root(root)  # type: ignore[attr-defined]
    powershell = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
    command = launcher.launcher_command(resolved, "server", powershell)  # type: ignore[attr-defined]

    assert command[0] == str(powershell)
    assert command[-4:] == ["-ProjectRoot", str(resolved), "-Mode", "server"]
    assert command[command.index("-File") + 1] == str(resolved / "scripts" / "run_locked.ps1")


def test_external_launcher_propagates_network_options(tmp_path: Path) -> None:
    launcher = _load_launcher()
    root = tmp_path / "Nivelle"
    powershell = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")

    command = launcher.launcher_command(  # type: ignore[attr-defined]
        root,
        "server",
        powershell,
        gateway_bind="0.0.0.0",
        gateway_advertised_host="192.168.10.20",
        network_diagnostics=True,
    )

    assert command[-5:] == [
        "-GatewayBind",
        "0.0.0.0",
        "-GatewayAdvertisedHost",
        "192.168.10.20",
        "-NetworkDiagnostics",
    ]


def test_locked_powershell_and_local_cmd_forward_network_arguments() -> None:
    run_locked = (PROJECT_ROOT / "scripts" / "run_locked.ps1").read_text(encoding="utf-8")
    local_cmd = (PROJECT_ROOT / "Nivelle-Local.cmd").read_text(encoding="utf-8")

    assert "[string]$GatewayBind" in run_locked
    assert "[string]$GatewayAdvertisedHost" in run_locked
    assert "[switch]$NetworkDiagnostics" in run_locked
    assert "@('--gateway-bind', $GatewayBind)" in run_locked
    assert "@('--gateway-advertised-host', $GatewayAdvertisedHost)" in run_locked
    assert "$launcherArguments += '--network-diagnostics'" in run_locked
    assert "-Mode all %*" in local_cmd


def test_executable_builder_handles_missing_pyinstaller_without_a_probe_traceback() -> None:
    script = (PROJECT_ROOT / "scripts" / "build_executables.ps1").read_text(
        encoding="utf-8"
    )

    assert "m.distributions()" in script
    assert "m.version('pyinstaller')" not in script


def test_missing_external_file_is_rejected(tmp_path: Path) -> None:
    launcher = _load_launcher()
    root = tmp_path / "incomplete"
    root.mkdir()

    with pytest.raises(launcher.LauncherError, match="누락된 파일"):  # type: ignore[attr-defined]
        launcher.validate_install_root(root)  # type: ignore[attr-defined]


def test_updater_waits_for_launcher_then_uses_external_script(tmp_path: Path) -> None:
    launcher = _load_launcher()
    root = tmp_path / "Nivelle's files"
    powershell = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")

    command = launcher.updater_command(root, powershell)  # type: ignore[attr-defined]
    encoded = command[command.index("-EncodedCommand") + 1]
    wrapper = base64.b64decode(encoded).decode("utf-16-le")

    assert "WaitForExit(30000)" in wrapper
    assert "scripts\\update_from_github.ps1" in wrapper
    assert "Nivelle''s files" in wrapper
    assert "-TargetRoot" in wrapper
    assert "powershell.exe' -NoLogo" in wrapper


def test_frozen_executable_parent_is_the_install_and_update_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launcher = _load_launcher()
    installed_executable = tmp_path / "server install" / "Nivelle-Updater.exe"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(installed_executable))

    root = launcher.default_install_root()  # type: ignore[attr-defined]
    powershell = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
    command = launcher.updater_command(root, powershell)  # type: ignore[attr-defined]
    encoded = command[command.index("-EncodedCommand") + 1]
    wrapper = base64.b64decode(encoded).decode("utf-16-le")
    escaped_root = str(root).replace("'", "''")

    assert root == installed_executable.parent.resolve()
    assert f"-TargetRoot '{escaped_root}'" in wrapper
