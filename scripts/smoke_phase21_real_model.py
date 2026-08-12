"""Run Nivelle Phase 2.1 acceptance questions against a local real Qwen model.

This script is intentionally isolated from the user's Nivelle data. It starts one
loopback-only llama-server process, creates a temporary Core database, records
the observable context and answers, and removes the temporary data on exit.
"""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import tempfile
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import yaml
from fastapi.testclient import TestClient
from nivelle_core.app import create_app
from nivelle_protocol.version import APP_VERSION, PROTOCOL_VERSION

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LLAMA = ROOT / "runtime" / "llama.cpp" / "b10231" / "llama-server.exe"
DEFAULT_MODEL = ROOT / "runtime" / "models" / "Qwen_Qwen3.5-9B-Q4_K_M.gguf"
TERMINAL_EVENTS = {"assistant.completed", "chat.cancelled", "error"}


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--llama-exe", type=Path, default=DEFAULT_LLAMA)
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument(
        "--port",
        type=int,
        help="optional loopback port; the default is an OS-selected ephemeral port",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--startup-timeout", type=float, default=180.0)
    parser.add_argument(
        "--cases",
        default="1,2,3,4,5,6,7,8",
        help="comma-separated Phase 2.1 case numbers (default: all eight)",
    )
    return parser.parse_args()


def _case_numbers(value: str) -> set[int]:
    try:
        selected = {int(item.strip()) for item in value.split(",") if item.strip()}
    except ValueError as exc:
        raise ValueError("--cases must contain comma-separated integers") from exc
    if not selected or not selected <= set(range(1, 9)):
        raise ValueError("--cases must select one or more numbers from 1 through 8")
    return selected


def _available_port(preferred: int | None) -> int:
    if preferred is None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            return int(probe.getsockname()[1])
    if not 1 <= preferred <= 65_535:
        raise ValueError("--port must be between 1 and 65535")
    for port in range(preferred, min(preferred + 100, 65_536)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("사용 가능한 loopback 테스트 포트를 찾지 못했습니다.")


def _wait_until_ready(base_url: str, process: subprocess.Popen[bytes], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last_error = "not started"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"llama-server가 준비 전에 종료되었습니다: {process.returncode}")
        try:
            response = httpx.get(f"{base_url}/health", timeout=2.0)
            if response.status_code == 200:
                return
            last_error = f"HTTP {response.status_code}"
        except httpx.HTTPError as exc:
            last_error = type(exc).__name__
        time.sleep(1.0)
    raise TimeoutError(f"llama-server 준비 시간 초과: {last_error}")


def _write_settings(data_dir: Path, base_url: str, model: Path) -> None:
    config = data_dir / "config"
    config.mkdir(parents=True, exist_ok=True)
    values = {
        "models": {
            "mode": "external",
            "llama_server_path": None,
            "external_url": base_url,
            "fallback_enabled": False,
            "models": [
                {
                    "id": "qwen-primary",
                    "name": "Qwen3.5-9B Q4_K_M",
                    "path": str(model),
                    "role": "primary",
                    "enabled": True,
                }
            ],
        },
        "inference": {
            "context_size": 8192,
            "gpu_layers": 999,
            "threads": 4,
            "batch_size": 512,
            "micro_batch_size": 128,
            "temperature": 0.2,
            "top_p": 0.9,
            "top_k": 40,
            "repeat_penalty": 1.1,
            "max_output_tokens": 256,
            "seed": 42,
            "request_timeout": 180,
            "concurrent_requests": 1,
            "streaming": True,
        },
    }
    for section, payload in values.items():
        (config / f"{section}.yaml").write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), "utf-8"
        )


def _pair(client: TestClient) -> dict[str, str]:
    code = client.app.state.services.pairing.code
    response = client.post(
        "/api/v1/pairing/complete",
        json={"code": code, "device_name": "nivelle-phase21-real-model-smoke"},
    )
    response.raise_for_status()
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _create_memory(
    client: TestClient,
    headers: dict[str, str],
    content: str,
    category: str,
    priority: int,
) -> str:
    response = client.post(
        "/api/v1/memories",
        headers=headers,
        json={"content": content, "category": category, "priority": priority},
    )
    response.raise_for_status()
    return str(response.json()["id"])


def _contains_all(value: str, terms: tuple[str, ...]) -> bool:
    folded = value.casefold()
    return all(term.casefold() in folded for term in terms)


def _contains_any(value: str, terms: tuple[str, ...]) -> bool:
    folded = value.casefold()
    return any(term.casefold() in folded for term in terms)


def _ask(
    websocket: Any,
    question: str,
    answer_check: Callable[[str], bool],
    expected_memory_id: str | None,
) -> dict[str, Any]:
    request_id = str(uuid4())
    websocket.send_json(
        {
            "type": "chat.request",
            "protocol_version": PROTOCOL_VERSION,
            "request_id": request_id,
            "client_message_id": str(uuid4()),
            "content": question,
            "runtime_context": {
                "profile_id": "primary",
                "connection_type": "local",
                # RFC 5737 TEST-NET-1; never publish a user's live LAN endpoint.
                "host": "192.0.2.10",
                "port": 8765,
                "tls": False,
                "client_version": APP_VERSION,
                "latency_ms": 1.0,
            },
        }
    )
    events: list[dict[str, Any]] = []
    while True:
        event = websocket.receive_json()
        if event.get("request_id") != request_id:
            continue
        events.append(event)
        if event.get("type") in TERMINAL_EVENTS:
            break

    types = [str(event.get("type")) for event in events]
    context_event = next(
        (event for event in events if event.get("type") == "assistant.context"), None
    )
    terminal = events[-1]
    answer = "".join(
        str(event.get("payload", {}).get("delta", ""))
        for event in events
        if event.get("type") == "assistant.delta"
    ).strip()
    context = context_event.get("payload", {}) if context_event else {}
    context_memories = context.get("memories", []) if isinstance(context, dict) else []
    selected_ids = [
        str(item.get("memory_id"))
        for item in context_memories
        if isinstance(item, dict) and item.get("included") is True
    ]
    context_json = json.dumps(context, ensure_ascii=False).casefold()
    forbidden_context_terms = (
        "authorization",
        "pairing_code",
        "password",
        "private_key",
        "bearer ",
    )
    event_order_ok = (
        "chat.accepted" in types
        and "assistant.context" in types
        and "assistant.delta" in types
        and types.index("assistant.context") < types.index("assistant.delta")
    )
    memory_ok = expected_memory_id is None or expected_memory_id in selected_ids
    terminal_ok = terminal.get("type") == "assistant.completed"
    metrics = terminal.get("payload", {}).get("metrics", {}) if terminal_ok else {}
    metrics_ok = (
        isinstance(metrics, dict)
        and isinstance(metrics.get("prompt_tokens"), int)
        and isinstance(metrics.get("completion_tokens"), int)
        and isinstance(metrics.get("tokens_per_second"), (int, float))
    )
    checks = {
        "terminal_completed": terminal_ok,
        "context_before_delta": event_order_ok,
        "expected_memory_selected": memory_ok,
        "answer_expected": answer_check(answer),
        "metrics_present": metrics_ok,
        "context_has_no_secret_fields": not any(
            term in context_json for term in forbidden_context_terms
        ),
    }
    return {
        "question": question,
        "answer": answer,
        "event_types": types,
        "selected_memory_ids": selected_ids,
        "candidate_count": context.get("retrieval", {}).get("candidate_count")
        if isinstance(context, dict)
        else None,
        "metrics": metrics,
        "checks": checks,
        "passed": all(checks.values()),
    }


def _run_gateway_smoke(
    data_dir: Path,
    base_url: str,
    model: Path,
    selected_cases: set[int],
) -> list[dict[str, Any]]:
    _write_settings(data_dir, base_url, model)
    app = create_app(data_dir)
    with TestClient(app) as client:
        headers = _pair(client)
        memory_ids = {
            "nickname": _create_memory(
                client,
                headers,
                "사용자의 기본 호칭은 히냥이이다.",
                "preference",
                100,
            ),
            "server": _create_memory(
                client,
                headers,
                "Nivelle Core PC는 시스템 RAM 16GB와 GPU 예약 메모리 8GB로 설정되어 있다.",
                "project",
                90,
            ),
            "client": _create_memory(
                client,
                headers,
                "Nivelle Link PC는 Windows 11 Pro, Ryzen 7 5700X, RAM 32GB, "
                "RTX 3060 12GB 사양이다.",
                "project",
                90,
            ),
            "architecture": _create_memory(
                client,
                headers,
                "Nivelle의 2PC는 two-phase commit이 아니라 두 대의 물리적 PC가 Core와 "
                "Link 역할을 나누는 구조다. Link는 Core PC의 사설 LAN IPv4를 쓴다.",
                "project",
                95,
            ),
            "remote": _create_memory(
                client,
                headers,
                "Nivelle 외부 접속은 공개 포트 포워딩보다 사설 VPN을 우선한다.",
                "project",
                95,
            ),
        }
        accent_id = _create_memory(
            client,
            headers,
            "Nivelle 테스트 강조색은 보라색이다.",
            "project",
            80,
        )
        updated = client.patch(
            f"/api/v1/memories/{accent_id}",
            headers=headers,
            json={"content": "Nivelle 테스트 강조색은 회색이다."},
        )
        updated.raise_for_status()
        if str(updated.json()["id"]) != accent_id:
            raise AssertionError("강조색 기억 업데이트가 같은 ID를 보존하지 않았습니다.")

        scenarios: list[tuple[str, Callable[[str], bool], str | None]] = [
            (
                "안녕. 넌 누구고 나를 어떻게 부를 거야?",
                lambda answer: (
                    _contains_any(answer, ("Nivelle", "니벨", "레시아 니벨"))
                    and "히냥이" in answer
                    and _contains_any(answer, ("당신을", "사용자님을", "히냥이님"))
                    and _contains_any(answer, ("부르겠습니다", "부를게", "호칭"))
                    and "저를 히냥" not in answer
                ),
                memory_ids["nickname"],
            ),
            (
                "내 서버 PC의 시스템 RAM과 GPU 예약 메모리는 각각 얼마야?",
                lambda answer: _contains_all(answer, ("16GB", "8GB")),
                memory_ids["server"],
            ),
            (
                "내 클라이언트 PC 사양을 알려줘.",
                lambda answer: _contains_all(
                    answer, ("Windows 11 Pro", "5700X", "32GB", "RTX 3060", "12GB")
                )
                and not _contains_any(
                    answer,
                    (
                        "기록되어 있지",
                        "명시되어 있지",
                        "정보가 없",
                        "확인되지",
                        "확인할 수 없",
                    ),
                )
                and "클라이언트" in answer
                and not _contains_any(
                    answer,
                    ("서버 PC의 현재 사양", "서버 PC의 정보", "서버 PC 정보입니다"),
                ),
                memory_ids["client"],
            ),
            (
                "2PC에서도 클라이언트 서버 주소를 127.0.0.1로 설정하면 되지?",
                lambda answer: (
                    "127.0.0.1" in answer
                    and _contains_any(
                        answer,
                        (
                            "아니",
                            "안 돼",
                            "사용하면 안",
                            "잘못",
                            "권장되지",
                            "권하지",
                            "수 없습니다",
                            "불가능",
                        ),
                    )
                    and _contains_any(answer, ("물리적", "사설 LAN", "서버 PC"))
                ),
                memory_ids["architecture"],
            ),
            (
                "현재 어떤 서버에 연결되어 있어?",
                lambda answer: _contains_all(answer, ("192.0.2.10", "8765")),
                None,
            ),
            (
                "현재 fallback 모델은 뭐야?",
                lambda answer: (
                    "fallback" in answer.casefold()
                    and _contains_any(
                        answer,
                        ("없", "구성되지", "설정되어 있지", "null"),
                    )
                ),
                None,
            ),
            (
                "외부에서 접속하려면 포트 포워딩부터 하면 돼?",
                lambda answer: (
                    "VPN" in answer
                    and _contains_any(answer, ("아니", "권장하지", "우선하지", "기본"))
                ),
                memory_ids["remote"],
            ),
            (
                "Nivelle 테스트 강조색은 뭐야?",
                lambda answer: "회색" in answer and "보라색" not in answer,
                accent_id,
            ),
        ]
        with client.websocket_connect("/ws/v1/chat", headers=headers) as websocket:
            return [
                _ask(websocket, question, check, expected_memory)
                for index, (question, check, expected_memory) in enumerate(scenarios, 1)
                if index in selected_cases
            ]


def main() -> int:
    args = _arguments()
    llama_exe = args.llama_exe.resolve()
    model = args.model.resolve()
    if not llama_exe.is_file():
        raise SystemExit(f"llama-server를 찾을 수 없습니다: {llama_exe}")
    if not model.is_file():
        raise SystemExit(f"GGUF 모델을 찾을 수 없습니다: {model}")
    try:
        selected_cases = _case_numbers(args.cases)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    port = _available_port(args.port)
    base_url = f"http://127.0.0.1:{port}"
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    output = (
        args.output or ROOT / "build" / f"nivelle-phase21-real-smoke-{stamp}.json"
    ).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    stdout_log = output.with_suffix(".llama.stdout.log")
    stderr_log = output.with_suffix(".llama.stderr.log")
    command = [
        str(llama_exe),
        "--model",
        str(model),
        "--alias",
        "Qwen3.5-9B-Q4_K_M",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--ctx-size",
        "8192",
        "--parallel",
        "1",
        "--gpu-layers",
        "999",
        "--threads",
        "4",
        "--batch-size",
        "512",
        "--ubatch-size",
        "128",
        "--flash-attn",
        "auto",
        "--jinja",
        "--reasoning",
        "off",
        "--metrics",
        "--temp",
        "0.2",
        "--top-p",
        "0.9",
        "--top-k",
        "40",
        "--repeat-penalty",
        "1.1",
        "--predict",
        "256",
        "--seed",
        "42",
    ]
    creation_flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
    with stdout_log.open("wb") as stdout, stderr_log.open("wb") as stderr:
        process = subprocess.Popen(
            command,
            cwd=llama_exe.parent,
            stdout=stdout,
            stderr=stderr,
            creationflags=creation_flags,
        )
        try:
            _wait_until_ready(base_url, process, args.startup_timeout)
            with tempfile.TemporaryDirectory(
                prefix="nivelle-phase21-real-smoke-", dir=output.parent
            ) as temporary:
                results = _run_gateway_smoke(
                    Path(temporary), base_url, model, selected_cases
                )
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=15)

    report = {
        "app_version": APP_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "model": model.name,
        "llama_server": str(llama_exe),
        # Do not publish a transient local test port in generated reports.
        "loopback_url": "http://127.0.0.1:<ephemeral>",
        "isolated_temporary_data": True,
        "result_count": len(results),
        "passed_count": sum(bool(result["passed"]) for result in results),
        "failed_count": sum(not bool(result["passed"]) for result in results),
        "results": results,
    }
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    print(output)
    print(f"real-model smoke: {report['passed_count']} passed, {report['failed_count']} failed")
    return 0 if report["failed_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
