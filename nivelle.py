"""One-command launcher for Nivelle Core and Nivelle Link."""

from __future__ import annotations

import argparse
import ipaddress
import os
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import SplitResult, urlsplit
from uuid import uuid4

import yaml
from nivelle_protocol.settings import InferenceSettings, ModelEntry, ModelsSettings, ServerSettings
from pydantic import BaseModel, ValidationError

from nivelle_runtime import RuntimePaths, ensure_runtime

ROOT = Path(__file__).resolve().parent
VENV_PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"

@dataclass(frozen=True)
class LauncherSettings:
    """Validated settings used to decide which local processes to launch."""

    server: ServerSettings
    models: ModelsSettings
    inference: InferenceSettings


def project_python() -> Path:
    """Return the project's isolated Python interpreter."""
    if not VENV_PYTHON.is_file():
        raise SystemExit(
            "Nivelle 가상환경이 없습니다. PowerShell에서 먼저 "
            "'.\\scripts\\setup_dev.ps1'을 실행하세요."
        )
    return VENV_PYTHON


def endpoint_is_ready(url: str) -> bool:
    """Check whether an HTTP health endpoint is responding successfully."""
    try:
        with urllib.request.urlopen(url, timeout=1) as response:
            return int(response.status) == 200
    except (OSError, urllib.error.URLError):
        return False


def server_is_ready(health_url: str) -> bool:
    """Check whether a Nivelle-compatible health endpoint is responding."""
    return endpoint_is_ready(health_url)


def wait_for_server(
    process: subprocess.Popen[bytes], health_url: str, timeout: float = 30
) -> None:
    """Wait for Gateway readiness and fail with an actionable message."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server_is_ready(health_url):
            return
        if process.poll() is not None:
            raise SystemExit(
                "Nivelle Core가 시작 중 종료되었습니다. 별도 Core 콘솔의 오류를 확인하세요."
            )
        time.sleep(0.25)
    raise SystemExit("30초 안에 Nivelle Core가 준비되지 않았습니다.")


def wait_for_llama(
    process: subprocess.Popen[bytes],
    health_url: str,
    model_name: str,
    timeout: float = 600,
) -> None:
    """Wait while llama-server loads the GGUF model into memory."""
    print(f"{model_name} 모델을 메모리에 불러오는 중입니다...")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if endpoint_is_ready(health_url):
            print(f"{model_name} 준비 완료.")
            return
        if process.poll() is not None:
            raise SystemExit(
                "llama-server가 모델 로딩 중 종료되었습니다. "
                "별도 llama.cpp 콘솔의 오류를 확인하세요."
            )
        time.sleep(1)
    raise SystemExit("10분 안에 모델이 준비되지 않았습니다.")


def configure_real_model(runtime: RuntimePaths) -> None:
    """Create or relocate the automatic Qwen runtime configuration.

    Server settings intentionally live outside a portable installation.  Older
    releases stored absolute paths there, so moving the portable directory to
    another PC left otherwise valid runtime files pointing at the old machine.
    Only paths with Nivelle's generated ``runtime`` layout are migrated; custom
    administrator paths are preserved.
    """
    from nivelle_core.paths import server_data_dir

    config_path = server_data_dir() / "config" / "models.yaml"

    def portable_reference(path: Path) -> Path:
        try:
            return path.resolve().relative_to(ROOT.resolve())
        except (OSError, ValueError):
            return path

    def automatic_runtime_reference(path: Path, *, kind: str) -> bool:
        parts = [part.casefold() for part in path.parts]
        runtime_positions = [index for index, part in enumerate(parts) if part == "runtime"]
        if not runtime_positions:
            return False
        tail = parts[runtime_positions[-1] :]
        if kind == "server":
            return (
                len(tail) >= 3
                and tail[1] == "llama.cpp"
                and tail[-1] == "llama-server.exe"
            )
        return len(tail) >= 3 and tail[1] == "models" and tail[-1].endswith(".gguf")

    if config_path.is_file():
        try:
            existing = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except (OSError, UnicodeError, yaml.YAMLError):
            # load_launcher_settings() reports the complete validation error.
            return
        if not isinstance(existing, dict):
            return

        changed = False
        configured_server = existing.get("llama_server_path")
        if configured_server:
            configured_path = Path(str(configured_server))
            if automatic_runtime_reference(configured_path, kind="server"):
                replacement = portable_reference(runtime.server_path)
                if configured_path != replacement:
                    existing["llama_server_path"] = str(replacement)
                    changed = True

        configured_models = existing.get("models")
        automatic_model_paths = {
            "ministral-3-14b-instruct-2512-q4-k-m": runtime.model_path,
            "qwen3.5-9b-q4-k-m": runtime.fallback_model_path or runtime.model_path,
        }
        if isinstance(configured_models, list):
            configured_model_ids = {
                str(model.get("id"))
                for model in configured_models
                if isinstance(model, dict)
            }
            for model in configured_models:
                if not isinstance(model, dict):
                    continue
                configured_model = model.get("path")
                if not configured_model:
                    continue
                configured_path = Path(str(configured_model))
                model_id = str(model.get("id"))
                if (
                    model_id == "qwen3.5-27b-q4-k-m"
                    and model.get("role") == "primary"
                    and "ministral-3-14b-instruct-2512-q4-k-m"
                    not in configured_model_ids
                    and automatic_runtime_reference(configured_path, kind="model")
                    and configured_path.name.casefold()
                    == "Qwen_Qwen3.5-27B-Q4_K_M.gguf".casefold()
                ):
                    model.update(
                        id="ministral-3-14b-instruct-2512-q4-k-m",
                        name="Ministral-3-14B-Instruct-2512 Q4_K_M",
                        path=str(portable_reference(runtime.model_path)),
                    )
                    changed = True
                    continue
                runtime_model_path = automatic_model_paths.get(model_id)
                if runtime_model_path is None:
                    continue
                if automatic_runtime_reference(configured_path, kind="model"):
                    replacement = portable_reference(runtime_model_path)
                    if configured_path != replacement:
                        model["path"] = str(replacement)
                        changed = True

        if not changed:
            return
        try:
            validated = ModelsSettings.model_validate(existing)
        except ValidationError:
            # Never overwrite an administrator file that cannot be validated.
            return
        payload = yaml.safe_dump(
            validated.model_dump(mode="json"), allow_unicode=True, sort_keys=False
        )
        temporary = config_path.with_name(f".models.{uuid4().hex}.yaml.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, config_path)
        finally:
            temporary.unlink(missing_ok=True)
        print("포터블 위치 변경을 감지하여 모델/llama.cpp 경로를 현재 폴더로 복구했습니다.")
        return

    config_path.parent.mkdir(parents=True, exist_ok=True)
    initial = ModelsSettings(
        mode="external",
        llama_server_path=portable_reference(runtime.server_path),
        provider_endpoint="http://127.0.0.1:8080",
        fallback_enabled=False,
        models=[
            ModelEntry(
                id="ministral-3-14b-instruct-2512-q4-k-m",
                name="Ministral-3-14B-Instruct-2512 Q4_K_M",
                path=portable_reference(runtime.model_path),
                role="primary",
                enabled=True,
            ),
            *(
                [
                    ModelEntry(
                        id="qwen3.5-9b-q4-k-m",
                        name="Qwen3.5-9B Q4_K_M",
                        path=portable_reference(runtime.fallback_model_path),
                        role="fallback",
                        enabled=True,
                    )
                ]
                if runtime.fallback_model_path is not None
                else []
            ),
        ],
    )
    payload = yaml.safe_dump(
        initial.model_dump(mode="json"), allow_unicode=True, sort_keys=False
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(config_path, flags)
    except FileExistsError:
        # A second launcher may have completed the bootstrap after our first check.
        return
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        config_path.unlink(missing_ok=True)
        raise


def _load_settings_file[SettingsModel: BaseModel](
    path: Path, model: type[SettingsModel]
) -> SettingsModel:
    if not path.is_file():
        return model()
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise SystemExit(f"설정 파일을 읽을 수 없습니다: {path}\n{exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"설정 파일의 최상위 값은 객체여야 합니다: {path}")
    try:
        return model.model_validate(value)
    except ValidationError as exc:
        raise SystemExit(f"설정 파일이 올바르지 않습니다: {path}\n{exc}") from exc


def load_launcher_settings() -> LauncherSettings:
    """Load the server-owned YAML settings used by the launcher."""
    from nivelle_core.paths import server_data_dir

    directory = server_data_dir() / "config"
    return LauncherSettings(
        server=_load_settings_file(directory / "server.yaml", ServerSettings),
        models=_load_settings_file(directory / "models.yaml", ModelsSettings),
        inference=_load_settings_file(directory / "inference.yaml", InferenceSettings),
    )


def _external_endpoint(models: ModelsSettings) -> SplitResult:
    endpoint = urlsplit(models.provider_endpoint)
    try:
        port = endpoint.port
    except ValueError as exc:
        raise SystemExit(
            f"llama-server 주소의 포트가 올바르지 않습니다: {models.provider_endpoint}"
        ) from exc
    if endpoint.scheme not in {"http", "https"} or not endpoint.hostname:
        raise SystemExit(f"llama-server 주소가 올바르지 않습니다: {models.provider_endpoint}")
    if endpoint.query or endpoint.fragment:
        raise SystemExit(
            "llama-server 주소에는 query 또는 fragment를 사용할 수 없습니다: "
            f"{models.provider_endpoint}"
        )
    # Accessing the property above eagerly validates an explicitly supplied port.
    _ = port
    return endpoint


def _is_loopback(host: str) -> bool:
    if host.rstrip(".").lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def should_start_local_llama(models: ModelsSettings) -> bool:
    """Return whether this launcher owns the configured loopback llama endpoint."""
    if models.mode == "mock":
        return False
    endpoint = _external_endpoint(models)
    # llama-server launched here has no TLS configuration. A loopback HTTPS URL
    # therefore belongs to a separately managed proxy/process.
    return endpoint.scheme == "http" and _is_loopback(endpoint.hostname or "")


def llama_health_url(models: ModelsSettings) -> str:
    endpoint = _external_endpoint(models)
    base_path = endpoint.path.rstrip("/")
    return endpoint._replace(path=f"{base_path}/health", query="", fragment="").geturl()


def gateway_health_url(server: ServerSettings, bind_host: str | None = None) -> str:
    """Return a local readiness URL for the effective Gateway bind address."""
    host = (bind_host or server.host).strip()
    if host in {"0.0.0.0", "*"}:
        host = "127.0.0.1"
    elif host in {"::", "[::]"}:
        host = "::1"
    host = host.strip("[]")
    url_host = f"[{host}]" if ":" in host else host
    return f"http://{url_host}:{server.port}/health"


def _selected_model(models: ModelsSettings) -> tuple[str, Path | None]:
    enabled = [model for model in models.models if model.enabled]
    selected = next((model for model in enabled if model.role == "primary"), None)
    if selected is None:
        selected = enabled[0] if enabled else None
    return (
        selected.name if selected is not None else "Ministral-3-14B-Instruct-2512",
        selected.path if selected is not None else None,
    )


def llama_command(
    runtime: RuntimePaths, models: ModelsSettings, inference: InferenceSettings
) -> list[str]:
    """Build a llama-server command entirely from validated saved settings."""
    endpoint = _external_endpoint(models)
    executable = models.llama_server_path or runtime.server_path
    model_name, configured_model_path = _selected_model(models)
    model_path = configured_model_path or runtime.model_path
    if not executable.is_absolute():
        executable = (ROOT / executable).resolve()
    if not model_path.is_absolute():
        model_path = (ROOT / model_path).resolve()
    port = endpoint.port or (443 if endpoint.scheme == "https" else 80)
    return [
        str(executable),
        "--model",
        str(model_path),
        "--alias",
        model_name,
        "--host",
        endpoint.hostname or "127.0.0.1",
        "--port",
        str(port),
        "--cors-origins",
        "localhost",
        "--no-cors-credentials",
        "--ctx-size",
        str(inference.context_size),
        "--parallel",
        str(inference.concurrent_requests),
        "--gpu-layers",
        str(inference.gpu_layers),
        "--threads",
        str(inference.threads),
        "--batch-size",
        str(inference.batch_size),
        "--ubatch-size",
        str(inference.micro_batch_size),
        "--flash-attn",
        "auto",
        "--jinja",
        "--reasoning",
        "off",
        "--metrics",
        "--temp",
        str(inference.temperature),
        "--top-p",
        str(inference.top_p),
        "--top-k",
        str(inference.top_k),
        "--repeat-penalty",
        str(inference.repeat_penalty),
        "--predict",
        str(inference.max_output_tokens),
        "--seed",
        str(inference.seed),
    ]


def start_llama(
    runtime: RuntimePaths, models: ModelsSettings, inference: InferenceSettings
) -> subprocess.Popen[bytes]:
    """Start the configured GGUF through the pinned llama.cpp Vulkan server."""
    creation_flags = subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0
    command = llama_command(runtime, models, inference)
    executable = Path(command[0])
    model_path = Path(command[command.index("--model") + 1])
    if not executable.is_file():
        raise SystemExit(
            "llama-server 실행 파일을 찾을 수 없습니다.\n"
            f"확인한 경로: {executable}\n"
            "서버를 다시 실행하면 자동 설치 경로를 복구합니다. 사용자 지정 경로라면 "
            "서버 관리자 설정에서 올바른 llama-server.exe를 선택하세요."
        )
    if not model_path.is_file():
        raise SystemExit(
            "모델 파일을 찾을 수 없습니다.\n"
            f"확인한 경로: {model_path}\n"
            "서버를 다시 실행하면 기본 모델 경로를 복구합니다. 사용자 지정 모델이라면 "
            "서버 관리자 설정에서 올바른 GGUF 파일을 선택하세요."
        )
    try:
        return subprocess.Popen(
            command,
            cwd=executable.parent,
            creationflags=creation_flags,
        )
    except FileNotFoundError as exc:
        raise SystemExit(
            "llama-server를 시작하지 못했습니다. 포터블 폴더의 runtime\\llama.cpp "
            "내용이 완전한지 확인한 뒤 다시 실행하세요.\n"
            f"실행 파일: {executable}"
        ) from exc


def core_command(
    *,
    provider_endpoint: str | None = None,
    gateway_bind: str | None = None,
    gateway_advertised_host: str | None = None,
    network_diagnostics: bool = False,
    ui: bool = False,
) -> list[str]:
    """Build the Core command shared by server and diagnostics launch paths."""
    command = [str(project_python()), "-m", "nivelle_core.main"]
    if provider_endpoint:
        command.extend(["--provider-endpoint", provider_endpoint])
    if gateway_bind:
        command.extend(["--gateway-bind", gateway_bind])
    if gateway_advertised_host:
        command.extend(["--gateway-advertised-host", gateway_advertised_host])
    if network_diagnostics:
        command.append("--network-diagnostics")
    if ui:
        command.append("--ui")
    return command


def start_server(
    provider_endpoint: str | None = None,
    gateway_bind: str | None = None,
    gateway_advertised_host: str | None = None,
) -> subprocess.Popen[bytes]:
    """Start the Gateway in a separate console so its pairing code is visible."""
    creation_flags = subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0
    command = core_command(
        provider_endpoint=provider_endpoint,
        gateway_bind=gateway_bind,
        gateway_advertised_host=gateway_advertised_host,
        ui=True,
    )
    return subprocess.Popen(
        command,
        cwd=ROOT,
        creationflags=creation_flags,
    )


def run_client(
    gateway_endpoint: str | None = None,
    *,
    local_mode: bool = False,
) -> int:
    """Run the desktop UI in the foreground."""
    command = [str(project_python()), "-m", "nivelle_link.main"]
    if gateway_endpoint:
        command.extend(["--gateway-endpoint", gateway_endpoint])
    if local_mode:
        command.append("--local-mode")
    return subprocess.call(
        command,
        cwd=ROOT,
    )


def stop_process_tree(process: subprocess.Popen[bytes], timeout: float = 10) -> None:
    """Stop a child and any interpreter/runtime processes it spawned."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Nivelle 실행기")
    parser.add_argument(
        "mode",
        nargs="?",
        choices=("all", "server", "client"),
        default="all",
        help="실행 대상(기본값: all)",
    )
    parser.add_argument(
        "--keep-server",
        action="store_true",
        help="클라이언트를 닫아도 이 런처가 시작한 서버를 유지합니다.",
    )
    parser.add_argument("--gateway-endpoint")
    parser.add_argument("--provider-endpoint")
    parser.add_argument("--gateway-bind")
    parser.add_argument("--gateway-advertised-host")
    parser.add_argument("--network-diagnostics", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    gateway_endpoint = getattr(args, "gateway_endpoint", None)
    provider_endpoint = getattr(args, "provider_endpoint", None)
    gateway_bind = getattr(args, "gateway_bind", None)
    gateway_advertised_host = getattr(args, "gateway_advertised_host", None)
    network_diagnostics = bool(getattr(args, "network_diagnostics", False))
    if network_diagnostics:
        return subprocess.call(
            core_command(
                provider_endpoint=provider_endpoint,
                gateway_bind=gateway_bind,
                gateway_advertised_host=gateway_advertised_host,
                network_diagnostics=True,
            ),
            cwd=ROOT,
        )
    if args.mode == "client":
        return run_client(gateway_endpoint) if gateway_endpoint else run_client()

    print("Ministral-3-14B primary/fallback 모델 및 llama.cpp 설치 상태를 확인합니다.")
    runtime = ensure_runtime()
    configure_real_model(runtime)
    settings = load_launcher_settings()
    effective_gateway_bind = (
        gateway_bind or os.environ.get("NIVELLE_GATEWAY_BIND") or settings.server.host
    )
    gateway_url = gateway_health_url(settings.server, effective_gateway_bind)
    local_llama = should_start_local_llama(settings.models)
    local_llama_health_url = llama_health_url(settings.models) if local_llama else None
    model_name, _ = _selected_model(settings.models)
    llama: subprocess.Popen[bytes] | None = None

    if args.mode == "server":
        try:
            if local_llama_health_url and not endpoint_is_ready(local_llama_health_url):
                llama = start_llama(runtime, settings.models, settings.inference)
                print(
                    f"{model_name} 모델을 백그라운드에서 불러옵니다. "
                    "Core UI는 모델 로딩과 동시에 열립니다."
                )
            return subprocess.call(
                core_command(
                    provider_endpoint=provider_endpoint,
                    gateway_bind=gateway_bind,
                    gateway_advertised_host=gateway_advertised_host,
                    ui=True,
                ),
                cwd=ROOT,
            )
        finally:
            if llama is not None:
                stop_process_tree(llama)
    server: subprocess.Popen[bytes] | None = None
    try:
        if local_llama_health_url and not endpoint_is_ready(local_llama_health_url):
            llama = start_llama(runtime, settings.models, settings.inference)
        if not server_is_ready(gateway_url):
            server = (
                start_server(
                    provider_endpoint,
                    gateway_bind,
                    gateway_advertised_host,
                )
                if provider_endpoint or gateway_bind or gateway_advertised_host
                else start_server()
            )
            wait_for_server(server, gateway_url)
        if llama is not None and local_llama_health_url is not None:
            wait_for_llama(llama, local_llama_health_url, model_name)
        print("Nivelle Core가 준비되었습니다. 1PC 로컬 Link를 엽니다.")
        return (
            run_client(gateway_endpoint)
            if gateway_endpoint
            else run_client(
                gateway_url.removesuffix("/health"),
                local_mode=True,
            )
        )
    finally:
        if server is not None and not args.keep_server:
            stop_process_tree(server)
        if llama is not None and not args.keep_server:
            stop_process_tree(llama)


if __name__ == "__main__":
    raise SystemExit(main())
