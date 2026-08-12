"""Thin Windows executable entry point for a patchable Nivelle installation.

Only this standard-library launcher is frozen into the EXE. Application source,
configuration, Python, models, and updater state remain external so a file-level
update or rollback remains authoritative.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

# Nozomi names are deliberately retained only as a 0.3.1 executable bridge.
ROLE_BY_EXECUTABLE = {
    "nivelle-core": "server",
    "nivelle-link": "client",
    "nivelle-local": "all",
    "nivelle-updater": "updater",
    "nozomi-server": "server",
    "nozomi-client": "client",
    "nozomi-local": "all",
    "nozomi-updater": "updater",
}
REQUIRED_INSTALL_PATHS = (
    "VERSION",
    "pyproject.toml",
    "scripts/bootstrap_python.ps1",
    "scripts/run_locked.ps1",
    "scripts/update_from_github.ps1",
)
LAUNCHER_SOURCE_OPTIONS = ("nivelle.py", "nozomi.py")


class LauncherError(RuntimeError):
    """Raised when the external Nivelle installation cannot be launched safely."""


def executable_role(executable: Path, requested_role: str | None = None) -> str:
    """Resolve a launch role from a current or explicitly supported legacy name."""

    role = ROLE_BY_EXECUTABLE.get(executable.stem.casefold())
    if role is not None:
        return role
    if requested_role is not None:
        return requested_role
    raise LauncherError(
        "실행 파일 이름에서 역할을 확인할 수 없습니다. "
        "Nivelle-Core.exe, Nivelle-Link.exe, Nivelle-Local.exe 또는 "
        "Nivelle-Updater.exe를 사용하세요."
    )


def default_install_root() -> Path:
    """Return the external install root in frozen and source execution modes."""

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def validate_install_root(candidate: Path) -> Path:
    """Resolve an install root and require all external launcher-owned files."""

    try:
        root = candidate.expanduser().resolve(strict=True)
    except OSError as exc:
        raise LauncherError(f"Nivelle 설치 폴더를 찾을 수 없습니다: {candidate}") from exc
    if not root.is_dir():
        raise LauncherError(f"Nivelle 설치 경로가 폴더가 아닙니다: {root}")

    missing = [relative for relative in REQUIRED_INSTALL_PATHS if not (root / relative).is_file()]
    if not any((root / relative).is_file() for relative in LAUNCHER_SOURCE_OPTIONS):
        missing.append("nivelle.py (or legacy nozomi.py)")
    if missing:
        raise LauncherError(
            "EXE와 외부 Nivelle 파일을 같은 설치 폴더에 두어야 합니다. "
            f"누락된 파일: {', '.join(missing)}"
        )
    return root


def system_powershell() -> Path:
    """Locate Windows PowerShell without trusting the process PATH."""

    system_root = os.environ.get("SystemRoot")
    if not system_root:
        raise LauncherError("Windows SystemRoot 환경 변수를 확인할 수 없습니다.")
    candidate = (
        Path(system_root)
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise LauncherError(f"Windows PowerShell 5.1을 찾을 수 없습니다: {candidate}") from exc
    if not resolved.is_file():
        raise LauncherError(f"Windows PowerShell 실행 파일이 아닙니다: {resolved}")
    return resolved


def launcher_command(
    root: Path,
    role: str,
    powershell: Path,
    *,
    python_path: Path | None = None,
    gateway_endpoint: str | None = None,
    provider_endpoint: str | None = None,
    gateway_bind: str | None = None,
    gateway_advertised_host: str | None = None,
    network_diagnostics: bool = False,
) -> list[str]:
    """Build an argument-safe command for the external launch pipeline."""

    command = [
        str(powershell),
        "-NoLogo",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(root / "scripts" / "run_locked.ps1"),
        "-ProjectRoot",
        str(root),
        "-Mode",
        role,
    ]
    if python_path is not None:
        command.extend(["-PythonPath", str(python_path)])
    if gateway_endpoint:
        command.extend(["-GatewayEndpoint", gateway_endpoint])
    if provider_endpoint:
        command.extend(["-ProviderEndpoint", provider_endpoint])
    if gateway_bind:
        command.extend(["-GatewayBind", gateway_bind])
    if gateway_advertised_host:
        command.extend(["-GatewayAdvertisedHost", gateway_advertised_host])
    if network_diagnostics:
        command.append("-NetworkDiagnostics")
    return command


def updater_command(root: Path, powershell: Path) -> list[str]:
    """Build a detached updater command that waits for the frozen EXE to exit."""

    process_ids = sorted({os.getpid(), os.getppid()})
    ids = ",".join(str(process_id) for process_id in process_ids)
    powershell_path = str(powershell).replace("'", "''")
    script_path = str(root / "scripts" / "update_from_github.ps1").replace("'", "''")
    target_root = str(root).replace("'", "''")
    wrapper = f"""
$ErrorActionPreference = 'Stop'
foreach ($processId in @({ids})) {{
    $launcher = Get-Process -Id $processId -ErrorAction SilentlyContinue
    if ($null -ne $launcher -and -not $launcher.WaitForExit(30000)) {{
        throw "Nivelle-Updater.exe did not exit before the update timeout."
    }}
}}
& '{powershell_path}' -NoLogo -NoProfile -ExecutionPolicy Bypass `
    -File '{script_path}' -TargetRoot '{target_root}'
$updateExitCode = $LASTEXITCODE
if ($updateExitCode -eq 0) {{
    Write-Host 'Nivelle 온라인 업데이트 작업이 끝났습니다.' -ForegroundColor Green
}} else {{
    Write-Host "Nivelle 온라인 업데이트가 실패했습니다. 종료 코드: $updateExitCode" -ForegroundColor Red
}}
Read-Host '창을 닫으려면 Enter 키를 누르세요'
exit $updateExitCode
"""
    encoded = base64.b64encode(wrapper.encode("utf-16-le")).decode("ascii")
    return [
        str(powershell),
        "-NoLogo",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-EncodedCommand",
        encoded,
    ]


def run_external_launcher(command: Sequence[str], root: Path) -> int:
    """Run and wait for the PowerShell/Python launch chain."""

    try:
        process = subprocess.Popen(list(command), cwd=root)
    except OSError as exc:
        raise LauncherError(f"Nivelle 실행기를 시작하지 못했습니다: {exc}") from exc

    try:
        return process.wait()
    except KeyboardInterrupt:
        try:
            return process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                return process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                return 130


def start_detached_updater(command: Sequence[str], root: Path) -> None:
    """Start the online updater in its own console and return immediately."""

    creation_flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)
    creation_flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    try:
        subprocess.Popen(
            list(command),
            cwd=root,
            creationflags=creation_flags,
            close_fds=True,
        )
    except OSError as exc:
        raise LauncherError(f"Nivelle 온라인 업데이트를 시작하지 못했습니다: {exc}") from exc


def _write_console(message: str) -> None:
    stream = getattr(sys, "stderr", None)
    if stream is not None:
        stream.write(message + "\n")
        stream.flush()


def _show_error(message: str) -> None:
    _write_console(message)
    if os.name == "nt" and getattr(sys, "stderr", None) is None:
        ctypes.windll.user32.MessageBoxW(None, message, "Nivelle 실행 오류", 0x10)


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Nivelle 외부 설치 파일 실행기")
    parser.add_argument(
        "--install-root",
        type=Path,
        help="EXE와 분리된 Nivelle 설치 루트(빌드 검증 및 개발용)",
    )
    parser.add_argument(
        "--role",
        choices=("server", "client", "all", "updater"),
        help="Python 소스 실행 시 역할 지정(배포 EXE는 파일 이름으로 결정)",
    )
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="외부 파일과 PowerShell만 검사하고 앱은 시작하지 않음",
    )
    parser.add_argument("--python", type=Path, help="Compatible Python executable")
    parser.add_argument("--gateway-endpoint", help="Nivelle Core Gateway endpoint")
    parser.add_argument("--provider-endpoint", help="Core-owned model provider endpoint")
    parser.add_argument("--gateway-bind", help="Core Gateway bind address")
    parser.add_argument(
        "--gateway-advertised-host",
        help="Core Gateway address advertised to Nivelle Link",
    )
    parser.add_argument(
        "--network-diagnostics",
        action="store_true",
        help="Print Core network diagnostics without starting models or the Gateway",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    try:
        executable = Path(sys.executable if getattr(sys, "frozen", False) else __file__)
        role = executable_role(executable, args.role)
        root = validate_install_root(args.install_root or default_install_root())
        powershell = system_powershell()
        resolved_executable = str(executable.resolve())
        os.environ["NIVELLE_EXECUTABLE_PATH"] = resolved_executable
        # Runtime 0.3.1 reads this old variable during the one-release bridge.
        os.environ["NOZOMI_EXECUTABLE_PATH"] = resolved_executable
        if args.smoke_test:
            if getattr(sys, "stdout", None) is not None:
                print(f"Nivelle {role} executable smoke test passed: {root}")
            return 0
        if role == "updater":
            start_detached_updater(updater_command(root, powershell), root)
            return 0
        return run_external_launcher(
            launcher_command(
                root,
                role,
                powershell,
                python_path=args.python,
                gateway_endpoint=args.gateway_endpoint,
                provider_endpoint=args.provider_endpoint,
                gateway_bind=args.gateway_bind,
                gateway_advertised_host=args.gateway_advertised_host,
                network_diagnostics=args.network_diagnostics,
            ),
            root,
        )
    except LauncherError as exc:
        _show_error(str(exc))
        return 2
    except Exception as exc:  # defensive boundary for a double-clicked EXE
        _show_error(f"예상하지 못한 Nivelle 실행 오류: {exc}")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
