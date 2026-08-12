# Agent handoff

## P0 Foundation (current)

- Client executable: `Nivelle-Link.exe`; server executable: `Nivelle-Core.exe`.
- Link owns only `gateway_endpoint`. Core owns `provider_endpoint` and model routing.
- Configuration priority is CLI, environment, local config, then safe default.
- `.venv` is disposable. Never copy, package, sync, path-edit, or `venv --upgrade` it.
- Use `scripts/bootstrap_python.ps1`; it stages and swaps only after validation.
- ConnectionManager deduplicates connect/retry tasks and blocks work after shutdown.
- P0 acceptance command: `.\scripts\test_p0_portability.ps1`.
- Details: `docs/architecture/p0-foundation.md`.
- `.git_disabled` is user state. Do not rename or initialize Git without permission.

## 현재 phase

Core v2 Model Runtime 기반 구현, 실제 chat generation 경로 연결 및 전체 회귀 검증 완료. 운영 종료 개선 검토가 남아 있다.

## 최근 완료 작업

- 비활성 `.git_disabled`를 읽기 전용 기준선으로 사용해 Nemotron 추정 변경을 확인
- 잘못된 `tests/test_bug_reproduction.py`와 fixture를 실제 API 기반 12개 테스트로 교체
- provider 상태/오류/요청 lifecycle과 primary/fallback router 추가
- 실제 Conversation -> Model 흐름을 router에 연결
- 부분 stream 이후 fallback 금지, provider duplicate final 억제, cancellation 전달 구현
- 모델별 선택적 OpenAI-compatible `endpoint` 설정 추가
- 실제 LLM 없는 10개 시나리오 simulation runner 추가
- Qwen3.5-27B Q4_K_M을 검증된 primary 모델로 설치하고 기존 9B를 fallback으로 등록
- 실제 사용자 `models.yaml` 변경 전 timestamped backup 생성

## 현재 failing test

Core 및 루트 통합 suite에는 없음. Nemotron 추정 클라이언트 중첩 suite는 현재 API와 맞지 않아 별도 실행 시 `3 passed, 6 failed`이다. 주요 불일치는 `ConnectionProfile.name` 사용(`id`가 실제 필수 필드)과 존재하지 않는 `RiskLevel.LOW` 참조다. 최근 Core 결과:

- Core v2 재현 테스트: 12 passed
- 관련 unit/integration 묶음: 51 passed, 1 warning
- 구현 전 전체 기준선: 361 passed, 1 skipped, 1 warning
- 구현 후 통합 전체 회귀(Core v2 12개 포함): 374 passed, 1 skipped, 1 warning
- 독립 simulation: 10/10 passed

## 주요 architecture decision

- DB의 assistant message 상태가 최종 완료의 authoritative source이다.
- fallback은 retryable 오류이면서 해당 provider의 delta가 아직 외부로 나가지 않은 경우에만 허용한다.
- Provider에는 Persona, Memory, Conversation 및 Tool policy를 넣지 않는다.
- 기존 `llm.py` 구현은 adapter 아래에 유지해 호환성과 작은 변경 범위를 지킨다.
- 모델 이름과 endpoint는 설정 데이터이며 Core logic에 특정 Qwen 이름을 넣지 않는다.

## 다음 작업

1. 실제 두 endpoint 환경에서 primary/fallback 운영 smoke test를 별도 수행한다.
   27B live load는 GPU를 많이 쓰는 게임을 종료하고 시스템 RAM 여유를 확보한 뒤 수행한다.
2. launcher에 graceful Core shutdown 단계를 추가할지 설계한다.
3. OpenAI-compatible provider 이외의 provider를 추가할 때 adapter 단위 테스트를 먼저 작성한다.
4. `apps/client/nivelle_link/tests`의 Nemotron 추정 테스트를 실제 client API에 맞춰 별도 복구한다.

## 절대 건드리지 말아야 할 부분

- `.git_disabled`를 임의로 `.git`으로 복구하거나 새로 `git init`하지 않는다.
- 기존 사용자 데이터, `.nivelle`, runtime model, pairing 정보 및 DB를 테스트에 사용하지 않는다.
- `reset --hard`, `clean -fd`, force push를 사용하지 않는다.
- WebSocket 완료 중복 방지를 UI에 따로 복제하지 않는다.
- 부분 출력 뒤 fallback 응답을 이어 붙이지 않는다.
