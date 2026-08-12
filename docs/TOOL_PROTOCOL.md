# Nivelle Agent 도구 프로토콜

## 기준과 버전

도구 프로토콜의 코드 기준은 `packages/nivelle_protocol/tools.py`다. 모든 메시지는
Pydantic의 `extra="forbid"` 정책을 사용하고, 중앙 `protocol_version`과 도구 버전
`1.0`을 검증한다. 날짜/시간 값에는 UTC offset이 있어야 하며 식별자는 UUID 또는 각
필드의 제한된 식별자 형식을 사용한다.

프로토콜 객체가 유효하다는 사실은 로컬 실행 허가를 뜻하지 않는다. Core와 Link는 각자
독립적으로 검증하며, Link 정책이 항상 최종 권한이다.

## `client.capabilities`

인증된 Link 세션은 다음 구조로 capability를 광고한다.

| 필드 | 의미 |
| --- | --- |
| `type` | 고정값 `client.capabilities` |
| `protocol_version` | 중앙 프로토콜 버전 |
| `client_id` | 인증된 Link ID |
| `session_id` | 이번 연결의 고유 세션 ID |
| `platform` | Phase 3에서는 `windows` |
| `app_version` | Link 애플리케이션 버전 |
| `tools` | 중복 이름이 없는 `ToolCapability` 목록 |
| `advertised_at` | offset이 포함된 광고 시각 |

각 capability에는 이름/버전, 활성화 여부, 구현 가용성, 위험 등급, 기본 승인 모드,
기본·최대 제한 시간, 결과 크기·항목·문자·줄 제한, 취소 지원, 멱등성 동작, 영구 승인
지원과 구현 ID가 포함된다.

Core는 인증에서 얻은 클라이언트 ID와 메시지의 ID를 대조해야 한다. 만료 시각은 연결
세션 수명과 서버 정책으로 계산해 저장하며 모델이 제공하지 않는다. 연결 해제 또는 새
광고 시 이전 세션과 누락된 capability는 만료된다.

## `tool.request`

| 필드 | 제약 |
| --- | --- |
| `type` | 고정값 `tool.request` |
| `protocol_version` | 지원 버전과 정확히 일치 |
| `tool_call_id` | 호출 고유 UUID |
| `request_id` | 사용자 턴 고유 UUID |
| `idempotency_key` | 실행 재전송을 묶는 UUID |
| `conversation_id` | 활성 대화 UUID |
| `user_message_id` | 요청한 사용자 메시지 UUID |
| `target_client_id` | Core가 선택한 Link UUID |
| `target_session_id` | 정확한 연결 세션 UUID |
| `tool_name` / `tool_version` | 닫힌 레지스트리 항목 |
| `arguments` | 해당 도구의 strict Pydantic 인자 |
| `risk_level` | 레지스트리 분류와 정확히 일치 |
| `created_at` | offset 포함 시각 |
| `timeout_ms` | 프로토콜 및 도구 최대값 이하 |
| `user_intent_summary` | 승인 UI용 제한된 일반 언어 요약 |

인자 JSON은 프로토콜 상 256 KiB 이하이며, 도구별 모델이 더 작은 한도를 적용한다.
모델이 제시한 클라이언트 ID를 라우팅에 사용하지 않는다. 일반 assistant 텍스트, 정규식으로
추출한 명령, 알 수 없는 필드나 잘못된 JSON은 실행하지 않는다.

## `tool.result`

모든 결과는 `trusted=false`이고 `source_tool`이 `tool_name`과 같아야 한다.

| 필드 | 의미 |
| --- | --- |
| `result_id` | 결과 고유 UUID |
| `tool_call_id`, `request_id` | 원 요청과의 상관관계 |
| `target_client_id`, `target_session_id` | 결과를 만든 정확한 Link 세션 |
| `tool_name`, `tool_version` | 실행한 등록 구현 |
| `status` | 실행 종착 상태 |
| `started_at`, `completed_at`, `duration_ms` | 실행 시간 정보 |
| `result` | 성공 시에만 존재하는 도구별 구조화 결과 |
| `safe_summary` | 감사·최종 답변용 제한된 요약 |
| `truncated`, `original_size`, `returned_size`, `omitted_count` | 제한/잘림 정보 |
| `error_code`, `error_message`, `retryable` | 실패 시에만 존재하는 안전한 오류 |

성공은 `completed`와 유효한 `result`가 함께 있을 때만 인정한다. 실패·거부·취소·만료·연결
해제 결과에는 성공 데이터가 없어야 하며 오류 코드와 메시지가 필요하다. 전체 결과 JSON의
프로토콜 상 절대 한도는 5 MiB이고, 레지스트리의 도구별 한도가 먼저 적용된다.

## 이벤트

모든 이벤트는 `tool_call_id`, `request_id`, `target_client_id`, `target_session_id`와 발생
시각으로 상관관계를 유지한다.

| 이벤트 | 상태 | 추가 요구사항 |
| --- | --- | --- |
| `tool.proposed` | `proposed` | 새 호출 또는 정확한 replay |
| `tool.validation_failed` | `validation_failed` | 안전한 오류 필수 |
| `tool.request` | `validated` | 활성 capability 확인 후 전송 |
| `tool.approval_required` | `awaiting_approval` | 실행 전 대기 |
| `tool.approved` | `approved` | `ToolApprovalDecision` 필수 |
| `tool.denied` | `denied` | deny 결정과 오류 필수 |
| `tool.queued` | `queued` | 병렬 한도 적용 |
| `tool.started` | `running` | 실제 실행 시작 |
| `tool.progress` | `running` | 1부터 증가하는 진행 sequence와 단위 |
| `tool.completed` | `completed` | 검증된 성공 결과 |
| `tool.failed` | `failed` | 안전한 오류 필수 |
| `tool.cancelled` | `cancelled` | 취소 오류 필수 |
| `tool.timed_out` | `timed_out` | 실행/승인 만료 오류 필수 |
| `tool.client_disconnected` | `client_disconnected` | 대상 세션 연결 해제 오류 필수 |

이 표는 감사 상태 전체를 설명하며 모든 행이 WebSocket에서 Link가 보내는 메시지는 아니다.
Core가 소유하고 기록하는 `tool.proposed`, `tool.request`,
`tool.approval_required`, `tool.queued`를 Link가 보내면 상관관계 오류로 거부한다.
승인이 필요한 요청은 Core가 먼저 `awaiting_approval`로 영속화한 채 Link에
`tool.request`를 보낸다. Link는 로컬 카드 상태만 `승인 대기`로 표시하고, 사용자가
허용하면 `tool.approved`만 보낸다. Core는 같은 소켓과 호출을 확인한 뒤
`approved → queued`를 직렬로 기록한다. 그 다음 Link의 `tool.started`와 단 하나의
`tool.result`를 받는다. 승인 불필요 요청은 Core가 전송 전에 `validated → queued`를
기록한다.

## 상태 전이 규칙

허용되는 전이는 다음과 같다.

```text
proposed -> validated | validation_failed | client_disconnected
validated -> awaiting_approval | queued | client_disconnected
awaiting_approval -> approved | denied | timed_out | client_disconnected
approved -> queued | client_disconnected
queued -> running | cancelled | client_disconnected
running -> completed | failed | cancelled | timed_out | client_disconnected
```

모든 종착 상태는 이후 전이를 허용하지 않는다. `tool.progress`는 `running`을 유지하는
메타데이터 이벤트다. `validated → queued`는 승인이 불필요한 호출에만 허용한다.

## 도구별 제한

| 도구 | 기본/최대 시간 | 최대 결과 | 추가 제한 |
| --- | --- | --- | --- |
| `get_system_status` | 5초 / 10초 | 64 KiB | 볼륨 최대 64개 |
| `get_active_window` | 3초 / 5초 | 32 KiB | 메타데이터만 |
| `open_application` | 10초 / 15초 | 16 KiB | 등록 ID, 인자 없음 |
| `open_folder` | 10초 / 15초 | 16 KiB | 정확한 승인 루트 |
| `search_files` | 10초 / 30초 | 100,000 bytes | 최대 200개, 깊이 8, 취소 가능 |
| `read_text_file` | 10초 / 30초 | 500,000 bytes | 최대 100,000자·10,000줄 |
| `create_note` | 10초 / 30초 | 32 KiB | txt/md, 덮어쓰기 없음 |
| `set_reminder` | 10초 / 30초 | 32 KiB | 미래 시각·유효 timezone |

클라이언트 정책은 이 값보다 엄격할 수 있으나 넓힐 수 없다. 서버가 요청한 시간도 정의와
로컬 정책 중 가장 작은 값으로 제한한다.

## 상관관계와 replay

- `request_id`는 사용자 턴마다 새로 생성한다.
- `tool_call_id`는 제안된 각 호출마다 새로 생성한다.
- `idempotency_key`는 동일 실행의 재전송에만 재사용한다.
- exact replay는 원래의 변경 불가능한 라우팅/도구/인자 요약과 모두 같아야 한다.
- 같은 ID에 다른 내용이 오면 충돌로 거부한다.
- 다른 client/session의 승인·진행·결과는 거부한다.
- 완료 결과의 중복 수신은 두 번째 로컬 동작이나 assistant 메시지를 만들지 않아야 한다.

## 신뢰되지 않은 결과 경계

창/경로/파일 관련 결과는 `source_tool`, `trusted=false`, `result_id`, 잘림 메타데이터와
명확한 content boundary를 가진다. 원시 파일 내용은 system 메시지에 넣지 않고 별도의
tool-result 데이터 메시지로 모델에 전달한다. 프롬프트는 결과 안의 명령을 무시하고 새
행동은 다시 일반 요청·정책·승인 절차를 거치도록 명시한다.

## 호환성과 오류 처리

지원하지 않는 `protocol_version` 또는 도구 버전, 알 수 없는 필드, 비유한 JSON,
timezone이 없는 시각, 위험 등급 불일치와 한도 초과는 검증 실패다. 구현은 추측해서 값을
고치거나 무제한으로 재계획하지 않는다. 현재 native 함수 호출 파서는 잘못된 구조를
실행하지 않고 거부한다. 제한된 1회 복구가 도입되는 경우에도 복구 결과 전체가 동일한
strict 검증을 다시 통과해야 한다.

## 검증 게이트

프로토콜 단위 테스트는 스키마, 8개 레지스트리, 이벤트 모양, 상태 전이, capability 제한,
결과 잘림과 오류 조합을 검사한다. 생산 준비 판정에는 실제 인증 WebSocket에서 다음을
추가로 증명해야 한다.

- capability 광고가 인증된 세션에 귀속되고 연결 해제 시 만료되는가
- 동시에 도착한 상태/결과가 호출별 순서를 보존하는가
- 다른 세션과 stale replay가 실패하는가
- 결과 중복이 assistant 완료와 로컬 부작용을 중복시키지 않는가
- 과대 프레임과 연결 중단이 안전한 종착 상태로 기록되는가
