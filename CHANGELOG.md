# Nivelle Lethia 변경 이력

## 0.4.0 (릴리스 후보 — 최종 검증 대기)

- 활성 제품·서버·클라이언트·기억·도구 실행기·업데이터 이름을 각각 Nivelle, Nivelle Core, Nivelle Link, Nivelle Archive, Nivelle Agent, Nivelle Updater로 통일
- 레시아 니벨 / Nivelle Lethia Persona v1.0과 중앙 identity/version 메타데이터를 추가하고 Core·Link 버전을 `0.4.0`으로 갱신
- 기존 0.3.1 데이터·Persona·연결 프로필·자격 증명을 보존하는 선행 백업, 충돌 시 fail-closed, SQLite 무결성 검사, 재실행 방지 marker 및 rollback 경로 추가
- `nivelle_protocol`, `nivelle_core`, `nivelle_link`를 활성 패키지로 전환하고 0.3.1 마이그레이션을 위한 얇은 호환 import·launcher·환경 변수 별칭을 한 릴리스 동안 유지
- 새 사용자 제출마다 고유 `request_id`·`client_message_id`를 만들고 `message_id` 기반 렌더링·history·reconnect 중복 방지로 assistant 완료 메시지를 정확히 한 번만 표시
- 인증된 별도 Agent WebSocket, 정확한 client/session capability routing, strict 공유 schema와 닫힌 8개 도구 registry를 구현
- Nivelle Link가 최종 권한자가 되는 deny-by-default 정책, 1회·세션·정확한 대상 승인, 쓰기 영구 승인 금지, 승인 만료·철회·취소와 상태 카드를 구현
- `get_system_status`, `get_active_window`, `open_application`, `open_folder`, `search_files`, `read_text_file`, `create_note`, `set_reminder`를 제한된 client-side 구현으로 추가하고 임의 shell·명령 문자열·삭제·덮어쓰기를 노출하지 않음
- Windows canonical path·허용 root·민감 파일·ADS·UNC·device path·reparse/junction 경계를 검증하고 검색을 streaming·bounded·cancellable하게 보강
- tool result를 `trusted=false` 데이터 경계로 분리하고 Core의 durable summary를 server-owned metadata로만 생성하며 Link 감사의 모든 자유형 문자열을 길이와 SHA-256으로 대체
- 동일 요청 replay와 연결 단절 후 불확실한 쓰기의 자동 재실행을 막고, 동일 대화의 정확히 같은 note/reminder 명시적 재시도는 7일 동안 기존 결과로 조정
- Nivelle Agent 정책·앱 allowlist·filesystem root·승인·감사 관리 창과 Nivelle Core의 읽기 전용 Agent 상태 화면을 추가
- Nivelle 이름의 Windows x64 EXE·portable ZIP·업데이터 흐름과 0.3.1 전환 전용 legacy bootstrap artifact를 추가
- 최종 자동 검증 `361 passed, 0 failed, 1 skipped`, 보안 집중 검증 `51 passed`, Ruff·strict mypy·PowerShell parser·diff check와 Nivelle EXE 4종 build/smoke 통과
- 최종 portable/update artifact의 SHA-256과 격리된 0.3.1 적용·rollback·재적용은 통과했으며, 실제 2PC tool smoke/disconnect만 미실행 사유와 함께 `docs/PHASE3_RESULT.md`에 분리 기록

## 0.3.1

- 질문 관련성이 우선되는 SQLite 하이브리드 장기 기억 검색과 한국어 부분 검색 보완
- 선택·제외된 기억의 점수와 사유를 보여 주는 대화 컨텍스트 이벤트 및 진단 화면 추가
- 자동 재연결 상태 머신, 오프라인 관리 작업 잠금과 메시지 중복 방지 보강
- Gateway·LLM·기억 DB·임베딩 상태를 구분하고 미구현 임베딩을 `unavailable`로 명시
- llama.cpp가 제공한 실제 토큰·timings와 측정 가능한 최초/전체 응답 지연을 생성 진단에 추가
- 앱·프로토콜·빌드·컴포넌트·실행 파일 경로를 포함하는 0.3.1 런타임 식별 정보 추가
- 프로토콜 주 버전 호환성 검사와 EXE 부모 설치 루트 업데이트 검증 추가
- user/assistant 원자 저장, 재시작 중단 복구, 완료 상태 단조 전이와 1회 제어 재시도 추가
- `VERSION` 단일 버전 원천, 동적 패키지 메타데이터와 v6 재시도 관계 마이그레이션 추가
- 서버/클라이언트 엔터티가 다른 하드웨어 기억을 제외하고 저장된 현재 사실을 답변에서
  불필요하게 미확인 정보로 낮추지 않도록 검색·프롬프트 규칙 보강

## 0.3.0

- 메인 클라이언트를 채팅과 입력에 집중된 단일 화면으로 재구성하고 좌우 빈 패널 제거
- `≡` 메뉴에 새 대화, 대화 기록, 서버 연결, 서버 관리, 장기 기억, 성격 관리 통합
- 사용자 질문과 Nivelle 스트리밍 답변을 독립된 plain-text 말풍선으로 분리
- 서버에 저장된 대화 목록·메시지 열람과 선택 대화 이어쓰기 연결
- 이전 대화 내용을 실제 모델 문맥에 포함하고 잘못된 대화 ID를 안전하게 거부
- 관리자용 성격 설정 조회·수정과 손상 방지·실패 롤백 YAML 저장 추가(안전 경계는 편집 대상에서 제외)

## 0.2.1

- 서버 PC나 포터블 폴더를 옮긴 뒤 남은 Qwen·llama.cpp 절대 경로를 현재 설치 위치로 자동 복구
- 누락된 llama-server 또는 GGUF 모델의 실제 확인 경로를 보여 주는 실행 오류 안내 추가
- 패치 가능한 Windows x64 서버·클라이언트·로컬·온라인 업데이트 EXE 4종과 포터블 ZIP 빌드 추가
- GitHub Releases 최신 버전 확인, 정확한 버전 패치 다운로드, SHA-256 검증과 안전 적용 추가
- `0.1.0` 및 `0.2.0` 설치본에서 직접 갱신할 수 있는 증분 패치 제공

## 0.2.0

- 실제 서버 상태, llama-server 상태와 시스템 사용량을 관리자 창에 연결
- 서버·모델·추론 설정 검증, 저장, 변경 이력과 롤백 구현
- 저장된 추론 설정을 Qwen 요청과 llama-server 실행 인수에 반영
- Phase 2 사용자 승인 장기 기억 CRUD·검색·프롬프트 주입 추가
- 서버/클라이언트 분리 실행과 Python·Qwen 자동 설치 안정화
- SHA-256 검증, 코드 백업, 실패 시 자동 복구를 제공하는 증분 업데이트 추가

## 0.1.0

- FastAPI Gateway, PySide6 클라이언트, 페어링과 텍스트 스트리밍 대화 기반 구현
