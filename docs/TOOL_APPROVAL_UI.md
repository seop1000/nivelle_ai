# Nivelle Agent 승인 UI

## 목적

승인 UI는 모델 제안을 사용자가 이해할 수 있는 로컬 결정으로 바꾸는 보안 경계다. 승인은
채팅 답변이나 서버 명령이 아니라 Nivelle Link 안의 명시적 버튼 동작에서만 발생한다.
카드는 실행 전에 정확한 도구, 대상 Link, 위험과 중요한 인자를 보여 주고 상태 기계의
결과를 계속 표시한다.

## 채팅 승인 카드

`ToolApprovalCard`는 채팅 타임라인에 삽입되는 일반 텍스트 카드다. 카드 ID는
`tool_call_id`이며 같은 ID로 다시 표시를 요청하면 기존 카드 객체를 재사용한다.

표시 항목은 다음과 같다.

- 도구 표시명과 일반 언어 작업 요약
- 대상 Link 이름 또는 `target_client_id`
- 애플리케이션, 루트, `path_ref`, 파일, 노트 또는 알림의 안전한 대상 요약
- 위험 등급
- 요청 이유 또는 사용자 의도 요약
- 안전하게 표시할 수 있는 중요 인자
- 승인 만료와 현재 호출 상태

카드는 인자 전체를 그대로 렌더링하지 않는다. 현재 안전 표시 allowlist는
`application_id`, `root_id`, `path_ref`, `query`, `title`, `scheduled_at`, `format`이다.
토큰, 인증 헤더, 비밀번호, 전체 파일 내용과 알림 원문은 일반 상세 영역에 표시하지 않는다.

## 버튼과 키보드 안전성

지원 버튼은 다음과 같다.

- 거부
- 한 번 허용
- 이 세션에서 허용(해당 도구가 지원할 때)
- 이 정확한 대상만 항상 허용(해당 도구가 지원할 때)

모든 버튼은 `autoDefault=false`, `default=false`다. 카드가 focus를 가졌을 때 Enter와
Return은 소비하고 어떤 승인도 내보내지 않는다. Escape는 안전하게 거부한다. 한 번 결정된
카드는 모든 버튼을 비활성화하여 중복 결정을 막는다.

만료 timer가 끝나면 카드는 `deny_expired`를 내보내고 버튼을 잠근다. `completed`,
`failed`, `cancelled`, `timed_out`, `denied`, `client_disconnected` 상태도 카드를 종착
상태로 잠근다.

`LOCAL_WRITE`에는 영구 승인 버튼을 표시하지 않는다. Phase 3 정책상 쓰기는 매번 승인해야
하므로 세션 승인 버튼도 표시하지 않아야 하며, UI payload가 잘못되어도 로컬 권한
관리자가 같은 모드를 거부해야 한다.

## 상태 표시

UI는 자체적으로 실행 성공을 추측하지 않는다. 다음 서버/Link 상태를 받아 표시한다.

```text
승인 대기 -> 승인됨/거부됨/만료됨
승인됨 -> 대기열 -> 실행 중
실행 중 -> 완료/실패/취소/시간 초과/연결 해제
```

`completed`는 검증된 Link 결과를 Core가 수락한 뒤에만 표시한다. 네트워크가 끊겼거나
결과가 없으면 성공 문구를 표시하지 않는다. 여러 호출은 서로 다른 `tool_call_id`와 카드로
구분한다.

## 노트와 알림 미리보기

`create_note`와 `set_reminder` 승인은 부작용 전에 다음을 분명히 보여야 한다.

- 노트: 정리된 파일명 예상값, `txt`/`md`, 내용의 길이와 줄 수, 승인할 정확한 내용의
  bounded plain-text preview
- 알림: 제목, 알림 내용의 bounded preview, timezone, 현지 시각과 UTC 시각

미리보기는 실행 인자와 동일한 정규화 결과에서 생성해야 한다. 렌더링을 위해 HTML로
해석하지 않고 선택 가능한 plain text로 표시한다. preview를 자를 경우 잘림을 명시한다.

일반 상세 allowlist에는 `content`와 `reminder_text`를 넣지 않는다. 대신 카드는 별도의
읽기 전용 plain-text `preview` 영역을 지원한다. 생산 경로는 승인할 정확한 인자에서 bounded
preview를 만들고, preview와 실행 인자의 exact-match를 테스트해야 한다.

## 대화 기록과 replay

카드는 거부·실패를 포함해 대화 기록에 남아야 한다. live UI는 `tool_call_id` map으로 동일
카드의 중복 생성을 막는다. 릴리스 완료에는 다음 복원 동작까지 필요하다.

- 대화를 다시 열 때 Core의 `tool_calls`/event 기록에서 terminal 카드를 한 번만 복원
- 대기 카드의 만료 시각을 재계산하고 만료된 카드에 승인 버튼을 다시 열지 않음
- 재연결 replay가 동일 카드와 상태를 갱신하되 새 카드를 만들지 않음
- 다른 대화의 호출을 현재 대화에 표시하지 않음
- 기록에서 복원한 완료/거부 카드의 버튼은 항상 비활성화

현재 live `tool_call_id` deduplication, 대화별 tool-call 조회 API와 읽기 전용 terminal 카드
복원 경로가 구현되어 있다. 이 코드의 존재만으로 재로드 지속성이 증명되지는 않으므로 실제
기록 API와 재연결 시험 결과가 필요하다.

## Nivelle Agent 관리 창

관리 창은 MainChatWindow당 하나만 생성하는 singleton 보조 창이다. 다음 읽기 전용 표와
로컬 제어를 제공한다.

- 개요: Agent 상태, 연결 Core, client/session ID, 활성 도구, 대기 승인, 최근 실패
- 도구: 이름, 활성화, 위험, 기본 승인, 구현 가용성, 제한 시간
- 애플리케이션: ID, 표시명, 실행 파일, 활성화, 영구 승인 가능 여부
- 파일시스템: root ID, 표시명, canonical path와 검색/읽기/폴더 열기 권한
- 승인: 범위, 모드, 생성/최근 사용 시각과 철회
- 감사: 도구, 상태, 대상 hash 요약, 시간, 오류

화면은 알려진 필드만 표에 넣으므로 snapshot의 임의 `token` 필드는 렌더링하지 않는다.
활성화 toggle, 새로고침과 철회 signal은 로컬 정책 서비스에 연결되어야 한다. 창의 존재나
signal 정의만으로 정책 변경이 실제 원자 저장·capability 재광고까지 수행된다고 간주하지
않는다.

## Nivelle Core 관리 화면

Core 관리 창의 Agent 페이지는 registry 버전, orchestration 활성 상태, 연결 클라이언트와
capability, 대기/실행/실패 통계, 프로토콜 호환성과 선택된 대상을 표시한다. 로컬
애플리케이션/파일 루트나 승인을 수정하는 버튼은 두지 않는다.

## 접근성 및 개인정보 검증

- 긴 대상은 줄바꿈되고 마우스로 선택할 수 있어야 한다.
- 색상만으로 위험과 상태를 표현하지 않는다.
- focus 이동 순서에서 승인 버튼이 기본 동작이 되지 않는다.
- Enter/Return이 어떤 자식 widget focus에서도 승인으로 전파되지 않는지 확인한다.
- Escape 거부는 한 번만 발생한다.
- 제어 문자와 bidi spoofing으로 표시/실행 대상이 다르게 보이지 않는지 검사한다.
- 화면 캡처나 로그에 secret 값이 노출되지 않는 fixture를 사용한다.

## 자동화된 UI 시험

`QT_QPA_PLATFORM=offscreen`에서 최소 다음을 확인한다.

1. Enter/Return 무동작, Escape 거부
2. 거부·한 번·세션·정확 대상 결정 signal
3. `LOCAL_WRITE`에서 세션/영구 승인 미표시
4. 만료 후 모든 승인 버튼 비활성화
5. pending/running/각 종착 상태 표시
6. 같은 `tool_call_id` 카드 중복 방지
7. Agent 창 singleton
8. snapshot secret 필드 미표시
9. 오프라인 시 변경 제어 비활성화
10. 재연결과 대화 재로드 카드 복원
11. 노트/알림 bounded preview와 실행 인자의 exact-match
12. 긴 Unicode 경로 가독성

실제 마우스/키보드 시험에서는 승인 버튼을 누르기 전 부작용이 없고, 거부·만료 후에도
도구가 실행되지 않았음을 로컬 감사와 대상 파일/프로세스로 함께 확인한다.
