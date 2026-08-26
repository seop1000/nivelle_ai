import argparse
from pathlib import Path
from typing import Any

import pytest
import yaml
from nivelle_protocol.settings import InferenceSettings, ModelEntry, ModelsSettings, ServerSettings

import nivelle
from nivelle_runtime import (
    FALLBACK_MODEL_PATH,
    FALLBACK_MODEL_SHA256,
    FALLBACK_MODEL_SIZE,
    PRIMARY_MODEL_PATH,
    PRIMARY_MODEL_SHA256,
    PRIMARY_MODEL_SIZE,
    PRIMARY_MODEL_URL,
    RuntimePaths,
)


def _runtime(root: Path, suffix: str = "") -> RuntimePaths:
    return RuntimePaths(
        model_path=root / f"model{suffix}.gguf",
        server_path=root / f"llama-server{suffix}.exe",
    )


def _argument(command: list[str], name: str) -> str:
    return command[command.index(name) + 1]


def test_pinned_runtime_uses_official_ministral_primary_and_qwen_fallback() -> None:
    assert PRIMARY_MODEL_PATH.name == "Ministral-3-14B-Instruct-2512-Q4_K_M.gguf"
    assert PRIMARY_MODEL_SIZE == 8_239_593_024
    assert (
        PRIMARY_MODEL_SHA256
        == "824e0f3373e69b84f2cae46fdcb9bd1ebc6ab3bfc7acc125d818b7b8178cc613"
    )
    assert "fb49df4a3cde2c774da8def12437118a66c4f5cf" in PRIMARY_MODEL_URL
    assert FALLBACK_MODEL_PATH.name == "Qwen_Qwen3.5-9B-Q4_K_M.gguf"
    assert FALLBACK_MODEL_SIZE == 6_169_341_984
    assert (
        FALLBACK_MODEL_SHA256
        == "d784ce9eda1a5a7b51e8f705a9e6310844bf4f173654d115823c775fdea56d43"
    )


def test_default_launcher_selects_ministral_primary_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("NIVELLE_CORE_DATA_DIR", str(data_dir))
    runtime = RuntimePaths(
        model_path=tmp_path / "Ministral-3-14B-Instruct-2512-Q4_K_M.gguf",
        server_path=tmp_path / "llama-server.exe",
        fallback_model_path=tmp_path / "Qwen_Qwen3.5-9B-Q4_K_M.gguf",
    )

    nivelle.configure_real_model(runtime)

    payload = yaml.safe_load(
        (data_dir / "config" / "models.yaml").read_text(encoding="utf-8")
    )
    models = ModelsSettings.model_validate(payload)
    assert [(model.id, model.role) for model in models.models] == [
        ("ministral-3-14b-instruct-2512-q4-k-m", "primary"),
        ("qwen3.5-9b-q4-k-m", "fallback"),
    ]
    command = nivelle.llama_command(runtime, models, InferenceSettings())
    assert Path(_argument(command, "--model")) == runtime.model_path
    assert _argument(command, "--alias") == "Ministral-3-14B-Instruct-2512 Q4_K_M"


def test_configure_real_model_bootstraps_once_and_preserves_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("NIVELLE_CORE_DATA_DIR", str(data_dir))
    first_runtime = _runtime(tmp_path, "-first")

    nivelle.configure_real_model(first_runtime)

    config_path = data_dir / "config" / "models.yaml"
    created = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert created["mode"] == "external"
    assert created["provider_endpoint"] == "http://127.0.0.1:8080"
    assert "external_url" not in created
    assert created["llama_server_path"] == str(first_runtime.server_path)
    assert created["models"][0]["path"] == str(first_runtime.model_path)

    custom_content = (
        "# This file belongs to the server administrator.\n"
        "mode: mock\n"
        "llama_server_path: null\n"
        "external_url: http://model-host:9000\n"
        "fallback_enabled: true\n"
        "models: []\n"
    )
    config_path.write_text(custom_content, encoding="utf-8")

    nivelle.configure_real_model(_runtime(tmp_path, "-second"))

    assert config_path.read_text(encoding="utf-8") == custom_content


def test_configure_real_model_repairs_paths_after_portable_folder_moves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install = tmp_path / "Nivelle Portable"
    server = install / "runtime" / "llama.cpp" / "b10231" / "llama-server.exe"
    model = install / "runtime" / "models" / "Qwen_Qwen3.5-9B-Q4_K_M.gguf"
    server.parent.mkdir(parents=True)
    model.parent.mkdir(parents=True)
    server.write_bytes(b"exe")
    model.write_bytes(b"gguf")

    data_dir = tmp_path / "data"
    config_dir = data_dir / "config"
    config_dir.mkdir(parents=True)
    monkeypatch.setenv("NIVELLE_CORE_DATA_DIR", str(data_dir))
    monkeypatch.setattr(nivelle, "ROOT", install)
    config_path = config_dir / "models.yaml"
    config_path.write_text(
        "mode: external\n"
        "llama_server_path: D:/Old-Nivelle/runtime/llama.cpp/b10231/llama-server.exe\n"
        "external_url: http://127.0.0.1:8080\n"
        "fallback_enabled: false\n"
        "models:\n"
        "  - id: qwen3.5-9b-q4-k-m\n"
        "    name: Qwen3.5-9B Q4_K_M\n"
        "    path: D:/Old-Nivelle/runtime/models/Qwen_Qwen3.5-9B-Q4_K_M.gguf\n"
        "    role: primary\n"
        "    enabled: true\n",
        encoding="utf-8",
    )

    runtime = RuntimePaths(model_path=model, server_path=server)
    nivelle.configure_real_model(runtime)

    repaired = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert Path(repaired["llama_server_path"]) == server.relative_to(install)
    assert Path(repaired["models"][0]["path"]) == model.relative_to(install)
    command = nivelle.llama_command(
        runtime,
        nivelle.load_launcher_settings().models,
        InferenceSettings(),
    )
    assert Path(command[0]) == server.resolve()
    assert Path(_argument(command, "--model")) == model.resolve()


def test_configure_real_model_migrates_legacy_automatic_qwen_primary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install = tmp_path / "Nivelle Portable"
    server = install / "runtime" / "llama.cpp" / "b10231" / "llama-server.exe"
    ministral = (
        install
        / "runtime"
        / "models"
        / "Ministral-3-14B-Instruct-2512-Q4_K_M.gguf"
    )
    qwen_27b = install / "runtime" / "models" / "Qwen_Qwen3.5-27B-Q4_K_M.gguf"
    qwen_9b = install / "runtime" / "models" / "Qwen_Qwen3.5-9B-Q4_K_M.gguf"
    server.parent.mkdir(parents=True)
    ministral.parent.mkdir(parents=True)
    for path in (server, ministral, qwen_27b, qwen_9b):
        path.write_bytes(b"artifact")

    data_dir = tmp_path / "data"
    config_dir = data_dir / "config"
    config_dir.mkdir(parents=True)
    monkeypatch.setenv("NIVELLE_CORE_DATA_DIR", str(data_dir))
    monkeypatch.setattr(nivelle, "ROOT", install)
    config_path = config_dir / "models.yaml"
    config_path.write_text(
        "mode: external\n"
        "llama_server_path: runtime/llama.cpp/b10231/llama-server.exe\n"
        "provider_endpoint: http://127.0.0.1:8080\n"
        "fallback_enabled: false\n"
        "models:\n"
        "  - id: qwen3.5-27b-q4-k-m\n"
        "    name: Qwen3.5-27B Q4_K_M\n"
        "    path: runtime/models/Qwen_Qwen3.5-27B-Q4_K_M.gguf\n"
        "    role: primary\n"
        "    enabled: true\n"
        "  - id: qwen3.5-9b-q4-k-m\n"
        "    name: Qwen3.5-9B Q4_K_M\n"
        "    path: runtime/models/Qwen_Qwen3.5-9B-Q4_K_M.gguf\n"
        "    role: fallback\n"
        "    enabled: true\n",
        encoding="utf-8",
    )
    runtime = RuntimePaths(
        model_path=ministral,
        server_path=server,
        fallback_model_path=qwen_9b,
    )

    nivelle.configure_real_model(runtime)

    migrated = ModelsSettings.model_validate(
        yaml.safe_load(config_path.read_text(encoding="utf-8"))
    )
    assert [(model.id, model.role) for model in migrated.models] == [
        ("ministral-3-14b-instruct-2512-q4-k-m", "primary"),
        ("qwen3.5-9b-q4-k-m", "fallback"),
    ]
    assert migrated.models[0].name == "Ministral-3-14B-Instruct-2512 Q4_K_M"
    assert migrated.models[0].path == Path(
        "runtime/models/Ministral-3-14B-Instruct-2512-Q4_K_M.gguf"
    )
    command = nivelle.llama_command(runtime, migrated, InferenceSettings())
    assert Path(_argument(command, "--model")) == ministral.resolve()
    assert qwen_27b.is_file()


def test_start_llama_reports_missing_configured_executable_before_spawn(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path, "-automatic")
    configured_model = tmp_path / "model.gguf"
    configured_model.write_bytes(b"gguf")
    models = ModelsSettings(
        mode="managed",
        llama_server_path=tmp_path / "missing" / "llama-server.exe",
        external_url="http://127.0.0.1:8080",
        models=[
            ModelEntry(
                id="assistant",
                name="Assistant",
                path=configured_model,
                role="primary",
            )
        ],
    )

    with pytest.raises(SystemExit, match="llama-server 실행 파일을 찾을 수 없습니다"):
        nivelle.start_llama(runtime, models, InferenceSettings())


def test_load_launcher_settings_reads_saved_server_models_and_inference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    config_dir = data_dir / "config"
    config_dir.mkdir(parents=True)
    monkeypatch.setenv("NIVELLE_CORE_DATA_DIR", str(data_dir))
    (config_dir / "server.yaml").write_text(
        "host: 127.0.0.1\nport: 9876\nlog_level: DEBUG\nmock_mode: false\n",
        encoding="utf-8",
    )
    (config_dir / "models.yaml").write_text(
        "mode: external\n"
        "llama_server_path: D:/tools/llama-server.exe\n"
        "external_url: http://127.0.0.1:9123\n"
        "fallback_enabled: false\n"
        "models:\n"
        "  - id: saved\n"
        "    name: Saved model\n"
        "    path: D:/models/saved.gguf\n"
        "    role: primary\n"
        "    enabled: true\n",
        encoding="utf-8",
    )
    (config_dir / "inference.yaml").write_text(
        "context_size: 16384\n"
        "gpu_layers: 22\n"
        "threads: 6\n"
        "batch_size: 256\n"
        "micro_batch_size: 64\n"
        "temperature: 0.25\n"
        "top_p: 0.75\n"
        "top_k: 12\n"
        "repeat_penalty: 1.05\n"
        "max_output_tokens: 333\n"
        "seed: 42\n"
        "request_timeout: 90\n"
        "concurrent_requests: 2\n"
        "streaming: true\n",
        encoding="utf-8",
    )

    settings = nivelle.load_launcher_settings()

    assert settings.server.port == 9876
    assert settings.models.external_url == "http://127.0.0.1:9123"
    assert settings.models.models[0].name == "Saved model"
    assert settings.inference.context_size == 16384
    assert settings.inference.temperature == 0.25


def test_llama_command_uses_saved_model_endpoint_and_inference_values(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, "-automatic")
    configured_server = tmp_path / "custom" / "llama-server.exe"
    configured_model = tmp_path / "custom" / "assistant.gguf"
    models = ModelsSettings(
        mode="managed",
        llama_server_path=configured_server,
        external_url="http://localhost:9123",
        fallback_enabled=False,
        models=[
            ModelEntry(
                id="assistant",
                name="Configured assistant",
                path=configured_model,
                role="primary",
            )
        ],
    )
    inference = InferenceSettings(
        context_size=16384,
        gpu_layers=27,
        threads=7,
        batch_size=384,
        micro_batch_size=96,
        temperature=0.35,
        top_p=0.81,
        top_k=17,
        repeat_penalty=1.07,
        max_output_tokens=777,
        seed=123,
        concurrent_requests=3,
    )

    command = nivelle.llama_command(runtime, models, inference)

    assert command[0] == str(configured_server)
    assert _argument(command, "--model") == str(configured_model)
    assert _argument(command, "--alias") == "Configured assistant"
    assert _argument(command, "--host") == "localhost"
    assert _argument(command, "--port") == "9123"
    assert _argument(command, "--cors-origins") == "localhost"
    assert "--no-cors-credentials" in command
    assert _argument(command, "--ctx-size") == "16384"
    assert _argument(command, "--parallel") == "3"
    assert _argument(command, "--gpu-layers") == "27"
    assert _argument(command, "--threads") == "7"
    assert _argument(command, "--batch-size") == "384"
    assert _argument(command, "--ubatch-size") == "96"
    assert _argument(command, "--temp") == "0.35"
    assert _argument(command, "--top-p") == "0.81"
    assert _argument(command, "--top-k") == "17"
    assert _argument(command, "--repeat-penalty") == "1.07"
    assert _argument(command, "--predict") == "777"
    assert _argument(command, "--seed") == "123"


@pytest.mark.parametrize(
    ("models", "expected"),
    [
        (ModelsSettings(mode="mock"), False),
        (
            ModelsSettings(mode="external", external_url="http://model-server:8080"),
            False,
        ),
        (
            ModelsSettings(mode="external", external_url="http://127.0.0.1:8080"),
            True,
        ),
        (
            ModelsSettings(
                mode="managed",
                llama_server_path=Path("llama-server.exe"),
                external_url="http://[::1]:8080",
            ),
            True,
        ),
        (
            ModelsSettings(mode="external", external_url="https://localhost:8443"),
            False,
        ),
    ],
)
def test_should_start_local_llama_only_for_owned_loopback_endpoint(
    models: ModelsSettings, expected: bool
) -> None:
    assert nivelle.should_start_local_llama(models) is expected


def test_health_urls_follow_saved_ports_and_wildcard_hosts() -> None:
    assert (
        nivelle.gateway_health_url(ServerSettings(host="0.0.0.0", port=9911))
        == "http://127.0.0.1:9911/health"
    )
    assert (
        nivelle.gateway_health_url(ServerSettings(host="::", port=9912))
        == "http://[::1]:9912/health"
    )
    models = ModelsSettings(
        mode="external", external_url="http://127.0.0.1:9913/llama/"
    )
    assert nivelle.llama_health_url(models) == "http://127.0.0.1:9913/llama/health"


def test_gateway_health_url_uses_effective_cli_bind_override() -> None:
    server = ServerSettings(host="192.168.10.20", port=9914)

    assert (
        nivelle.gateway_health_url(server, "0.0.0.0")
        == "http://127.0.0.1:9914/health"
    )


def test_core_command_propagates_network_overrides_and_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(nivelle, "project_python", lambda: tmp_path / "python.exe")

    assert nivelle.core_command(
        provider_endpoint="http://127.0.0.1:8080",
        gateway_bind="0.0.0.0",
        gateway_advertised_host="192.168.10.20",
        network_diagnostics=True,
    ) == [
        str(tmp_path / "python.exe"),
        "-m",
        "nivelle_core.main",
        "--provider-endpoint",
        "http://127.0.0.1:8080",
        "--gateway-bind",
        "0.0.0.0",
        "--gateway-advertised-host",
        "192.168.10.20",
        "--network-diagnostics",
    ]


def test_local_client_command_uses_loopback_profile_and_auto_pairing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], Path]] = []
    monkeypatch.setattr(nivelle, "project_python", lambda: tmp_path / "python.exe")

    def call(command: list[str], *, cwd: Path) -> int:
        calls.append((command, cwd))
        return 0

    monkeypatch.setattr(nivelle.subprocess, "call", call)

    assert (
        nivelle.run_client(
            "http://127.0.0.1:8765",
            local_mode=True,
        )
        == 0
    )
    assert calls == [
        (
            [
                str(tmp_path / "python.exe"),
                "-m",
                "nivelle_link.main",
                "--gateway-endpoint",
                "http://127.0.0.1:8765",
                "--local-mode",
            ],
            nivelle.ROOT,
        )
    ]


def test_network_diagnostics_runs_before_runtime_or_model_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], Path]] = []
    monkeypatch.setattr(
        nivelle,
        "parse_args",
        lambda: argparse.Namespace(
            mode="server",
            keep_server=False,
            gateway_endpoint=None,
            provider_endpoint=None,
            gateway_bind="0.0.0.0",
            gateway_advertised_host="192.168.10.20",
            network_diagnostics=True,
        ),
    )
    monkeypatch.setattr(nivelle, "project_python", lambda: tmp_path / "python.exe")
    monkeypatch.setattr(
        nivelle,
        "ensure_runtime",
        lambda: pytest.fail("diagnostics must not prepare or download the model runtime"),
    )
    monkeypatch.setattr(
        nivelle,
        "configure_real_model",
        lambda _runtime: pytest.fail("diagnostics must not configure the model runtime"),
    )

    def call(command: list[str], *, cwd: Path) -> int:
        calls.append((command, cwd))
        return 7

    monkeypatch.setattr(nivelle.subprocess, "call", call)

    assert nivelle.main() == 7
    assert calls == [
        (
            [
                str(tmp_path / "python.exe"),
                "-m",
                "nivelle_core.main",
                "--gateway-bind",
                "0.0.0.0",
                "--gateway-advertised-host",
                "192.168.10.20",
                "--network-diagnostics",
            ],
            nivelle.ROOT,
        )
    ]


@pytest.mark.parametrize(
    "models",
    [
        ModelsSettings(mode="mock"),
        ModelsSettings(mode="external", external_url="http://remote-model:8080"),
    ],
)
def test_server_mode_keeps_auto_install_but_skips_unowned_llama_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    models: ModelsSettings,
) -> None:
    runtime = _runtime(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(
        nivelle,
        "parse_args",
        lambda: argparse.Namespace(mode="server", keep_server=False),
    )
    monkeypatch.setattr(
        nivelle,
        "ensure_runtime",
        lambda: calls.append("ensure_runtime") or runtime,
    )
    monkeypatch.setattr(nivelle, "configure_real_model", lambda _runtime: None)
    monkeypatch.setattr(
        nivelle,
        "load_launcher_settings",
        lambda: nivelle.LauncherSettings(
            server=ServerSettings(), models=models, inference=InferenceSettings()
        ),
    )
    monkeypatch.setattr(
        nivelle,
        "start_llama",
        lambda *_args: pytest.fail("an unowned local llama process must not be started"),
    )
    monkeypatch.setattr(nivelle, "project_python", lambda: tmp_path / "python.exe")
    monkeypatch.setattr(nivelle.subprocess, "call", lambda *_args, **_kwargs: 0)

    assert nivelle.main() == 0
    assert calls == ["ensure_runtime"]


def test_all_mode_uses_saved_gateway_health_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _runtime(tmp_path)
    process = object()
    checked: list[str] = []
    waited: list[str] = []
    clients: list[tuple[str | None, bool]] = []
    monkeypatch.setattr(
        nivelle,
        "parse_args",
        lambda: argparse.Namespace(mode="all", keep_server=False),
    )
    monkeypatch.setattr(nivelle, "ensure_runtime", lambda: runtime)
    monkeypatch.setattr(nivelle, "configure_real_model", lambda _runtime: None)
    monkeypatch.setattr(
        nivelle,
        "load_launcher_settings",
        lambda: nivelle.LauncherSettings(
            server=ServerSettings(host="0.0.0.0", port=9988),
            models=ModelsSettings(mode="mock"),
            inference=InferenceSettings(),
        ),
    )
    monkeypatch.setattr(
        nivelle,
        "server_is_ready",
        lambda url: checked.append(url) or False,
    )
    monkeypatch.setattr(nivelle, "start_server", lambda: process)
    monkeypatch.setattr(
        nivelle,
        "wait_for_server",
        lambda _process, url: waited.append(url),
    )
    monkeypatch.setattr(
        nivelle,
        "run_client",
        lambda endpoint=None, *, local_mode=False: clients.append(
            (endpoint, local_mode)
        )
        or 0,
    )
    monkeypatch.setattr(nivelle, "stop_process_tree", lambda _process: None)

    assert nivelle.main() == 0
    assert checked == ["http://127.0.0.1:9988/health"]
    assert waited == ["http://127.0.0.1:9988/health"]
    assert clients == [("http://127.0.0.1:9988", True)]


def test_server_mode_starts_local_llama_with_saved_health_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _runtime(tmp_path)
    process = object()
    waited: list[tuple[Any, str, str]] = []
    models = ModelsSettings(
        mode="external",
        external_url="http://localhost:9444",
        models=[
            ModelEntry(
                id="local", name="Local configured model", path=runtime.model_path, role="primary"
            )
        ],
    )
    monkeypatch.setattr(
        nivelle,
        "parse_args",
        lambda: argparse.Namespace(mode="server", keep_server=False),
    )
    monkeypatch.setattr(nivelle, "ensure_runtime", lambda: runtime)
    monkeypatch.setattr(nivelle, "configure_real_model", lambda _runtime: None)
    monkeypatch.setattr(
        nivelle,
        "load_launcher_settings",
        lambda: nivelle.LauncherSettings(
            server=ServerSettings(), models=models, inference=InferenceSettings()
        ),
    )
    monkeypatch.setattr(nivelle, "endpoint_is_ready", lambda url: False)
    monkeypatch.setattr(nivelle, "start_llama", lambda *_args: process)
    monkeypatch.setattr(
        nivelle,
        "wait_for_llama",
        lambda value, url, name: waited.append((value, url, name)),
    )
    monkeypatch.setattr(nivelle, "project_python", lambda: tmp_path / "python.exe")
    monkeypatch.setattr(nivelle.subprocess, "call", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(nivelle, "stop_process_tree", lambda _process: None)

    assert nivelle.main() == 0
    assert waited == [(process, "http://localhost:9444/health", "Local configured model")]
