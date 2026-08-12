# Phase 2: 안전한 장기 기억

레시아 니벨의 첫 번째 Phase 2 수직 기능은 사용자가 직접 승인해 저장한 짧은 기억을 Core의
SQLite 데이터베이스에 보관하고, 활성 기억 중 우선순위가 높은 최대 `prompt_top_k`개만
대화 프롬프트에 넣는 기능이다. 임베딩 모델이나 외부 기억 서비스는 사용하지 않으며,
이 기능 때문에 모델을 다운로드하지 않는다.

## 데이터와 마이그레이션

- `schema_versions`가 적용된 버전을 기록한다. 기존 Phase 1 스키마는 버전 1,
  `memories` 테이블과 인덱스는 버전 2다.
- 검색은 SQLite FTS5를 우선 사용한다. 운영체제 SQLite에 FTS5가 없거나 인덱스가
  손상된 경우 매개변수화된 `LIKE ... ESCAPE` 검색으로 자동 전환한다.
- 저장 행에는 기억의 짧은 본문, 제한된 분류, 활성 상태, 우선순위와 시각만 기록한다.
  대화 ID, 클라이언트 ID, 메시지 원문, 프롬프트, 응답 로그는
  기억 테이블에 저장하지 않는다.
- 삭제 API는 행을 즉시 삭제한다.

## 개인정보 경계

기억은 500자 이하의 한 줄 요약이어야 한다. API는 여러 줄 대화 원문과 흔한 직접
식별자(이메일, 한국 전화번호, 주민등록번호, IP 주소, 명시적으로 표기된 주소·여권번호)
및 자격 증명을 거부한다. 의미 기반 개인정보 탐지기는 아니므로 이름이나 드문 식별자를
완벽하게 판별할 수 없다. 따라서 클라이언트 UI도 사용자에게 개인정보를 저장하지 말라고
안내해야 한다.

자동 추출 설정 `automatic_extraction`의 기본값은 `false`다. 이 최소 구현은 대화가
끝난 뒤 기억을 자동 생성하지 않는다. 설정이 켜지더라도 자동 추출기는 실행되지 않으며,
명시적인 `POST /api/v1/memories`로 저장한 행만 프롬프트 후보가 된다.

## 인증 API

모든 경로는 기존 `Authorization: Bearer <token>` 인증이 필요하다.

- `POST /api/v1/memories` — 기억 생성
- `GET /api/v1/memories?active=true&category=instruction&limit=20&offset=0` — 목록
- `GET /api/v1/memories/search?q=검색어&active=true&limit=20` — 텍스트 검색
- `GET /api/v1/memories/{id}` — 단일 조회
- `PATCH /api/v1/memories/{id}` — 본문·분류·활성 상태·우선순위 수정
- `DELETE /api/v1/memories/{id}` — 영구 삭제

생성·수정 본문은 `content`, 분류는 `preference`, `project`, `workflow`,
`instruction`, `other` 중 하나다. `priority`는 0~100이며 큰 값이 먼저 프롬프트에
포함된다. 같은 우선순위에서는 최근 수정 항목이 먼저다.

설정은 `GET/PUT /api/v1/settings/memory`에서 관리한다. `enabled=false` 또는
`prompt_top_k=0`이면 기억을 프롬프트에 넣지 않는다. `prompt_top_k`는 최대 10으로
검증된다.
