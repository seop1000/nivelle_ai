# Nivelle Lethia 통합 개발·실행·운영 가이드

이 문서는 Nivelle Lethia 0.4.0 저장소의 구현 구조, 생성 파일, 실행 과정,
설정, 오류 코드, 사용법, 빌드, 업데이트, 테스트와 구 `Nozomi` 호환 파일의
처리 기준을 한곳에 정리한 기준 문서다.

문서 기준:

- 제품 이름: `Nivelle Lethia` / `레시아 니벨`
- 호출 이름: `Nivelle` / `니벨`
- 서버 프로그램: `Nivelle Core`
- 클라이언트 프로그램: `Nivelle Link`
- 현재 앱 버전: `0.4.0`
- 프로토콜 버전: `1.0`
- 지원 운영체제: Windows x64
- 지원 Python: `3.12 이상, 3.15 미만`

> 이 문서는 실제 코드가 기준이다. 설명과 코드가 다르면 `VERSION`,
> `pyproject.toml`, `packages/nivelle_protocol/settings.py`, `nivelle_runtime.py`와
> 각 실행 진입점의 값을 우선한다.

---

## 1. 가장 먼저 알아야 할 값

| 구분 | 값 | 용도 |
|---|---:|---|
| Gateway 기본 포트 | `8765` | Link가 Core의 HTTP/WebSocket Gateway에 접속 |
| Gateway 기본 bind | `0.0.0.0` | Core PC의 모든 IPv4 인터페이스에서 수신 |
| Gateway advertised 주소 | 자동 감지 | Link에 입력할 실제 Core PC LAN IPv4 |
| llama.cpp Provider | `127.0.0.1:8080` | Core만 사용하는 로컬 모델 API |
| Core 데이터 | `%LOCALAPPDATA%\Nivelle\NivelleCore` | DB, 설정, Persona, 로그, 백업 |
| Link 데이터 | `%LOCALAPPDATA%\Nivelle\NivelleLink` | 연결 프로필과 Link 로컬 상태 |
| Updater 데이터 | `%LOCALAPPDATA%\Nivelle\Updater` | 다운로드, staging, 업데이트 백업 |
| 모델 저장소 | `<설치 폴더>\runtime\models` | GGUF 모델 파일 |
| llama.cpp 저장소 | `<설치 폴더>\runtime\llama.cpp\b10231` | 고정 Vulkan 런타임 |

포트 `8765`와 `8080`은 서로 다른 역할이다. Link는 `8765`만 사용한다.
`8080`은 Core와 llama.cpp 사이의 loopback 전용 포트이므로 Link 연결 주소로
입력하거나 외부에 공개하면 안 된다.

---

## 2. 전체 구조

```text
Nivelle-Link.exe
  -> scripts/nivelle_executable_launcher.py
  -> scripts/run_locked.ps1
  -> nivelle.py client
  -> nivelle_link.main
  -> PySide6 UI / HTTP / WebSocket
             |
             | http://<Core LAN IPv4>:8765
             v
Nivelle-Core.exe
  -> scripts/nivelle_executable_launcher.py
  -> scripts/run_locked.ps1
  -> nivelle.py server
  -> Qwen 및 llama.cpp 설치/검증
  -> llama-server.exe (127.0.0.1:8080)
  -> nivelle_core.main (0.0.0.0:8765)
  -> FastAPI / SQLite / Persona / Archive / Agent Gateway
```

역할 경계:

- Link는 화면, 사용자 입력, 연결 프로필, 토큰 보관과 선택적 로컬 Agent 실행을 담당한다.
- Core는 인증, 대화, DB, 기억 검색, Persona, 모델 라우팅, 설정과 서버 상태를 담당한다.
- llama.cpp는 Core가 소유하며 Link가 직접 접근하지 않는다.
- 공통 메시지·설정·오류 모델은 `nivelle_protocol` 패키지에 둔다.
- EXE는 소스를 내장한 단일 앱이 아니라 설치 폴더의 외부 스크립트를 실행하는 얇은 실행기다.
  이 구조 덕분에 파일 단위 업데이트와 롤백이 가능하다.

---

## 3. 사용 기술과 코딩 원칙

| 기술 | 사용 위치 |
|---|---|
| Python 3.12~3.14 | Core, Link, 프로토콜, 런처, 빌더 |
| FastAPI / Uvicorn | Core HTTP 및 WebSocket Gateway |
| PySide6 / qasync | Link 데스크톱 UI와 asyncio 통합 |
| Pydantic | 설정, API, WebSocket 메시지 검증 |
| SQLite / aiosqlite | 대화, 기억, 설정 이력, 도구 감사 로그 |
| httpx / websockets | Link-Core 및 Core-Provider 통신 |
| keyring | Windows Credential Manager 호환 토큰 보관 |
| PowerShell 5.1 | Python bootstrap, 실행 잠금, 빌드, 업데이트, 롤백 |
| PyInstaller | 외부 스크립트를 호출하는 Windows x64 EXE 생성 |
| pytest / Ruff / mypy | 회귀 테스트, 스타일, 정적 타입 검사 |

주요 구현 원칙:

1. 설정 입력은 Pydantic으로 검증하고 알 수 없는 필드는 거부한다.
2. CLI, 환경변수, 로컬 설정과 안전 기본값의 우선순위를 명시한다.
3. Gateway와 Provider 주소를 섞지 않는다.
4. 토큰, 원문 도구 결과와 민감정보를 로그에 남기지 않는다.
5. 대화 메시지는 `message_id`, `request_id`, `client_message_id`로 중복을 방지한다.
6. 파일 쓰기와 업데이트는 임시 파일을 만든 뒤 `os.replace` 또는 원자적 이동을 사용한다.
7. 모델·업데이트 파일은 크기와 SHA-256을 모두 검증한다.
8. 0.3.1 호환 코드는 별도 bridge로 격리하고 새 코드에서는 Nivelle 이름만 사용한다.

---

## 4. 파일과 디렉터리 설명

### 4.1 저장소 루트

| 파일 | 역할 |
|---|---|
| `VERSION` | 앱 버전의 단일 기준. 현재 `0.4.0` |
| `pyproject.toml` | 의존성, 빌드 패키지, CLI entry point, pytest/Ruff/mypy 설정 |
| `README.md` | 짧은 설치·기능 소개 |
| `CHANGELOG.md` | 버전별 변경 이력 |
| `.env.example` | Core/Link 데이터 경로 재지정 예제 |
| `.gitignore` | 런타임·모델·빌드·캐시·비밀 파일 제외 규칙 |
| `nivelle.py` | Core/Link/Local 공통 상위 런처. 모델 준비와 프로세스 생명주기 담당 |
| `nivelle_runtime.py` | Qwen GGUF와 llama.cpp 다운로드, 재개, 해시 검증, 안전 압축 해제 |
| `Nivelle-Core.exe` | 서버용 얇은 Windows 실행 파일 |
| `Nivelle-Link.exe` | 클라이언트용 얇은 Windows 실행 파일 |
| `Nivelle-Local.exe` | 한 PC 통합 시험용 실행 파일 |
| `Nivelle-Updater.exe` | GitHub 업데이트 실행 파일 |
| `Nivelle-Core.cmd` | EXE 없이 Core를 실행하는 CMD 진입점 |
| `Nivelle-Link.cmd` | EXE 없이 Link를 실행하는 CMD 진입점 |
| `Nivelle-Local.cmd` | EXE 없이 통합 실행하는 CMD 진입점 |
| `Nivelle-Update.cmd` | 로컬 패치 적용 진입점 |
| `Nivelle-Update-Online.cmd` | GitHub Release 확인·다운로드·적용 진입점 |
| `Nivelle-Rollback.cmd` | 마지막 업데이트 백업으로 복원 |
| `레시아 니벨 *.cmd` | 현재 제품명을 사용한 한국어 바로가기 |

다음 디렉터리는 소스가 아니라 생성물이다.

| 디렉터리 | 내용 | Git 포함 여부 |
|---|---|---|
| `.venv` | 이 PC에서 다시 만든 Python 가상환경 | 제외 |
| `runtime` | 모델, 다운로드 조각, llama.cpp | 제외 |
| `build` | PyInstaller 중간 파일 | 제외 |
| `dist` | portable/update ZIP | 제외 |
| `.nivelle` | 실행·bootstrap 잠금 | 제외 |
| `.pytest_cache`, `.ruff_cache`, `.mypy_cache` | 개발 도구 캐시 | 제외 |

`.venv`는 다른 PC로 복사해 사용하는 파일이 아니다. 실행기는 현재 PC의 Python으로
다시 만들며, 복사되거나 깨진 기존 환경은 `.venv.broken-<날짜>`로 보존한 뒤 교체한다.

### 4.2 `packages/nivelle_protocol`

Core와 Link가 함께 사용하는 wire model과 정책을 둔다.

| 파일 | 역할 |
|---|---|
| `__init__.py` | 공용 타입과 상수 export |
| `chat.py` | `chat.request`, 취소, context, assistant completion과 ID 일치 검증 |
| `configuration.py` | CLI → 환경변수 → local config → safe default 해석 |
| `envelopes.py` | 공통 data/meta envelope |
| `errors.py` | 예약/호환 `ErrorCode`, `ErrorDetail`, `ErrorEnvelope` 카탈로그 |
| `identity.py` | Nivelle Lethia 제품·인물·컴포넌트 이름과 기본 Persona |
| `local_migration.py` | 0.3.1 데이터 디렉터리·DB를 Nivelle 경로로 안전하게 이전 |
| `memory.py` | 기억 CRUD, 검색 결과와 retrieval 설정 모델 |
| `pairing.py` | 6자리 pairing 요청·결과 모델 |
| `persona.py` | Persona identity/behavior/boundary 설정 모델 |
| `server_status.py` | Gateway, LLM, 메모리, Agent, 네트워크 상태 응답 모델 |
| `settings.py` | server/models/inference/agent/connection 설정과 범위 검증 |
| `tools.py` | 도구 registry, 상태 전이, 승인, 오류 코드와 프로토콜 모델 |
| `version.py` | 앱/프로토콜 버전, 호환성, 실행 파일·빌드 식별 정보 |
| `network/address_detection.py` | Windows 어댑터 수집, Ethernet 우선 IPv4 선택, bind/advertised 분리 |
| `network/__init__.py` | 네트워크 API export |

### 4.3 `apps/server/nivelle_core`

| 파일 | 역할 |
|---|---|
| `main.py` | Core CLI, 네트워크 해석, 진단 출력, Uvicorn 시작 |
| `app.py` | FastAPI 앱, HTTP/WebSocket route, 대화·상태·설정·Persona 조정 |
| `auth.py` | pairing 코드, 토큰 발급·검증과 관리자 권한 |
| `backend_status.py` | OpenAI 호환 Provider health/model 상태 확인 |
| `config.py` | YAML load/save/validate, 설정 revision과 restart 판정 |
| `database.py` | SQLite 초기화, 스키마 migration, 백업·무결성 처리 |
| `llm.py` | mock 및 llama.cpp/OpenAI 호환 provider adapter |
| `model_runtime.py` | primary/fallback 모델 라우팅과 요청/응답 normalization |
| `repositories.py` | 대화·메시지 저장, message ID 멱등성, 중단 복구 |
| `memory_api.py` | `/api/v1/memories` REST router |
| `memory_repository.py` | 기억 저장·검색·중복·soft state 관리 |
| `memory_retriever.py` | relevance/priority/recency 결합 검색과 prompt context 생성 |
| `persona.py` | Persona load/save/recovery와 최종 system prompt 생성 |
| `telemetry.py` | uptime, CPU, 메모리 등 상태 측정 |
| `agent_gateway.py` | Core와 Link Agent 세션, 요청·결과 correlation |
| `tool_execution.py` | 모델 도구 제안 검증과 실행 오케스트레이션 진입 |
| `tool_orchestrator.py` | 승인·timeout·동시성·취소 상태 전이 |
| `tool_repository.py` | 도구 호출, 감사 이벤트, 멱등성 레코드 저장 |
| `paths.py` | Core 데이터 경로와 0.3.1 이전 경로 migration |
| `__init__.py` | 패키지 버전 export |

### 4.4 `apps/client/nivelle_link`

| 파일 | 역할 |
|---|---|
| `main.py` | Link CLI와 `--gateway-endpoint` 처리 |
| `app.py` | Qt 앱 lifecycle, 연결·대화·상태·관리 화면 조정 |
| `windows.py` | 메인 채팅, 메뉴, 연결, Core 관리, Archive, Persona, Agent UI |
| `network.py` | HTTP/WebSocket client, reconnect state machine, stale generation guard |
| `storage.py` | 연결 프로필, 데이터 경로, keyring token, endpoint 검증 |
| `agent_controller.py` | Link 로컬 도구 요청, 승인 UI와 결과 전송 조정 |
| `__init__.py` | Link 앱 버전 export |

`apps/client/nivelle_link/agent`의 파일은 다음처럼 나뉜다.

| 파일 | 역할 |
|---|---|
| `models.py` | Agent 정책·요청·결과 내부 모델 |
| `runtime.py` | registry와 실제 도구 실행 runtime |
| `protocol_adapter.py` | 공통 tool wire model과 Link 내부 모델 변환 |
| `policy.py`, `approvals.py` | 위험 등급, 허용 범위, 1회/세션/항상 승인 |
| `path_security.py` | 허용 root, symlink/reparse, 민감 경로 검사 |
| `idempotency.py` | 동일 도구 요청 재실행 방지 |
| `atomic_store.py` | 로컬 정책·상태의 원자적 저장 |
| `audit.py` | 원문 대신 안전 요약을 남기는 감사 로그 |
| `errors.py`, `result_utils.py` | 안전한 오류와 결과 크기/요약 처리 |
| `system_status.py` | 시스템 상태 읽기 |
| `active_window.py` | 활성 창 정보 읽기 |
| `application.py` | 허용된 애플리케이션 실행·상태 처리 |
| `folder.py`, `search.py`, `text_file.py` | 제한된 폴더/검색/텍스트 파일 작업 |
| `note.py`, `reminder.py` | 로컬 메모와 알림 데이터 처리 |
| `__init__.py` | Agent 패키지 export |

### 4.5 `config/examples`

이 디렉터리는 설정 형식과 권장값을 보여 주는 참조 템플릿이다. 현재
`setup_dev.ps1`는 이 YAML을 Core 데이터 디렉터리로 복사하지 않는다. 실제 실행은
설정 파일이 없으면 Pydantic 기본값을 사용하고, Core 관리 UI 또는 `ConfigService`가
저장할 때 `%LOCALAPPDATA%\Nivelle\NivelleCore\config` 아래에 실제 YAML을 만든다.

| 파일 | 역할 |
|---|---|
| `server.yaml` | Gateway bind, advertised host, 포트, 로그 수준, mock flag |
| `models.yaml` | managed/mock/external 모드, provider, primary/fallback 모델 |
| `inference.yaml` | context, GPU layer, thread, batch, sampling, timeout |
| `memory.yaml` | Archive 활성화, 자동 추출, 검색/점수 가중치 |
| `agent.yaml` | Agent 호출 수, 승인/결과 timeout, 감사 보존 |
| `network.yaml` | Link 연결 프로필 예제. 실제 Link 저장 파일과는 별개 |
| `tools.yaml` | 이전 Phase 2 도구 설정 예제. 활성 registry 기준은 코드 |
| `voice.yaml` | 보류된 STT/TTS 설정 자리 |

### 4.6 `scripts`

| 파일 | 역할 |
|---|---|
| `bootstrap_python.ps1` | 호환 Python 탐색·설치, 새 `.venv` 구성·검증·교체 |
| `setup_dev.ps1` | 개발 의존성을 포함한 환경 준비 |
| `run_locked.ps1` | `.nivelle`/`.nozomi` 동시 실행 잠금과 `nivelle.py` 호출 |
| `run_server.ps1`, `run_client.ps1` | 개발용 짧은 실행 wrapper |
| `ensure_venv.cmd` | CMD에서 Python bootstrap 호출 |
| `check_environment.ps1` | Python, import, llama 위치와 기본 환경 확인 |
| `backup_nivelle_data.ps1` | DB 무결성 검증을 포함한 Core 데이터 백업 |
| `audit_runtime_memories.py` | 실제 DB 기억 상태, 개인정보 형태, 불일치 점검 |
| `nivelle_executable_launcher.py` | EXE가 설치 root를 검증하고 PowerShell 명령을 안전하게 생성 |
| `build_executables.ps1` | PyInstaller 6.21.0으로 4개 얇은 x64 EXE 생성·smoke test |
| `build_portable.py`, `build_portable.ps1` | 배포 허용 파일만 deterministic ZIP으로 생성·검증 |
| `build_update.ps1` | 이전 portable과 현재 트리의 파일·hash diff 패치 생성 |
| `apply_update.ps1` | manifest 검증, 프로세스/lock 확인, 백업 후 원자적 패치 |
| `rollback_update.ps1` | 백업 metadata와 hash를 검증한 뒤 이전 버전 복원 |
| `update_from_github.ps1` | stable GitHub Release와 SHA-256 sidecar 다운로드·선택 |
| `verify_update.ps1` | 실제 이전 portable에 apply → rollback → reapply 검증 |
| `run_tests.ps1` | pytest → Ruff → mypy 순서의 개발 검증 |
| `test_server_health.ps1` | Core health 확인 |
| `test_client_server_connection.ps1` | Link-Core 연결 smoke test |
| `test_reconnect.ps1` | 재연결 동작 확인 |
| `test_p0_portability.ps1` | 복사된 경로·별도 데이터 root에서 P0 연결 검증 |
| `model_runtime_simulation.py` | 다운로드/해시/설치 실패를 실제 대용량 다운로드 없이 시험 |
| `smoke_phase21_real_model.py` | 실제 Qwen3.5-9B로 Phase 2.1 질의 acceptance 실행 |

### 4.7 테스트

`tests/unit`은 한 클래스·함수·상태 전이를 격리해 검증한다. 주요 파일군은 다음과 같다.

- 네트워크·실행기: `test_address_detection.py`, `test_link_network_addresses.py`,
  `test_network.py`, `test_p0_foundation.py`, `test_launcher.py`,
  `test_executable_launcher.py`, `test_online_updater.py`, `test_update_builder.py`,
  `test_updater_scripts.py`, `test_portable_builder.py`
- 대화·중복 방지: `test_client_chat.py`, `test_client_conversations.py`,
  `test_conversation_repository.py`, `test_uncertain_write_dedupe.py`,
  `test_history_budget.py`, `test_protocol.py`
- Core·모델·설정: `test_auth.py`, `test_backend_status.py`, `test_configuration.py`,
  `test_database_migrations.py`, `test_llm.py`, `test_server_status.py`,
  `test_version_consistency.py`, `test_local_migration.py`
- Archive·Persona: `test_memory.py`, `test_memory_retriever.py`,
  `test_memory_operational_scripts.py`, `test_persona.py`, `test_client_memory.py`
- Agent·도구: `test_agent_controller.py`, `test_agent_gateway.py`,
  `test_agent_idempotency.py`, `test_agent_management_ui.py`, `test_agent_paths.py`,
  `test_agent_policy.py`, `test_agent_tools.py`, `test_search_security.py`,
  `test_tool_approval_ui.py`, `test_tool_execution.py`, `test_tool_orchestrator.py`,
  `test_tool_protocol.py`, `test_tool_repository.py`
- Link UI: `test_client_admin.py`, `test_client_app.py`, `test_client_storage.py`

`tests/integration`은 실제 FastAPI lifespan과 SQLite를 사용한다.

- `test_api.py`: health, pairing, 상태, 설정, network status
- `test_chat_phase21.py`: streaming, metrics, interruption, message ID
- `test_conversation_history.py`: 저장·재시작·history dedupe
- `test_memory_api.py`: Archive REST API
- `test_persona_api.py`: Persona load/save/recovery
- `test_agent_api.py`: Agent WebSocket/API lifecycle

`apps/server/nivelle_core/tests`와 `apps/client/nivelle_link/tests`에는 발견된 회귀를
실제 사용자 흐름으로 재현하는 추가 테스트가 있다.

### 4.8 문서

| 문서군 | 내용 |
|---|---|
| `ARCHITECTURE.md`, `architecture/*.md` | 전체 구조, Core v2, 모델 runtime, P0 경계 |
| `CONFIGURATION.md`, `WINDOWS_SETUP.md`, `DEPENDENCIES.md` | 설치·설정·네트워크·의존성 |
| `PROTOCOL.md`, `PROTOCOL_EVENTS.md`, `RECONNECT_STATE_MACHINE.md` | HTTP/WebSocket와 재연결 |
| `PHASE2_*`, `MEMORY_RETRIEVAL.md` | Phase 2 계획·결과·기억 검색 |
| `TOOLS_ARCHITECTURE.md`, `TOOL_*`, `CLIENT_PERMISSIONS.md` | Agent 도구·승인·권한 |
| `SECURITY.md`, `*_THREAT_MODEL.md`, `WINDOWS_PATH_SECURITY.md` | 위협 모델과 Windows 경로 보안 |
| `UPDATES.md`, `ONLINE_UPDATES.md`, `DATABASE_MIGRATIONS.md` | 패치·롤백·DB migration |
| `NIVELLE_RENAME_*`, `reports/*` | 이름 전환과 단계별 감사 기록 |
| `NIVELLE_LETHIA_PERSONA_V1.md` | Persona 원문 기준 |

---

## 5. 프로그램이 실행되는 과정

### 5.1 EXE 공통 단계

1. `Nivelle-*.exe`가 자기 위치를 설치 root로 결정한다.
2. `scripts/nivelle_executable_launcher.py`가 필수 외부 파일과 root 경계를 확인한다.
3. PowerShell을 argument array로 실행해 문자열 명령 주입을 피한다.
4. `scripts/run_locked.ps1`가 역할별 lock을 얻는다.
5. `bootstrap_python.ps1`가 Python과 `.venv`를 검사한다.
6. `.venv\Scripts\python.exe nivelle.py <mode>`를 실행한다.

EXE에 앱 소스나 모델이 내장되어 있지 않으므로 EXE만 다른 폴더로 단독 복사하면
실행되지 않는다. portable ZIP 전체를 같은 구조로 풀거나 기존 설치의 EXE를 교체해야 한다.

### 5.2 Python 자동 설치와 `.venv`

`bootstrap_python.ps1`의 처리 순서:

1. `-PythonPath`, `NIVELLE_PYTHON`, `py -3.x`, PATH, 알려진 설치 경로를 검사한다.
2. `3.12 <= version < 3.15`, Windows x64인지 검사한다.
3. Python이 없으면 WinGet 사용자 범위 설치를 시도한다.
4. WinGet을 쓸 수 없으면 python.org 서명 64비트 installer를 사용한다.
5. 새 venv를 임시 디렉터리에 만든다.
6. 프로젝트를 editable install하고 import smoke test를 수행한다.
7. 검증 성공 후 기존 `.venv`와 원자적으로 교체한다.

서버 PC로 폴더를 복사했을 때 이전 PC의 Python 절대 경로를 계속 사용하지 않는 이유가
이 단계에 있다.

### 5.3 모델과 llama.cpp

Core 또는 Local 모드는 시작 시 다음 세 파일을 검사한다.

| 항목 | 파일 | 크기/검증 |
|---|---|---|
| primary | `Qwen_Qwen3.5-27B-Q4_K_M.gguf` | 약 17.98GB, 고정 SHA-256 |
| fallback | `Qwen_Qwen3.5-9B-Q4_K_M.gguf` | 약 6.17GB, 고정 SHA-256 |
| runtime | `llama-b10231-bin-win-vulkan-x64.zip` | 약 34MB, 고정 SHA-256 |

파일이 없거나 크기/hash가 다르면 다시 다운로드한다. `.part` 파일이 있으면 HTTP Range로
이어받고, 서버가 Range를 무시하면 처음부터 다시 받는다. 압축 파일은 절대경로,
`..`, symlink와 설치 디렉터리 이탈을 거부한 뒤 임시 폴더에 풀어 교체한다.

첫 Core/Local 실행은 **27B와 9B를 모두** 준비한다. 새 `models.yaml`에는 27B가
primary, 9B가 fallback으로 기록된다. 따라서 "9B만 설치"하는 동작은 아니다.
Link 모드는 모델이나 llama.cpp를 설치하지 않는다.

### 5.4 Core 시작

1. 서버 설정을 읽는다.
2. CLI/환경변수/local config 순서로 Gateway bind와 provider를 해석한다.
3. advertised host가 없으면 Windows adapter/route를 수집한다.
4. 물리 Ethernet, 물리 Wi-Fi 순서로 실제 IPv4를 선택한다.
5. llama.cpp가 로컬 managed endpoint면 `127.0.0.1:8080`으로 시작한다.
6. provider health가 준비될 때까지 기다린다.
7. Core Uvicorn Gateway를 `0.0.0.0:8765`에 시작한다.
8. DB migration과 중단된 generation 복구를 수행한다.
9. pairing이 필요하면 6자리 코드를 생성한다.

네트워크만 확인할 때는 모델을 준비하지 않고 다음 명령으로 종료할 수 있다.

```powershell
.\Nivelle-Core.exe --network-diagnostics
```

### 5.5 Link 시작

1. 연결 프로필을 `priority` 순으로 읽는다.
2. `--gateway-endpoint`, `NIVELLE_GATEWAY_ENDPOINT`, 저장 프로필 순으로 endpoint를 고른다.
3. keyring에서 해당 `host:port`의 토큰을 읽는다.
4. `/health`, 인증 status와 chat WebSocket에 연결한다.
5. 연결이 끊기면 generation token을 갱신하고 하나의 reconnect task만 실행한다.
6. 이전 세대 callback과 중복 `assistant.completed` 이벤트를 버린다.
7. 대화 기록을 `message_id` 기준으로 합쳐 한 번만 표시한다.

저장된 remote endpoint는 자동으로 다른 IP로 바꾸지 않는다. Core PC의 IP가 바뀌면
Core 진단에서 새 advertised 주소를 확인하고 Link의 `서버 연결` 메뉴에서 수정한다.

### 5.6 Local 시작

`Nivelle-Local.exe`는 개발·시험을 위해 같은 PC에서 llama.cpp, Core와 Link를 함께
실행한다. 실제 두 PC 운영에서는 서버에 Core, 사용자 PC에 Link를 각각 실행한다.

### 5.7 종료

Local 모드는 자신이 시작한 Core/llama.cpp process tree를 종료한다. Python 진입점인
`.\.venv\Scripts\python.exe .\nivelle.py all --keep-server`를 사용하면 Link가 닫혀도
서버 프로세스를 유지한다. 현재 얇은 `Nivelle-Local.exe` 인자 파서는
`--keep-server`를 받지 않는다. 개별 Core와 Link EXE는 각각 독립적으로 종료한다.
업데이트 전에는 Core, Link, Local, llama-server를 모두 닫아야 한다.

---

## 6. 설치와 기본 사용법

### 6.1 새 portable 설치

1. `Nivelle-Windows-x64-0.4.0.zip`을 짧고 쓰기 가능한 경로에 푼다.
2. 서버 PC에서 `Nivelle-Core.exe`를 실행한다.
3. Python, Qwen과 llama.cpp 설치가 끝날 때까지 기다린다.
4. 별도 콘솔에서 네트워크 진단 주소와 pairing 코드를 확인한다.
5. 클라이언트 PC에서 같은 배포본의 `Nivelle-Link.exe`를 실행한다.
6. `≡` → `서버 연결`에서 Core의 advertised IPv4와 포트 `8765`를 입력한다.
7. 6자리 코드를 입력해 pairing한다.

현재 PC의 실제 예시는 다음과 같다. 다른 서버 PC에서는 반드시 진단 결과를 사용한다.

```text
Core bind:       0.0.0.0:8765
Link endpoint:   192.168.219.100:8765
Provider:        127.0.0.1:8080
```

Windows 네트워크 프로필이 Public이면 Core PC 방화벽에서 의도한 subnet/profile에만
인바운드 TCP `8765`를 허용해야 할 수 있다. Provider `8080`은 허용하지 않는다.

### 6.2 채팅

1. 메인 입력창에 메시지를 쓴다.
2. 전송할 때마다 새 `request_id`와 `client_message_id`가 생성된다.
3. Core는 user message를 먼저 저장한 뒤 assistant message ID를 예약한다.
4. streaming delta는 임시 bubble에만 누적한다.
5. `assistant.completed`의 canonical message를 한 번만 확정한다.
6. 재연결 후 history가 돌아와도 같은 `message_id`는 다시 표시하지 않는다.

### 6.3 대화 기록

`≡` 메뉴의 저장된 대화에서 이전 conversation을 연다. 기록은 Link 파일이 아니라
Core DB에 저장되므로 다른 Link에서도 같은 Core와 인증하면 불러올 수 있다.

### 6.4 Nivelle Archive

- `장기 기억`에서 직접 추가·수정·비활성화·삭제한다.
- 기본 자동 추출은 `false`다.
- 활성이고 명시적으로 저장된 기억만 검색 대상으로 쓴다.
- relevance 70%, priority 20%, recency 10% 기본 가중치를 사용한다.
- 기본 `top_k`는 5다.
- 삭제/비활성 기억은 기본 prompt context에 포함하지 않는다.

### 6.5 Persona

`성격` 메뉴에서 identity와 behavior를 편집한다. 기본 Persona는
`Nivelle Lethia Persona v1.0`이며 이름은 `레시아 니벨`, 호출명은 `니벨`이다.
저장 실패 시 원본과 backup을 사용해 복구하며 둘 다 실패하면 오류를 명시한다.

### 6.6 Core 관리

서버 관리 화면에서 다음을 볼 수 있다.

- Core/Provider/DB/embedding/Agent 상태
- bind와 실제 advertised endpoint 및 선택 출처
- 모델 이름, quantization, latency, token 처리량
- 설정과 revision, restart 필요 여부
- 최근 도구 실패의 안전한 요약

`host`, `port`, context/GPU/thread/batch, 모델 경로처럼 프로세스 구성에 영향을 주는
변경은 `pending_restart`가 된다.

---

## 7. 설정 기준

### 7.1 데이터 경로 환경변수

| 환경변수 | 역할 |
|---|---|
| `NIVELLE_CORE_DATA_DIR` | Core 데이터 root 강제 지정 |
| `NIVELLE_LINK_DATA_DIR` | Link 데이터 root 강제 지정 |
| `NIVELLE_SERVER_DATA_DIR` | Core의 현재 보조 alias |
| `NIVELLE_CLIENT_DATA_DIR` | Link의 현재 보조 alias |
| `NIVELLE_PYTHON` | bootstrap에 사용할 Python executable |
| `NIVELLE_LLAMA_SERVER_PATH` | `check_environment.ps1` 진단에서만 llama-server 경로 지정 |

`NOZOMI_*` 환경변수는 0.3.1 migration 전용 fallback이다. 새 설정에 사용하지 않는다.
`.env` 파일을 자동으로 읽는 코드는 없다. 환경변수는 PowerShell 또는 Windows 환경
설정에서 직접 지정해야 한다. 실제 runtime의 llama 실행 파일 경로는
`models.yaml`의 `llama_server_path`이며 `NIVELLE_LLAMA_SERVER_PATH`가 바꾸지 않는다.

### 7.2 endpoint 우선순위

Gateway bind:

```text
--gateway-bind
  > NIVELLE_GATEWAY_BIND
  > server.yaml host
  > 0.0.0.0
```

Gateway advertised:

```text
--gateway-advertised-host
  > NIVELLE_GATEWAY_ADVERTISED_HOST
  > server.yaml advertised_host
  > Windows LAN IPv4 자동 감지
  > unavailable 명시
```

Link endpoint:

```text
--gateway-endpoint
  > NIVELLE_GATEWAY_ENDPOINT
  > 저장된 connections.yaml profile
```

Provider endpoint:

```text
--provider-endpoint
  > NIVELLE_PROVIDER_ENDPOINT
  > models.yaml provider_endpoint
  > http://127.0.0.1:8080
```

### 7.3 주요 CLI

```powershell
# Core
.\Nivelle-Core.exe
.\Nivelle-Core.exe --gateway-bind 0.0.0.0
.\Nivelle-Core.exe --gateway-advertised-host 192.168.1.20
.\Nivelle-Core.exe --provider-endpoint http://127.0.0.1:8080
.\Nivelle-Core.exe --network-diagnostics

# Link
.\Nivelle-Link.exe
.\Nivelle-Link.exe --gateway-endpoint http://192.168.1.20:8765

# Local
.\Nivelle-Local.exe

# Link 종료 뒤에도 자신이 시작한 Core/llama.cpp를 유지할 때만 Python으로 실행
.\.venv\Scripts\python.exe .\nivelle.py all --keep-server
```

### 7.4 주요 inference 기본값

| 항목 | 기본값 |
|---|---:|
| context size | `8192` |
| GPU layers | `42` |
| threads | `4` |
| batch / micro batch | `512 / 128` |
| temperature | `0.7` |
| top-p / top-k | `0.9 / 40` |
| repeat penalty | `1.1` |
| max output tokens | `1024` |
| request timeout | `120초` |
| concurrent requests | `1` |
| streaming | `true` |

---

## 8. API와 프로토콜

### 8.1 HTTP endpoint

| Method | Path | 인증 | 역할 |
|---|---|---|---|
| GET | `/health` | 없음 | Gateway process health |
| GET | `/api/v1/pairing/status` | 없음 | pairing 필요 여부 |
| GET | `/api/v1/pairing/local-code` | Core PC loopback만 | 6자리 코드 확인 |
| POST | `/api/v1/pairing/complete` | 코드 | token 발급 |
| GET | `/api/v1/status` | Bearer | Core/LLM/DB/network/Agent 상태 |
| GET | `/api/v1/conversations` | Bearer | 대화 목록 |
| GET | `/api/v1/conversations/{id}/messages` | Bearer | 대화 메시지 |
| GET | `/api/v1/conversations/{id}/tool-calls` | Bearer | 도구 카드 metadata |
| GET/POST | `/api/v1/memories` | Bearer | 기억 목록/생성 |
| GET | `/api/v1/memories/search` | Bearer | 기억 검색 |
| GET/PATCH/DELETE | `/api/v1/memories/{id}` | Bearer | 기억 상세/수정/삭제 |
| GET/PUT | `/api/v1/persona` | 관리자 | Persona 읽기/저장 |
| GET | `/api/v1/settings` | 관리자 | 모든 설정 |
| GET/PUT | `/api/v1/settings/{section}` | 관리자 | 섹션 읽기/저장 |
| POST | `/api/v1/settings/validate` | 관리자 | 저장 전 검증 |
| GET | `/api/v1/settings/revisions` | 관리자 | 설정 이력 |
| POST | `/api/v1/settings/rollback/{id}` | 관리자 | 설정 revision 복원 |

### 8.2 WebSocket

| Path | 역할 |
|---|---|
| `/ws/v1/chat` | chat request, context, streaming delta, completion, cancel |
| `/ws/v1/agent` | capability 등록, 도구 요청·승인·진행·결과 |

대화 completion의 `message_id`, `assistant_message_id`, `message.id`는 반드시 같은
UUID여야 한다. Link는 완료 ID set과 history ID set으로 중복 렌더링을 막는다.
재시도는 새 `request_id`를 사용하되 원래 `client_message_id`를
`retry_of_client_message_id`로 명시한다.

---

## 9. 데이터와 보안

### 9.1 Core DB

기본 DB는 `%LOCALAPPDATA%\Nivelle\NivelleCore\database\nivelle.db`다.

저장 대상:

- clients와 token hash
- conversations와 messages
- memories와 검색 상태
- settings revisions
- tool calls, capability, audit events
- schema migration version

평문 인증 token은 DB에 저장하지 않는다. Link의 token은 keyring에 저장하고 Core에는
검증용 hash만 둔다.

### 9.2 로그에 남기지 않는 값

- pairing code 원문
- Bearer token과 keyring secret
- 도구 argument/result 원문
- `.env` 내용
- 사용자 대화·기억 원문을 포함한 불필요한 debug dump

### 9.3 백업

```powershell
.\scripts\backup_nivelle_data.ps1
```

백업 전에 SQLite `integrity_check`를 실행하고 복사본도 다시 검사한다. 업데이트 백업은
사용자 DB 전체 복사가 아니라 변경될 앱 파일만 대상으로 하며 runtime, 모델, venv,
사용자 설정과 secrets는 패치 대상에서 제외한다.

---

## 10. 오류 코드와 조치

### 10.1 실제 채팅 WebSocket 오류 코드

아래 코드는 현재 Core가 `chat.error` 이벤트로 실제 송신한다. 새 사용자 전송은 매번 새
`request_id`와 `client_message_id`를 써야 하며, 완료된 assistant message는
`message_id`당 한 번만 표시해야 한다.

| 코드 | 발생 조건 | 조치 |
|---|---|---|
| `INVALID_REQUEST` | JSON 또는 `ChatRequest` 형식 오류 | Core와 Link 버전을 맞추고 요청 schema 확인 |
| `DUPLICATE_REQUEST` | 처리 중이거나 완료된 `request_id` 재사용 | 새 전송에 새 `request_id` 생성 |
| `DUPLICATE_MESSAGE` | DB/inflight의 `client_message_id` 재사용 | 새 사용자 메시지에 새 ID 생성 |
| `RETRY_TARGET_NOT_FOUND` | 재시도할 원본 assistant message가 없음 | 기록을 새로 불러와 실제 중단 메시지 선택 |
| `RETRY_ALREADY_CREATED` | 같은 중단 메시지의 재시도가 이미 존재 | 기존 재시도 완료를 기다림 |
| `RETRY_TARGET_NOT_INTERRUPTED` | 완료됐거나 아직 생성 중인 메시지를 재시도 | 완료를 기다리거나 새 메시지 전송 |
| `RETRY_CONVERSATION_MISMATCH` | 원본과 다른 대화에서 재시도 | 원본과 같은 대화에서 재시도 |
| `CONVERSATION_BUSY` | 같은 대화에서 응답 생성 중 | 기존 생성 완료 뒤 새 ID로 전송 |
| `CONVERSATION_NOT_FOUND` | 대화가 없거나 보관됨 | 목록 새로고침 또는 새 대화 생성 |
| `PROMPT_TOO_LARGE` | Persona·기억·기록·입력이 context 예산 초과 | 입력·기억·기록을 줄이거나 context 조정 |
| `LLM_STREAM_INTERRUPTED` | Provider 오류, timeout 또는 stream 중단 | llama 상태 확인 후 중단 메시지를 명시적으로 재시도 |
| `PROTOCOL_VERSION_MISMATCH` | Core/Link protocol major 불일치 | 같은 배포 버전으로 업데이트 |
| `INTERNAL_ERROR` | 알 수 없는 client event type 등 내부 오류 | 최신 Link 사용, Core 로그와 요청 ID 보존 |

### 10.2 예약/호환 공통 `ErrorCode` 카탈로그

아래 19개는 `packages/nivelle_protocol/errors.py`에 선언되어 있지만, 현재 HTTP/WS
handler는 이 enum이나 `ErrorEnvelope`를 직접 만들지 않는다. 같은 문자열 일부가 실제
채팅 오류로 쓰이더라도 **enum 기반 통합 오류 응답이 보장된다는 뜻은 아니다.** 향후
통합을 위한 예약/호환 목록으로 본다.

| 코드 | 뜻 | 우선 조치 |
|---|---|---|
| `AUTH_REQUIRED` | 인증 header/token 없음 | 다시 pairing하거나 token 확인 |
| `AUTH_INVALID` | token이 유효하지 않음 | Link의 연결을 삭제 후 재-pairing |
| `PAIRING_REQUIRED` | 등록된 client가 없음 | Core의 6자리 코드 입력 |
| `PAIRING_CODE_INVALID` | 코드 불일치 | Core에서 현재 코드를 다시 확인 |
| `PAIRING_CODE_EXPIRED` | 10분 유효시간 만료 | 새 코드를 생성해 다시 입력 |
| `PROTOCOL_VERSION_MISMATCH` | protocol major 불일치 | Core와 Link를 같은 release로 업데이트 |
| `SERVER_OFFLINE` | Gateway에 연결할 수 없음 | IP, `8765`, Core 실행, 방화벽 확인 |
| `MODEL_NOT_CONFIGURED` | 활성 primary 모델 없음 | models 설정에서 primary 활성화 |
| `MODEL_FILE_NOT_FOUND` | GGUF 경로 없음 | 자동 설치 재실행 또는 모델 경로 수정 |
| `MODEL_LOAD_FAILED` | llama.cpp 모델 로드 실패 | VRAM/RAM, GGUF hash, GPU layer 확인 |
| `MODEL_UNAVAILABLE` | Provider가 요청을 받을 수 없음 | llama-server 콘솔과 `/health` 확인 |
| `LLM_REQUEST_TIMEOUT` | 설정 시간 내 응답 없음 | 모델 부하, timeout, context 크기 확인 |
| `LLM_STREAM_INTERRUPTED` | streaming 중 연결 종료 | Core 상태 확인 후 새 요청/명시적 재시도 |
| `DATABASE_ERROR` | SQLite 작업 실패 | 디스크, 권한, integrity와 backup 확인 |
| `CONFIG_VALIDATION_FAILED` | 설정 형식·범위 오류 | 응답 details와 해당 YAML/UI 값 수정 |
| `CONFIG_APPLY_FAILED` | 검증 후 적용/저장 실패 | 쓰기 권한과 설정 revision 확인 |
| `SETTINGS_ROLLBACK_FAILED` | 설정 revision 복원 실패 | revision 존재 여부와 데이터 디렉터리 확인 |
| `PERMISSION_DENIED` | 관리자/도구 권한 부족 | 관리자 token 또는 Agent 정책 확인 |
| `INTERNAL_ERROR` | 분류되지 않은 서버 오류 | Core console/log와 request ID 확인 |

### 10.3 기억 API 코드

| 코드/상태 | 발생 조건 | 조치 |
|---|---|---|
| `MEMORY_DUPLICATE` / HTTP 409 | 동일한 활성 기억 존재 | 기존 기억을 수정하거나 비활성화 후 생성 |
| HTTP 404 `memory not found` | 없는 ID 조회/수정/삭제 | 목록을 새로고침하고 올바른 ID 사용 |
| HTTP 422 | 길이·category·priority 범위 오류 | validation detail 확인 |

### 10.4 Agent/도구 오류 코드

| 코드 | 뜻 | 조치 |
|---|---|---|
| `validation_failed` | argument/schema 검증 실패 | 도구 입력 형식 수정 |
| `permission_denied` | 정책상 실행 불가 | 권한 범위 또는 도구 선택 수정 |
| `approval_denied` | 사용자가 거부 | 승인 없이는 재실행하지 않음 |
| `approval_expired` | 승인 제한시간 초과 | 다시 요청하고 시간 내 결정 |
| `unsupported_tool` | registry에 없는 도구 | 지원 도구 목록 확인 |
| `tool_disabled` | 도구가 비활성 | Agent 관리에서 활성 여부 확인 |
| `client_offline` | 실행 대상 Link가 offline | Link 연결 후 다시 요청 |
| `target_not_found` | 대상 client/session 없음 | 대상 선택 새로고침 |
| `path_not_allowed` | 허용 root 밖 경로 | 승인된 작업 폴더 사용 |
| `sensitive_path` | 민감 경로 접근 | 해당 작업을 수동 수행 |
| `duplicate_request` | 같은 at-most-once 요청 재수신 | 새 요청 ID를 쓰거나 기존 결과 확인 |
| `timed_out` | 실행 제한시간 초과 | 작업 범위 축소 후 재시도 |
| `cancelled` | 사용자/서버 취소 | 필요하면 새 요청 생성 |
| `execution_failed` | 로컬 실행 실패 | 안전 요약과 Link 로그 확인 |
| `result_too_large` | 결과 크기 제한 초과 | 검색/읽기 범위를 줄임 |
| `client_disconnected` | 실행 중 Link 연결 종료 | 재연결 후 결과 상태 확인 |

### 10.5 HTTP와 WebSocket 상태

| 상태 | 뜻 |
|---:|---|
| HTTP 200/201/204 | 조회 성공 / 생성 성공 / 삭제 성공 |
| HTTP 400 | pairing code 만료 또는 불일치 |
| HTTP 401 | 인증 없음 또는 token 불일치 |
| HTTP 403 | 관리자 권한 없음, local-code 원격 조회, 공인망 pairing 시도 |
| HTTP 404 | section, revision, memory 등 대상 없음 |
| HTTP 409 | Persona 저장 충돌 또는 기억 중복 |
| HTTP 422 | Pydantic/API 설정 검증 실패 |
| HTTP 500 | Persona 복구 포함 내부 저장 실패 |
| WS 4400 | Agent protocol 요청 형식 오류 |
| WS 4401 | WebSocket 인증 실패 |
| WS 4403 | Agent 기능 비활성 또는 금지 |

처리되지 않은 서버 예외는 표준 HTTP 500이 될 수 있으며 통일된 `ErrorEnvelope` 형식을
보장하지 않는다.

### 10.6 네트워크 진단 코드

| 코드/reason | 의미 | 조치 |
|---|---|---|
| `no_usable_ipv4` | 조건을 통과한 LAN IPv4가 없음 | 유선/Wi-Fi, DHCP 주소와 adapter 상태 확인 |
| `collector_failed: ...` | PowerShell adapter 수집 또는 JSON 해석 실패 | PowerShell과 `Get-Net*` cmdlet 실행 상태 확인 |
| `adapter_not_up`, `adapter_not_connected` | 연결되지 않은 adapter 제외 | 실제 연결 adapter 활성화 |
| `link_local`, `loopback`, `multicast`, `reserved_or_broadcast` | 광고할 수 없는 주소 제외 | 사설 LAN의 정상 IPv4 사용 |
| `virtual_adapter`, `not_physical_lan`, `vpn_not_allowed` | 가상/VPN adapter 제외 | 물리 Ethernet/Wi-Fi 사용 또는 명시적 override |

`eligible`, `selected_ethernet_default_route` 같은 값은 오류가 아니라 선택 이유다.

### 10.7 실행 파일 종료 코드

| 코드 | 발생 위치 | 의미 |
|---:|---|---|
| `0` | 모든 실행기 | 성공 또는 정상 종료 |
| `1` | CMD/PowerShell/build | 일반 실행·build·update 실패 |
| `2` | Core/network/launcher | 설정 오류, advertised 주소 감지 실패, 잘못된 설치 root |
| `3` | EXE launcher | 예상하지 못한 launcher 내부 오류 |
| `130` | EXE launcher | Ctrl+C 뒤 child가 제한시간 안에 종료되지 않음 |

`Nivelle-Core.exe --network-diagnostics`는 실제 advertised host를 찾으면 0,
찾지 못하거나 네트워크 설정이 잘못되면 2를 반환한다.

EXE → PowerShell → Python 체인은 일반적으로 가장 안쪽 child 종료 코드를 전달한다.
반면 `Nivelle-Core.cmd`, `Nivelle-Link.cmd`, `Nivelle-Local.cmd`는 비정상 child 코드를
최종 `1`로 평탄화한다. `Nivelle-Updater.exe`는 업데이트 콘솔을 성공적으로 띄우면
즉시 0을 반환하므로, 실제 패치 성공 여부는 새 콘솔의 마지막 결과로 판단한다.

### 10.8 자주 보는 실행 오류

| 메시지/현상 | 원인 | 해결 |
|---|---|---|
| `did not find executable ... python.exe` | 다른 PC에서 복사한 venv의 절대경로 | 최신 launcher로 실행해 `.venv` 재생성 |
| `llama-server 실행 파일을 찾을 수 없습니다` | runtime 이동/삭제 또는 잘못된 path | 자동 설치 재실행, models 설정 수정 |
| `Qwen 모델 파일을 찾을 수 없습니다` | GGUF 누락/경로 오류 | runtime 설치 재실행, 여유 공간 확인 |
| `10분 안에 ... 모델이 준비되지 않았습니다` | 로드 실패/VRAM 부족/Provider crash | llama 콘솔, GPU layers와 RAM 확인 |
| `30초 안에 Nivelle Core가 준비되지 않았습니다` | bind/DB/startup 실패 | Core 콘솔과 port 점유 확인 |
| `Gateway endpoint points to localhost` | remote Core인데 Link에 127.0.0.1 입력 | Core의 LAN advertised IPv4 입력 |
| 연결 시간 초과 | stale IP, Core 꺼짐, firewall | 진단 주소, TCP 8765, subnet 확인 |
| 같은 답변이 두 번 표시 | 구 Link 실행/중복 event 처리 | 최신 `Nivelle-Link.exe`만 실행, 버전 확인 |
| 새 질문에 이전 답변 표시 | 구 client buffer/request ID 재사용 | 최신 client로 업데이트하고 프로세스 완전 종료 후 재실행 |
| 업데이트 hash 불일치 | ZIP/sidecar 손상 또는 잘못된 자산 | 두 파일 삭제 후 stable release에서 다시 다운로드 |
| 업데이트 base hash 불일치 | 설치 앱 파일이 수동 변경됨 | portable 새 설치 또는 변경분 별도 백업 후 적용 |
| `Nivelle가 실행 중입니다` | Core/Link/llama/lock 사용 중 | 관련 프로세스 종료 후 재실행 |

### 10.9 업데이트·롤백 오류 식별자

업데이트 계열은 세부 숫자 코드 대신 실패 시 `1`과 메시지 prefix를 사용한다.

| prefix | 대표 원인 | 우선 조치 |
|---|---|---|
| `[Nivelle 온라인 업데이트]` | GitHub/API/proxy, stable asset 한 쌍 누락, 크기·SHA 불일치 | 인터넷과 ZIP/`.sha256` 이름 확인 후 재다운로드 |
| `[Nivelle 업데이트]` | 버전·manifest·base hash 불일치, 실행 중 process, 공간/백업/적용 실패 | 모든 process 종료, 정확한 시작 버전 package 사용 |
| `[Nivelle 롤백]` | 백업 없음/손상, 버전·설치 root 불일치, 수동 변경 충돌 | 같은 설치본의 최신 성공 backup과 충돌 파일 확인 |

Updater 작업 상태는 `%LOCALAPPDATA%\Nivelle\Updater` 아래 `downloads`, `staging`,
`backups`, `rollback-temp`에 나뉘어 저장된다.

---

## 11. 개발과 빌드

### 11.1 개발 환경

```powershell
Set-Location D:\Nozomi
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\setup_dev.ps1
```

### 11.2 소스 실행

```powershell
# Core
.\scripts\run_server.ps1

# Link
.\scripts\run_client.ps1

# 통합
.\.venv\Scripts\python.exe .\nivelle.py
```

### 11.3 테스트

```powershell
.\.venv\Scripts\python.exe -m pytest -q
ruff check --config pyproject.toml .
python -m mypy --python-executable .\.venv\Scripts\python.exe `
  packages\nivelle_protocol apps\server\nivelle_core
```

PowerShell 구문 검사:

```powershell
$errors = $null
[void][Management.Automation.Language.Parser]::ParseFile(
  (Resolve-Path '.\scripts\run_locked.ps1'), [ref]$null, [ref]$errors
)
$errors
```

### 11.4 EXE 생성

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\build_executables.ps1 `
  -ProjectRoot D:\Nozomi `
  -OutputRoot D:\Nozomi
```

결과:

- `Nivelle-Core.exe`
- `Nivelle-Link.exe`
- `Nivelle-Local.exe`
- `Nivelle-Updater.exe`

빌더는 PE machine이 x64(`0x8664`)인지 확인하고 각 EXE를 `--smoke-test`로 실행한다.

### 11.5 portable ZIP

```powershell
.\scripts\build_portable.ps1 -Force
```

결과:

- `dist\Nivelle-Windows-x64-<version>.zip`
- 같은 이름의 `.sha256`

model, runtime, venv, DB, log, secret, cache는 portable에 포함하지 않는다.

### 11.6 update ZIP

```powershell
.\scripts\build_update.ps1 `
  -BasePath .\dist\Nivelle-Windows-x64-<old>.zip `
  -FromVersion <old> `
  -ToVersion <new> `
  -Force
```

manifest에는 changed/new file의 size, SHA-256, base SHA-256과 삭제 파일의 base hash가
들어간다. 사용자가 수동 수정한 기존 앱 파일은 덮어쓰지 않고 중단한다.

실제 검증:

```powershell
.\scripts\verify_update.ps1 `
  -BasePath .\dist\<old-portable>.zip `
  -UpdatePath .\dist\<update>.zip `
  -ProjectRoot D:\Nozomi
```

성공 기준은 `REAL_PORTABLE_APPLY_ROLLBACK_OK`다.

---

## 12. 업데이트와 롤백 사용법

### 12.1 온라인 업데이트

1. Core, Link, Local, llama-server를 종료한다.
2. `Nivelle-Updater.exe` 또는 `Nivelle-Update-Online.cmd`를 실행한다.
3. stable GitHub release의 version과 protocol을 확인한다.
4. 현재 버전에 정확히 맞는 ZIP과 `.sha256` 한 쌍을 받는다.
5. SHA-256과 내부 manifest를 검증한다.
6. 기존 앱 파일 hash를 확인한다.
7. 변경될 파일만 `%LOCALAPPDATA%\Nivelle\Updater\backups`에 백업한다.
8. staging에서 검증 후 설치 root에 반영한다.

draft와 prerelease는 자동 업데이트 대상이 아니다.

### 12.2 로컬 패치

업데이트 ZIP을 설치 폴더 또는 지정 경로에 두고 `Nivelle-Update.cmd`를 실행한다.
0.3.1에서 0.4.0으로 올리는 전환 패치만 구 updater가 찾을 수 있도록
`Nozomi-Update-0.3.1-to-0.4.0.zip` 이름을 유지한다.

### 12.3 롤백

```powershell
.\Nivelle-Rollback.cmd
```

백업 metadata의 product/version/hash와 설치 경계를 확인한 뒤 복원한다. DB와 사용자
설정은 업데이트 payload가 아니므로 일반 코드 롤백에서 덮어쓰지 않는다.

---

## 13. 구 Nozomi 파일 처리 기준

`Nozomi/nozomi` 문자열이 남았다는 이유만으로 모두 삭제하면 안 된다. 현재 0.4.0은
0.3.1 사용자의 데이터와 바로가기, keyring token, 업데이트 경로를 한 번 이전하는
transition release다.

### 13.1 유지해야 하는 호환 항목

| 항목 | 유지 이유 |
|---|---|
| `Nozomi-*.cmd`, `Nozomi *.cmd` | 기존 바로가기와 사용자 습관이 0.4.0으로 연결되게 함 |
| `nozomi.py`, `nozomi_runtime.py` | 구 Python 진입점/import가 새 구현으로 위임 |
| `apps/*/nozomi_*`, `packages/nozomi_protocol` | 설치된 0.3.x extension/import의 1회 호환 |
| `scripts/nozomi_executable_launcher.py` | 구 build/launcher entry를 새 실행기로 전달 |
| `scripts/backup_nozomi_data.ps1` | 기존 백업 명령을 새 백업기로 전달 |
| `NOZOMI_*` fallback | 구 환경변수로 지정된 데이터·build 경로 migration |
| `%LOCALAPPDATA%\Nozomi\...` 검사 | 기존 DB, 설정, updater backup 발견·이전 |
| `NozomiClient` keyring fallback | 기존 인증 token을 잃지 않고 Nivelle service로 이전 |
| `.nozomi` lock | 구 실행기와 새 실행기의 동시 실행 방지 |
| `nozomi.db` fallback | 기존 DB를 `nivelle.db`로 이동하기 전 발견 |
| `Nozomi-Update-0.3.1-to-0.4.0.zip` | 구 0.3.1 online updater가 인식하는 전환 asset 이름 |
| GitHub repository slug `nozomi_ai` | 실제 원격 저장소 주소이며 별도 repository rename 없이는 변경 불가 |

이 항목들은 활성 UI 이름이 아니다. 새 UI, 창 제목, 기본 Persona, 로그 component와
신규 배포 파일 이름에는 Nivelle만 사용한다.

### 13.2 제거할 수 있는 항목

다음 조건을 모두 만족한 generated artifact는 삭제할 수 있다.

- Git에 tracked되지 않음
- 현재 build/test/update 입력이 아님
- 실행 중 프로세스가 사용하지 않음
- `dist`, 임시 추출 폴더 또는 cache에서 다시 만들 수 있음
- 0.3.1→0.4.0 검증에 필요한 base/bridge 자산이 아님

2026-08-12 감사에서는 아래 비추적·무시 대상만 제거했다.

| 제거 항목 | 성격 |
|---|---|
| `.tmp.nv-s-23279602` | 이전 portability 검증이 만든 임시 가상환경 |
| `Nozomi-Windows-x64-0.2.1` | 0.2.1 portable 압축 해제본 |
| `Nozomi-Update-0.1.0-to-0.2.1` | 구 update 압축 해제본 |
| `build/version-wheel-final-20260803211435` | 구 wheel build 임시 출력 |
| `build/version-wheel-test-20260803204756141` | 구 wheel test 임시 출력 |
| `dist`의 과거 Nozomi ZIP/sidecar 35개 | 0.1.x~0.3.0 및 timestamp portable 산출물 |

총 `5,352,269,367` bytes, 약 `4.985 GiB`를 정리했다. 이 파일들은 Git에 없으며
필요하면 이전 소스/Release에서 다시 build 또는 압축 해제해야 한다. 휴지통으로 옮긴 것이
아니므로 현재 작업 폴더에서 즉시 복구할 수는 없다. Git tracked source, LocalAppData의
사용자 DB·설정·토큰, `.git_disabled`는 삭제하지 않았다.

다음 네 파일은 실제 0.3.1 bridge 검증과 구 updater 호환을 위해 의도적으로 유지했다.

- `dist/Nozomi-Windows-x64-0.3.1.zip`
- `dist/Nozomi-Windows-x64-0.3.1.zip.sha256`
- `dist/Nozomi-Update-0.3.1-to-0.4.0.zip`
- `dist/Nozomi-Update-0.3.1-to-0.4.0.zip.sha256`

### 13.3 호환 코드를 완전히 제거할 시점

0.4.0 stable이 배포되고 0.3.1 직접 업데이트 지원 기간이 끝난 다음 release에서 별도
breaking migration으로 제거한다. 그때는 다음을 동시에 해야 한다.

1. 구 CMD/Python/import shim 삭제
2. `pyproject.toml`의 legacy package 제거
3. `NOZOMI_*`, 구 AppData/keyring/DB fallback 제거
4. `.nozomi` 이중 lock 제거
5. updater process/backup product compatibility 정리
6. 0.3.1 bridge asset 지원 종료 공지
7. migration·update·rollback 회귀 테스트 갱신

부분적으로 먼저 지우면 기존 사용자의 실행·업데이트·인증·데이터 복구 중 한 경로가
깨질 수 있다.

---

## 14. 운영 점검 순서

### Core PC

```powershell
.\Nivelle-Core.exe --network-diagnostics
.\scripts\check_environment.ps1
```

확인할 것:

- bind가 `0.0.0.0:8765`인지
- advertised 주소가 실제 Ethernet IPv4인지
- Provider가 `127.0.0.1:8080`인지
- APIPA/가상/VPN이 제외되었는지
- 디스크 여유가 모델 24GB와 임시 다운로드를 감당하는지
- 방화벽이 의도한 network profile에서 TCP 8765만 허용하는지

### Link PC

- 저장 주소가 Core의 현재 advertised IPv4인지
- `127.0.0.1` 또는 `0.0.0.0`을 remote 주소로 쓰지 않았는지
- Core와 protocol major가 같은지
- 오래된 `Nozomi-Client.exe`나 0.3.1 Link가 동시에 실행 중이지 않은지
- 대화 완료 메시지가 `message_id`당 한 번만 보이는지

### 업데이트 전

- Core, Link, Local, llama-server 종료
- 중요한 Core 데이터 수동 백업
- ZIP과 `.sha256` 파일 이름이 정확히 일치
- base installation 앱 파일을 수동 수정하지 않았는지 확인
- update apply/rollback/reapply acceptance 결과 보관

---

## 15. 빠른 명령 모음

```powershell
# 환경 준비
.\scripts\setup_dev.ps1

# 실행
.\Nivelle-Core.exe
.\Nivelle-Link.exe
.\Nivelle-Local.exe

# 네트워크 진단
.\Nivelle-Core.exe --network-diagnostics

# 테스트
.\.venv\Scripts\python.exe -m pytest -q

# 백업
.\scripts\backup_nivelle_data.ps1

# 실행 파일과 portable 생성
.\scripts\build_executables.ps1
.\scripts\build_portable.ps1 -Force

# 온라인 업데이트 / 롤백
.\Nivelle-Updater.exe
.\Nivelle-Rollback.cmd
```

---

## 16. 문서 작업 기록

- 작성일: 2026-08-12
- 기준 버전: 0.4.0
- 기준 PC에서 검증된 advertised IPv4: `192.168.219.100`
- Gateway/Provider 포트: `8765 / 8080`
- 추적 중인 구 Nozomi 이름 파일 18개는 0.3.1→0.4.0 호환 bridge임을 import,
  build/update 참조와 회귀 테스트로 확인해 유지했다.
- 비추적 generated artifact 5개 디렉터리와 과거 `dist` 파일 35개를 제거했다.
- 제거량: `5,352,269,367` bytes (`4.985 GiB`).
- 유지한 0.3.1 base/bridge ZIP 두 쌍은 sidecar SHA-256 일치를 다시 확인했다.
- 사용자 DB·설정·Persona·token, `%LOCALAPPDATA%\Nozomi`, `.nozomi` 호환 lock,
  `.git_disabled`에는 손대지 않았다.
