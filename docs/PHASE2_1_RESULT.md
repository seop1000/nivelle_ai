# Nivelle Phase 2.1 Implementation Result (historical 0.3.1 baseline)

> This report records the pre-rename 0.3.1 release. Former package, executable, workspace,
> release-asset, and product identifiers below are historical evidence, not active branding.

> 문서 상태: **로컬 검증·GitHub v0.3.1 게시·live 2PC 배포 확인 완료 / backup 증거·수동 reconnect 관찰 대기**
>
> 판정 기준 시각은 2026-08-03 KST이다. 각 필수 검증은 `passed`, `failed`, `skipped — 이유`로 개별 기록하며, 건너뛴 live 검증을 성공으로 간주하지 않는다.

## 1. Summary

현재 작업 트리는 Phase 2.1의 서버, 공유 프로토콜, 클라이언트, SQLite 마이그레이션, 운영 스크립트 및 테스트를 함께 수정한다. 구현된 주요 변경은 질문 관련성이 우선인 SQLite 하이브리드 기억 검색, 한국어 부분 문자열 보완 검색, 중복 기억 방지 및 이력, `assistant.context` 관찰 이벤트, 단일 WebSocket 기반 자동 재연결, 오프라인 읽기 전용 UI, 메시지 멱등성·중단 복구, 세분화된 서버 상태와 생성 지표, 그리고 `VERSION` 기반 0.3.1 버전 일원화이다.

벡터 검색은 구현됐다고 주장하지 않는다. Qwen3-Embedding과 `sqlite-vec`는 명시적으로 `unavailable/not_configured` 상태이며, Phase 2.1 검색 백엔드는 `sqlite_hybrid`이다.

로컬 0.3.1 소스, 자동화 180개, 실제 Qwen3.5-9B 8문항, EXE/portable 및 0.2.1·0.3.0 업데이트 경로를 검증하고 GitHub v0.3.1을 게시했다. 이후 실제 server PC도 0.3.1로 업데이트되었고, health, typed status, Qwen, SQLite hybrid search, 기억 수 및 최신 대화 무중복을 읽기 전용으로 확인했다. 사전 backup 파일의 실제 경로/hash와 client의 reconnecting→online 화면 전이는 현재 PC에서 증명할 수 없어 별도 수동 확인으로 남는다.

## 2. Root causes found

1. 기존 프롬프트 경로가 현재 질문을 검색하지 않고 활성 기억을 우선순위와 수정 시각으로 정렬한 고정 상위 항목만 주입했다.
2. FTS5 `unicode61` 토큰 경계만으로는 한국어 조사·어미가 붙은 문자열의 부분 검색을 안정적으로 처리할 수 없었다.
3. 기억 본문에 정규화 키와 데이터베이스 유일성 제약이 없어 동일한 기억이 다른 UUID로 만들어질 수 있었다.
4. 기억 수정 이력과 supersession 관계가 없어 과거 값이 현재 값과 함께 검색될 위험을 명확히 통제하기 어려웠다.
5. WebSocket 이벤트가 검색 후보, 점수, 선택 여부와 제외 이유를 클라이언트에 제공하지 않았다.
6. Persona 자연어 규칙만으로 응답 길이와 기술 답변 형식을 안정적으로 제어하지 못했고, 프로젝트 용어·현재 런타임 사실·사설망 정책이 별도 계층으로 구성되지 않았다.
7. 재연결용 backoff 로직이 실제 채팅 WebSocket 수명주기와 연결되지 않았고, 채팅 요청마다 여는 임시 소켓 대신 애플리케이션이 소유하는 authoritative socket 규칙이 필요했다.
8. 오프라인 상태가 저장 버튼의 표시와 실제 저장 핸들러 양쪽에 일관되게 적용되지 않았다.
9. `client_message_id`가 영속적으로 유일하지 않았으며 user/assistant 턴 할당, 제어 재시도, 서버 비정상 종료 복구가 하나의 내구성 규칙으로 연결되지 않았다.
10. 서버 상태가 Gateway, llama.cpp, 기억 DB, embedding을 명확히 분리하지 않았고 llama.cpp usage/timing 정보를 결과 이벤트와 관리 화면에 전달하지 않았다.
11. 애플리케이션 버전이 여러 파일과 이전 설치본에 분산되어 로컬 소스와 실제 서버 설치본이 다르게 보고될 수 있었다.
12. Updater의 실행 위치와 실제 설치 루트가 일치하는지에 대한 회귀 검증이 부족했다.

## 3. Files changed

아래는 결과 문서 작성 시점의 작업 트리를 기능별로 묶은 목록이다. 최종 커밋에서는 `git diff --name-only`로 다시 확인해야 한다.

| 영역 | 파일 |
|---|---|
| 버전·공유 프로토콜 | `VERSION`, `pyproject.toml`, `packages/nozomi_protocol/version.py`, `chat.py`, `memory.py`, `server_status.py`, `settings.py`, `__init__.py` |
| 서버 | `apps/server/nozomi_server/app.py`, `backend_status.py`, `database.py`, `llm.py`, `main.py`, `memory_api.py`, `memory_repository.py`, `memory_retriever.py`, `persona.py`, `repositories.py` |
| 클라이언트 | `apps/client/nozomi_client/app.py`, `main.py`, `network.py`, `windows.py` |
| 설정·빌드·업데이트 | `config/examples/memory.yaml`, `config/examples/models.yaml`, `scripts/build_portable.py`, `build_update.ps1`, `nozomi_executable_launcher.py` |
| 운영·smoke 스크립트 | `scripts/audit_runtime_memories.py`, `backup_nozomi_data.ps1`, `test_server_health.ps1`, `test_client_server_connection.ps1`, `test_reconnect.ps1`, `smoke_phase21_real_model.py` |
| 자동화 테스트 | `tests/unit/test_memory_retriever.py`, `test_database_migrations.py`, `test_conversation_repository.py`, `test_memory_operational_scripts.py`, `test_server_status.py`와 기존 단위 테스트 보강, `tests/integration/test_chat_phase21.py`, API·대화 기록 통합 테스트 보강 |
| 문서 | `docs/PHASE2_1_IMPLEMENTATION_PLAN.md`, `PHASE2_1_TEST_PLAN.md`, 이 결과 문서, `MEMORY_RETRIEVAL.md`, `RECONNECT_STATE_MACHINE.md`, `PROTOCOL_EVENTS.md`, `DATABASE_MIGRATIONS.md`, `ONLINE_UPDATES.md`, `CHANGELOG.md` |

## 4. Database migrations

마이그레이션은 `schema_versions`에 순차 기록하며 기존 UUID를 유지한다.

| 버전 | 변경 |
|---|---|
| v4 | `normalized_content`, supersession 필드, `memory_revisions`, 활성 기억 부분 유일 인덱스, 기존 정확 중복의 비파괴적 정리 및 FTS 재구축 |
| v5 | `messages.client_message_id`와 부분 유일 인덱스 |
| v6 | `messages.retry_of_client_message_id`와 원본당 재시도 자식 하나만 허용하는 부분 유일 인덱스 |

기존 DB를 올리기 전 SQLite online backup을 생성하고 `PRAGMA integrity_check = ok` 및 0바이트가 아님을 확인한다. v4 중복 정리는 사용자의 행을 삭제하지 않고 한 행을 canonical record로 선택한 뒤 나머지를 inactive/superseded 상태로 보존한다. 일반 기억 수정은 같은 ID를 유지하고 `memory_revisions`에 이전/새 내용을 기록하며 FTS의 과거 본문을 제거한다.

user 메시지와 assistant `generating` placeholder는 하나의 `BEGIN IMMEDIATE` 트랜잭션으로 할당한다. 시작 시 남은 `generating` assistant와 legacy orphan user 턴은 `interrupted`로 복구한다. terminal 상태 갱신은 현재 상태가 `generating`일 때만 허용해, 완료 커밋 뒤 늦게 도착한 disconnect/cancel이 `completed`를 되돌리지 못한다.

현재 삭제 API는 기존 계약을 유지한 hard delete이다. 그러므로 관리자용 `include_deleted` 목록은 제공하지 않으며 삭제된 행은 일반 목록, FTS, 검색, 프롬프트에서 제외된다. 이전 schema 사본을 이용한 online backup, 무결성 검사, v4/v5/v6 순차 migration, UUID·상태·시각 보존, 중복 repair 및 재시작 검증은 자동화 suite에서 통과했다. 실제 server PC DB에는 아직 이 migration을 적용하지 않았다.

## 5. Memory retrieval implementation

`MemoryRetriever.retrieve()`가 현재 사용자 질문을 가장 먼저 정규화하고, 설정된 수만큼의 최근 사용자 메시지를 낮은 가중치의 보조 문맥으로 사용한다. 활성·비-superseded 기억을 기본 후보로 검색하고, 명시적으로 첨부된 기억은 별도 경로로 가져온다. inactive 디버그 후보는 별도 제한을 사용하므로 활성 후보의 `candidate_limit`을 소모하지 않는다.

후보 점수는 다음 식을 사용한다.

```text
final_score = relevance_score * 0.70
            + priority_score  * 0.20
            + recency_score   * 0.10
```

현재 질문 관련성이 `minimum_relevance`보다 낮으면 기억 우선순위가 높아도 제외된다. exact phrase, substring, prefix boost를 적용하며 Pydantic은 세 가중치 합계가 1.0인지와 `candidate_limit >= top_k`인지 검사한다. 기본값은 `config/examples/memory.yaml`에 있으며 `top_k=5`, `candidate_limit=30`, `minimum_relevance=0.12`, 최근 사용자 메시지 2개이다.

동일 정규화 본문은 한 번만 선택한다. 제한적인 단일 사실 형식에서만 충돌 키를 만들고, 명시 첨부 여부, 최신 `updated_at`, 관련성, 우선순위, ID 순으로 결정한다. 선택·제외 결과는 `selected`, `explicitly_attached`, `inactive`, `deleted`, `superseded`, `duplicate`, `low_relevance`, `top_k_limit`, `conflict_lost` 등의 이유와 함께 반환한다. 실제 프롬프트에는 `included=true`인 항목만 들어간다.

## 6. Korean search implementation

검색은 Unicode NFKC, case folding, 구두점·공백 정규화 뒤 다음 후보를 병합하고 ID 중복을 제거한다.

1. 정규화한 exact phrase 검색
2. SQLite 빌드가 지원할 때만 feature-detected FTS5 `trigram`
3. 기존 FTS5 `unicode61` prefix 검색
4. 최대 20개 검색어에 한정한 `normalized_content LIKE` substring fallback
5. 결과 설명을 위한 제한된 high-priority backfill

한국어 형태소 분석기를 사용하거나 사용한다고 표시하지 않는다. 프로젝트에서 필요한 작은 동의어 그룹만 사용한다. `히냥이` 부분 검색과 서버 기억에 대한 `서버 메모리`, `메모리 배분`, `RAM 16GB`, `GPU 8GB`, `시스템 램`, `Ally X 메모리` 검색을 자동화 테스트에서 검증했다. 서버/클라이언트처럼 공유 하드웨어 용어가 많은 기억은 질문이 한쪽 PC를 명시하면 반대쪽 전용 기억의 관련도를 낮춰 혼입을 막는다.

## 7. Context observability

성공 이벤트 순서는 다음과 같다.

```text
chat.accepted
assistant.context
assistant.delta (0회 이상)
assistant.completed
```

`assistant.context`는 첫 delta보다 먼저 전송되며 `request_id`, `conversation_id`, `user_message_id`, `assistant_message_id`, `client_message_id`를 연계한다. 검색 backend, top-k, 후보 수와 함께 각 기억의 ID, 안전한 요약, category, priority, relevance/priority/recency/final score, 포함 여부 및 사유를 제공한다. 자격 증명처럼 보이는 내용은 요약에서 가리고 토큰·Authorization header·pairing code는 이벤트와 로그에 넣지 않는다.

클라이언트는 canonical `assistant.context`와 이전 `chat.context`를 모두 파싱하고, 대화 정보 창에서 선택·제외 기억과 backend를 표시한다. 서버는 중복 UI 갱신을 피하기 위해 canonical 이벤트만 보낸다. 자동화와 실제 모델 smoke 모두 context가 첫 delta보다 먼저 왔고, context JSON에 token, Authorization header, pairing code 같은 비밀 필드가 없음을 확인했다.

## 8. Reconnect state machine

클라이언트 애플리케이션 하나가 `ConnectionManager`, health monitor, reconnect task, send task와 authoritative chat WebSocket 하나를 소유한다. 관리 창은 별도 소켓을 만들지 않는다. 상태는 `DISCONNECTED`, `CONNECTING`, `AUTHENTICATING`, `CONNECTED`, `RECONNECT_WAIT`, `FAILED`, `MANUAL_OFFLINE`으로 구분한다.

예기치 않은 종료는 두 번의 연속 health 실패 뒤 1, 2, 4, 8, 16, 최대 30초의 backoff와 제한된 jitter로 재연결한다. `/health` 한 번의 성공만으로 retry counter를 초기화하지 않는다. 인증, typed status, authoritative WebSocket까지 성공한 `mark_connected()` 뒤에만 초기화한다. 수동 오프라인은 자동 재연결을 막고 애플리케이션 종료는 소유한 task를 취소·await한 뒤 소켓을 닫는다.

불확실한 in-flight 메시지는 자동 재전송하지 않는다. 같은 `client_message_id`는 DB 유일 인덱스로 중복 저장·생성을 막고, 사용자가 제어 재시도를 선택하면 새 ID와 `retry_of_client_message_id`를 사용한다. 중단된 동일 대화 요청만 재시도할 수 있고 원본당 자식 하나만 허용한다. 원시 WebSocket을 실제로 닫았을 때 assistant가 `interrupted`로 남는 loopback Uvicorn 통합 테스트를 포함한 회귀 suite가 통과했다. live server stop/restart 관찰은 수행하지 않았다.

## 9. Offline UI behavior

연결이 끊겨도 이미 불러온 Persona, 기억, 서버 상태, 대화 내용과 편집 중인 채팅 초안은 화면에 남는다. Persona 저장, 기억 생성·수정·삭제, 서버 설정 저장·rollback처럼 서버를 바꾸는 동작은 위젯과 애플리케이션 핸들러 양쪽에서 차단한다. 검색 화면의 `비활성 포함`은 명시적 library query인 `include_inactive=true`만 만들며 일반 채팅 검색 정책을 바꾸지 않는다.

연결이 완전히 복구된 뒤 변경 동작이 다시 활성화되고 창 singleton은 유지된다. headless Qt 테스트에서 offline/reconnecting/online 표시, mutation 차단·복구, 마지막 데이터·초안 유지, context 및 상세 상태 표시, singleton 창을 모두 검증했다.

## 10. Runtime and token metrics

`/health`는 인증 없이 사용하는 가벼운 생존 확인으로 유지하고, `/api/v1/status`는 인증된 상세 상태를 반환한다. 상세 상태는 Gateway, llama.cpp LLM, SQLite memory DB, embedding을 서로 다른 component로 표현한다. LLM은 configured model과 실제 `/v1/models`에서 확인한 loaded model을 구분하고 engine 및 quantization을 표시한다. 구성값만으로 모델을 loaded라고 표시하지 않는다.

기억 상태는 backend `sqlite`, search backend `sqlite_hybrid`, active/inactive count를 제공한다. embedding은 실제 구현이 없으므로 `unavailable`, provider `null`, reason `not_configured`이다. llama.cpp가 제공하는 경우 `prompt_tokens`, `completion_tokens`, `total_tokens`, `tokens_per_second`, `first_token_latency_ms`, `total_latency_ms`, `finish_reason`, `interrupted`, `model`, `request_id`를 `assistant.completed`와 마지막 요청 상태에 전달한다. backend가 제공하지 않는 값은 추정하지 않고 `null`로 둔다. typed status 단위 테스트와 실제 Qwen smoke에서 usage, throughput, first-token/total latency 및 request ID가 기록됨을 확인했다.

## 11. Version synchronization

저장소의 애플리케이션 버전 원본은 ASCII `VERSION` 한 파일이며 현재 값은 `0.3.1`이다. `pyproject.toml`은 dynamic version을 사용하고, 공유 protocol 패키지는 설치본에 포함된 `VERSION`, 저장소 root의 `VERSION`, 설치 패키지 metadata 순으로 읽는다. protocol version은 `1.0`이며 major가 같으면 호환, minor/patch 차이는 경고, major 차이 또는 잘못된 형식은 거부한다.

서버와 클라이언트 시작 기록 및 상태는 component, app/protocol version, 가능할 때 build commit/time, 실제 executable path, frozen 여부를 제공한다. 환경에 build metadata가 없으면 값을 추정하지 않고 `null`로 둔다. `Nozomi-Updater.exe`는 현재 작업 디렉터리가 아니라 실행 파일의 부모를 설치 root로 전달한다.

소스, wheel metadata, portable의 `VERSION`은 모두 0.3.1로 일치한다. 배포 후 인증된 live status도 `app_version=0.3.1`, `protocol_version=1.0`, component `nozomi-server`를 보고했다. 실행 경로는 기존 in-place 설치 폴더의 `Nozomi-Server.exe`이며 폴더 이름에 이전 버전 문자열이 남아 있어도 실행 중인 애플리케이션 버전은 0.3.1이다. live version synchronization은 passed이다.

## 12. Automated test results

최종 검증은 실제 모델 smoke가 연 임시 llama 프로세스가 완전히 종료된 뒤 최신 트리에서 다시 시작했다. 검증 시작과 종료 시 계산한 릴리스 대상 98개 파일 목록·내용이 같았다. 확정된 pytest summary는 `180 passed, 1 warning in 98.94s`이며 failed 또는 skipped 항목은 없었다. 경고는 Starlette TestClient의 httpx 호환 경고 1건이다.

| 검증 | 결과 | 판정 |
|---|---|---|
| 전체 pytest | `180 passed, 1 warning in 98.94s`; failed/skipped 0 | `passed` |
| Ruff | 저장소 전체 `ruff check .` 통과 | `passed` |
| mypy (`packages`, server, client) | `Success: no issues found in 26 source files` | `passed` |
| migration/backup/audit 집중 테스트 | 전체 pytest에 관련 단위 테스트가 포함되어 통과 | `passed` |
| headless Qt | 전체 pytest에 client/Qt 단위 테스트가 포함되어 통과 | `passed` |
| 로컬 FastAPI/WebSocket 통합 | mock provider 및 real loopback transport 통합 테스트 통과 | `passed` |
| PowerShell 스크립트 parser | `scripts/*.ps1` 18개 모두 parse error 없음 | `passed` |
| wheel version/package 검사 | `nozomi_ai-0.3.1-py3-none-any.whl`; metadata와 bundled `VERSION` 모두 0.3.1 | `passed` |
| EXE/portable/update | 4개 EXE, 0.3.1 portable, 0.2.1/0.3.0→0.3.1 patch와 SHA-256 sidecar 생성 | `passed` |
| whitespace/patch 검사 | `git diff --check` 오류 없음; checkout CRLF 변환 경고만 존재 | `passed` |

### Required final validation checklist

| # | 필수 검증 항목 | 상태 | 근거 |
|---:|---|---|---|
| 1 | 기존 테스트 전체 실행 | `passed` | 전체 suite: `180 passed, 1 warning in 98.94s` |
| 2 | 새 테스트 전체 실행 | `passed` | 새 memory/reconnect/durability/status/Qt/integration 테스트가 전체 suite에 포함됨 |
| 3 | Ruff 실행 | `passed` | 저장소 전체 검사 통과 |
| 4 | 저장소가 사용하는 범위의 mypy 실행 | `passed` | `Success: no issues found in 26 source files` |
| 5 | 마이그레이션 테스트 | `passed` | 이전 schema backup, v4/v5/v6, duplicate repair, ID 보존 테스트가 전체 suite에서 통과 |
| 6 | headless Qt 테스트 | `passed` | client 관리 창, 상태, draft, offline mutation 테스트가 전체 suite에서 통과 |
| 7 | 로컬 client/server 통합 테스트 | `passed` | FastAPI TestClient 및 real loopback Uvicorn/WebSocket 종료 테스트 통과 |
| 8 | 안전한 환경의 실제 모델 smoke test | `passed` | 실제 Qwen3.5-9B Q4_K_M acceptance 8/8; 섹션 13 |
| 9 | 출력에 인증 토큰이 없음 | `passed` | privacy/startup 테스트, context JSON 및 최종 출력 점검에서 비밀 필드 없음 |
| 10 | 실제 server/client가 같은 버전을 보고 | `passed` | live server, local client/release 모두 app 0.3.1, protocol 1.0 |
| 11 | 활성 server executable path가 올바름 | `passed` | live runtime status가 실제 in-place `Nozomi-Server.exe` 경로와 component를 보고 |
| 12 | 마이그레이션 전 DB backup 존재 | `skipped — 원격 파일 증거 미확인` | 자동 backup/migration 테스트는 passed이나 server PC backup 경로/hash에 접근할 수 없음 |
| 13 | 기존 기억이 모두 보존됨 | `skipped — 사전 ID snapshot 없음` | 실제 baseline과 배포 후 모두 13개(활성 12, 비활성 1), unique ID 13개이나 사전 전체 ID 목록은 기록하지 못함 |
| 14 | 임시 test 기억 정리 | `passed` | smoke는 임시 data directory를 제거했고 live count는 13/12/1로 불변 |
| 15 | 중복 WebSocket 연결이 없음 | `passed` | authoritative-socket 재사용·재연결 자동 테스트 통과; live 관찰은 섹션 14에 별도 기록 |
| 16 | 중복 대화 메시지가 생성되지 않음 | `passed` | 자동화 통과; live 최신 대화도 user 1개/assistant 1개, 모두 completed, client message ID 중복 0 |

## 13. Real-model smoke-test results

자동화 테스트는 실제 모델을 요구하지 않는다. 아래 결과는 실제 Qwen3.5-9B Q4_K_M과 `assistant.context` 이벤트를 함께 관찰한 경우에만 passed로 기록한다. 답변 문자열만 맞고 올바른 기억/런타임 문맥이 선택되지 않았다면 실패이다.

| # | 질문 목적 | 기대 사실/행동 | 사용한 기억·런타임 증거 | 관찰한 답변 | 상태 |
|---:|---|---|---|---|---|
| 1 | 사용자 호칭 | 저장된 사용자 호칭 기억 사용 | 호칭 기억 1개만 selected; context-before-delta | “저는 Nozomi… 당신을 ‘히냥이’라고 부르겠습니다.” | `passed` |
| 2 | 서버 RAM/GPU 배분 | system RAM 16GB, GPU reserved 8GB | 서버 메모리 기억 selected | “시스템 RAM은 16GB… GPU 예약 메모리는 8GB” | `passed` |
| 3 | 클라이언트 PC 사양 | Windows 11 Pro, Ryzen 7 5700X, RAM 32GB, RTX 3060 12GB | 클라이언트 장치 기억 selected; 장치 범위 client 고정 | 네 사양을 모두 답하고 “Nozomi 클라이언트 PC”로 명시 | `passed` |
| 4 | 2PC와 127.0.0.1 | 2PC는 두 물리 PC이며 client는 server의 사설 IPv4 사용 | 프로젝트 구조 기억과 private-network runtime context | 127.0.0.1 불가, 서로 다른 물리 PC와 서버 LAN IP 사용 설명 | `passed` |
| 5 | 현재 연결 서버 | 활성 profile과 현재 server address 사용 | runtime profile `primary`, local, TLS false | 비공개 LAN endpoint, local, TLS 비활성 | `passed` |
| 6 | fallback model | 구성되지 않았으면 없다고 명시 | fallback `null`, selected memory 0개 | “현재 fallback 모델은 null” | `passed` |
| 7 | 외부 접속 | private VPN 우선, public port forwarding 기본 권고 금지 | private-network policy 기억 selected | 사설 VPN 우선, 포트 포워딩은 기본 권고하지 않음 | `passed` |
| 8 | 테마 색상 수정 | purple 기억을 같은 ID의 gray로 바꾼 뒤 gray만 답변 | 수정 뒤 gray 기억 1개만 selected; purple 미포함 | “Nozomi 테스트 강조색은 회색” | `passed` |

최신 전체 실행은 8개 답변의 의미가 모두 맞았고, case 6의 `null` 표현을 smoke checker가 처음 허용하지 않아 자동 집계만 7/8이었다. checker에 동등한 미설정 표현인 `null`을 추가한 뒤 case 6을 재실행해 1/1을 통과했다. 따라서 최종 acceptance matrix는 8/8이다. 모든 문항에서 terminal completion, context-before-delta, 기대 기억, usage metrics 및 secret-field 부재를 함께 확인했다.

- 실제 모델: `Qwen3.5-9B Q4_K_M` (`Qwen_Qwen3.5-9B-Q4_K_M.gguf`)
- 모델 파일: `D:\Nozomi\runtime\models\Qwen_Qwen3.5-9B-Q4_K_M.gguf`, 6,169,341,984 bytes
- 모델 SHA-256: `d784ce9eda1a5a7b51e8f705a9e6310844bf4f173654d115823c775fdea56d43`
- llama.cpp executable: `D:\Nozomi\runtime\llama.cpp\b10231\llama-server.exe`
- 실행 시각: 2026-08-03 21:40~21:46 KST
- 실행 격리: 운영체제가 선택한 일회성 loopback 포트, 임시 server/client data 및 SQLite DB 사용
- 임시 데이터 정리: passed; 종료 뒤 Nozomi runtime llama process 0개, live 기억 count 13/12/1 불변
- 전체 결과: `D:\Nozomi\build\phase21-real-smoke-final7.json`
- checker 보정 재검증: `D:\Nozomi\build\phase21-real-smoke-final8.json` (`1 passed, 0 failed`)

## 14. Live restart-test results

대상은 비공개 server PC endpoint, 저장 profile `primary`, local connection, TLS disabled이다. 사용자가 updater를 실행한 뒤 health, 인증된 status, 기억 검색과 최근 대화를 읽기 전용으로 검사했다. 별도의 test memory나 test conversation은 만들지 않았다. 실제 LAN 주소와 포트는 공개 문서에서 제거했다.

| 단계 | 기대 체크포인트 | 결과 |
|---|---|---|
| 배포 전 backup 및 memory ID 기록 | 무결성 OK, backup 경로와 SHA-256 기록 | `skipped — 원격 backup 경로/hash 및 사전 ID snapshot 미확인` |
| server/client 0.3.1 설치·시작 | app/protocol/executable identity 일치 | `passed`; server/client 0.3.1, protocol 1.0, component와 executable path 확인 |
| 정상 health와 인증 status | Gateway/LLM/memory/embedding 분리 표시 | `passed`; health 200/147.81ms, Gateway running, Qwen ready, memory `sqlite_hybrid`, embedding `unavailable/not_configured` |
| Nozomi Server만 정상 종료 | client가 reconnecting, 초안·창 유지 | `skipped — 전이 화면을 직접 관찰하지 못함`; loopback 자동화는 passed |
| Nozomi Server만 재시작 | 자동 online 복귀, backoff reset | server 재시작 후 screenshot의 `online` 복귀는 확인; backoff reset 전이는 `skipped — 직접 관찰하지 못함` |
| DB 재검사 | 기존 기억 보존, 메시지 중복 없음 | count 13/12/1 및 unique ID 13 유지; 최신 대화 user/assistant 1:1 completed, client ID 중복 0 |

최종 상태: **live server 0.3.1 배포 및 핵심 기능은 passed**이다. 한국어 검색 `히냥이`, `서버 메모리`, `메모리 배분`, `시스템 램`, `RAM 16GB`, `GPU 8GB`가 모두 기대 활성 기억을 반환했다. 남은 수동 증거는 updater 실행 전 backup 파일의 경로/hash, 사전 전체 memory ID snapshot, reconnecting→online 전이와 초안 유지 관찰이다.

## 15. Remaining limitations

- Qwen3-Embedding, `sqlite-vec`, vector/semantic search는 구현하지 않았다. 상태 API는 이를 명시적으로 unavailable로 보고한다.
- 한국어 검색은 NFKC, FTS, prefix, 제한된 substring과 작은 동의어 집합의 결정론적 보완이며 완전한 형태소 분석이 아니다.
- 기억 delete는 hard delete이다. 이전 내용 추적은 update revision에는 적용되지만 삭제된 행을 일반 API로 복원하거나 `include_deleted`로 조회하는 기능은 없다.
- conflict detection은 오탐을 피하기 위해 짧은 단일 사실 패턴에만 보수적으로 적용한다.
- llama.cpp가 usage/timing 또는 GPU 지표를 제공하지 않으면 해당 값은 null/unsupported이다.
- build commit/time은 release build가 환경 metadata를 주입할 때만 채워진다.
- 실제 2PC 서버는 0.3.1로 업데이트됐다. 다만 updater 실행 전 backup 파일의 실제 경로/hash, 사전 ID 목록과 stop/restart 중 UI 전이 증거는 현재 PC에서 소급 확인할 수 없다. 실제 baseline은 기준서의 11개가 아니라 13개였다.
- 로컬 릴리스 산출물은 `Nozomi-Server.exe`, `Nozomi-Client.exe`, `Nozomi-Local.exe`, `Nozomi-Updater.exe`, `dist/Nozomi-Windows-x64-0.3.1.zip`, `dist/Nozomi-Update-0.2.1-to-0.3.1.zip`, `dist/Nozomi-Update-0.3.0-to-0.3.1.zip`이다. 각 zip 옆 `.sha256` sidecar를 권위 있는 hash로 사용한다.
- GitHub 초안 PR: `https://github.com/seop1000/nozomi_ai/pull/1`
- GitHub release: `https://github.com/seop1000/nozomi_ai/releases/tag/v0.3.1`

## 16. Exact commands to reproduce validation

아래 명령은 Windows PowerShell에서 실행한다. 실제 server data를 다루기 전에는 server 프로세스가 쓰는 정확한 data 경로를 확인한다.

```powershell
Set-Location D:\Nozomi

# 전체 자동화 테스트
.\.venv\Scripts\python.exe -m pytest -q

# 정적 검사
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy packages\nozomi_protocol apps\server\nozomi_server apps\client\nozomi_client

# 마이그레이션, 운영 스크립트, 메시지 내구성 및 Phase 2.1 WebSocket 집중 검증
.\.venv\Scripts\python.exe -m pytest -q `
  tests\unit\test_database_migrations.py `
  tests\unit\test_memory_operational_scripts.py `
  tests\unit\test_conversation_repository.py `
  tests\integration\test_chat_phase21.py

# PowerShell 스크립트 문법 검사
$scripts = Get-ChildItem -LiteralPath .\scripts -Filter *.ps1 -File
$parseErrors = foreach ($script in $scripts) {
    $tokens = $null
    $errors = $null
    [System.Management.Automation.Language.Parser]::ParseFile(
        $script.FullName,
        [ref]$tokens,
        [ref]$errors
    ) | Out-Null
    $errors
}
if ($parseErrors) { $parseErrors | Format-List; throw 'PowerShell parse failed.' }

# 현재 runtime DB의 읽기 전용 감사
.\.venv\Scripts\python.exe .\scripts\audit_runtime_memories.py --json --strict

# live data 변경 전 수동 backup (필요하면 -DataDir로 실제 경로 지정)
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\backup_nozomi_data.ps1

# LAN 관찰 검사: server를 종료하거나 설정을 바꾸지 않음
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\test_server_health.ps1 `
  -ServerHost <private-server-address> -Port <configured-port>
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\test_client_server_connection.ps1 `
  -ServerHost <private-server-address> -Port <configured-port>
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\test_reconnect.ps1 `
  -ServerHost <private-server-address> -Port <configured-port>

# EXE와 portable package
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build_executables.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build_portable.ps1 -Force
```

증분 update는 검증할 이전 portable 경로마다 별도로 만든다.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build_update.ps1 `
  -BasePath <0.2.1-portable-or-extracted-root> `
  -FromVersion 0.2.1 -ToVersion 0.3.1 -Force

powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build_update.ps1 `
  -BasePath <0.3.0-portable-or-extracted-root> `
  -FromVersion 0.3.0 -ToVersion 0.3.1 -Force
```

실제 Qwen smoke test는 자동 pytest에 포함하지 않는다. 사용자 data와 분리된 임시 DB, loopback llama-server 및 8개 질문을 사용하는 명령은 다음과 같다.

```powershell
.\.venv\Scripts\python.exe .\scripts\smoke_phase21_real_model.py `
  --output .\build\phase21-real-smoke.json

# 일부 case만 재검증하는 예: fallback 문항(case 6)
.\.venv\Scripts\python.exe .\scripts\smoke_phase21_real_model.py `
  --cases 6 --output .\build\phase21-real-smoke-case6.json
```

배포 검증에서는 두 patch 각각을 이전 portable 사본에 적용하고, rollback으로 이전 버전과 보호 대상 사용자 파일이 복구되는지 확인한 뒤 다시 0.3.1을 적용한다. 성공 로그는 `업데이트 완료 → 롤백 완료 → 재적용 완료 → REAL_PORTABLE_APPLY_ROLLBACK_OK` 순서여야 한다. 산출물 hash는 다음처럼 sidecar와 대조한다.

```powershell
Get-FileHash .\dist\Nozomi-Windows-x64-0.3.1.zip -Algorithm SHA256
Get-Content .\dist\Nozomi-Windows-x64-0.3.1.zip.sha256

Get-FileHash .\dist\Nozomi-Update-0.2.1-to-0.3.1.zip -Algorithm SHA256
Get-Content .\dist\Nozomi-Update-0.2.1-to-0.3.1.zip.sha256

Get-FileHash .\dist\Nozomi-Update-0.3.0-to-0.3.1.zip -Algorithm SHA256
Get-Content .\dist\Nozomi-Update-0.3.0-to-0.3.1.zip.sha256
```
