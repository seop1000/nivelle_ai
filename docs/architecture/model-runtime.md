# Model Runtime

## 구성 요소

- `nivelle_core.llm`: 기존 OpenAI-compatible HTTP/SSE 구현과 Mock LLM
- `nivelle_core.model_runtime`: provider 계약, 상태/오류, router, lifecycle, fake provider
- `Services.model_router`: 저장된 모델 설정과 기존 provider를 runtime adapter로 연결
- `scripts/model_runtime_simulation.py`: 실제 LLM 없는 독립 복구 시뮬레이션

## Provider 계약

각 provider는 다음 메타데이터와 동작을 제공한다.

- `provider_id`, `model_id`, `endpoint`, `timeout`
- `capabilities`
- `health()`
- `generate(ModelRequest)`
- `stream(ModelRequest)`
- `cancel(request_id)`

`ModelRequest`에는 prompt message와 `request_id`, 선택적 `conversation_id`만 들어간다. Persona, Memory, Tool policy 및 UI 데이터는 provider가 직접 조회하지 않는다.

## 오류와 fallback 규칙

| 오류 | 분류 | 부분 출력 전 fallback | 부분 출력 후 fallback |
|---|---|---:|---:|
| 연결 실패 | `connection_error` | 예 | 아니요 |
| timeout | `timeout` | 예 | 아니요 |
| model health 실패 | `model_unavailable` | 예 | 아니요 |
| provider 내부 실패 | `provider_internal_error` | 예 | 아니요 |
| malformed response | `malformed_response` | 예 | 아니요 |
| 잘못된 request | `invalid_request` | 아니요 | 아니요 |
| 취소 | `cancelled` | 아니요 | 아니요 |

부분 delta가 클라이언트에 전달된 후 다른 모델의 응답을 이어 붙이면 한 답변에 두 모델의 출력이 섞인다. 따라서 stream interruption은 중단으로 기록하고 명시적 retry를 사용한다.

## Streaming 완료 권위

Provider는 delta와 final 신호를 낸다. Router는 provider의 중복 final을 하나로 줄이고 final 이후 content를 malformed response로 거부한다. WebSocket 계층은 router final을 직접 전송하지 않고, DB의 assistant message를 `completed`로 전이한 뒤 기존 `emit_durable_completion()`에서 단 한 번 완료 이벤트를 보낸다. 영구 상태의 authoritative source는 DB이다.

## 요청 수명주기와 로그

Router record에는 다음을 유지한다.

- `request_id`, `conversation_id`
- 선택된 `provider_id`, `model_id`
- `started_at`, `completed_at`, `latency_ms`, `status`
- fallback 사용 여부와 provider attempt/error type

구조화 로그에는 위 식별자와 상태만 넣는다. prompt 원문, 전체 대화, pairing token, API key, password 및 기타 secret은 넣지 않는다.

## 상태 분리

`ModelRuntimeSnapshot`은 `gateway_state=online`과 primary/fallback `ProviderHealth`를 따로 반환한다. 따라서 Gateway가 살아 있는 상태에서 Primary `FAILED`, Fallback `READY`를 표현할 수 있다. provider 상태는 `UNAVAILABLE`, `STARTING`, `READY`, `BUSY`, `DEGRADED`, `FAILED`를 사용한다.

## 설정

모델 ID와 이름은 `models.yaml`의 `models` 목록에 둔다. 각 `ModelEntry`의 `endpoint`는 선택 사항이며 없으면 Core 소유 `provider_endpoint`를 사용한다. `external_url`은 0.3.1 입력 마이그레이션에만 허용된다. Core logic에는 Qwen 모델 이름을 하드코딩하지 않는다. 기존 portable 설정은 `fallback_enabled: false`이므로 존재하지 않는 두 번째 모델을 암시하지 않는다.

## 종료 순서

현재 코드에서 확인된 순서:

1. WebSocket disconnect 또는 `chat.cancel`이 generation task를 취소한다.
2. Router가 활성 provider에 `cancel(request_id)`를 전달한다.
3. Core가 durable assistant state를 `interrupted`로 전이하고 in-flight ID를 해제한다.
4. WebSocket disconnect handler가 남은 generation task를 모두 취소하고 `gather`로 기다린다.
5. SQLite 연결은 각 DB 호출의 context manager 종료 시 닫힌다.
6. 상위 launcher는 Core process tree를 정리한 뒤 llama-server process tree를 정리한다.

Windows launcher의 `taskkill /T /F` 앞에 graceful Gateway shutdown 신호를 보내는 단계는 아직 없다. 이는 orphan 방지에는 강하지만 정상 종료 품질 측면의 후속 과제이다.

## 검증

```powershell
.\.venv\Scripts\python.exe -m pytest apps\server\nivelle_core\tests -v --tb=short
.\.venv\Scripts\python.exe .\scripts\model_runtime_simulation.py
```

두 명령 모두 저장소 루트에서 실행한다.

## 로컬 모델 설치 기준 (2026-08-10)

- Primary: `Qwen3.5-27B Q4_K_M`, 17,984,872,928 bytes, SHA-256
  `81657841d62f1821c748d0fea6c260b7d3508844fe4e9250253ef81c4e4d9edf`
- Fallback: `Qwen3.5-9B Q4_K_M`, 6,169,341,984 bytes, SHA-256
  `d784ce9eda1a5a7b51e8f705a9e6310844bf4f173654d115823c775fdea56d43`
- Runtime: llama.cpp Vulkan build `b10231`
- 설치 경로: `<현재 설치 루트>\runtime\models`
- RTX 3060 12GB 기본값: `context_size: 8192`, `gpu_layers: 42`

27B Q4 모델은 RTX 3060 12GB VRAM에 전부 적재되지 않으므로 GPU/CPU 혼합 적재를
전제로 한다. llama.cpp `llama-fit-params` 추정 기준 42개 GPU layer는 Vulkan에 약
10,922MiB, host에 약 6,613MiB를 요구한다. 게임 등으로 VRAM과 시스템 RAM이 사용
중일 때 동시에 시작하면 심한 메모리 압박이 생길 수 있다. `fallback_enabled`는 별도
endpoint 또는 여러 모델을 동시에 제공하는 endpoint가 준비되기 전까지 `false`로
유지한다.
