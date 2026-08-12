# Nivelle Agent Windows 경로 보안

## 보안 목표

파일시스템 도구는 사용자가 Nivelle Link에서 등록한 루트 안의 명시적으로 허용된 작업만
수행한다. Core가 보낸 경로 또는 “검증됨” 표시는 신뢰하지 않는다. Link는 접근 직전에
Windows 경로를 다시 canonicalize하고 루트 포함 관계, 민감도와 객체 동일성을 확인한다.

적용 도구는 `open_folder`, `search_files`, `read_text_file`이다. `create_note`는 임의 경로를
받지 않고 전용 `Nivelle Notes` 디렉터리에만 생성한다. `open_application`은 별도의 로컬
애플리케이션 레지스트리 검증을 사용한다.

## 기본 정책

- 등록 파일시스템 루트 없음
- 전체 드라이브 자동 허용 없음
- 직접 경로 입력 비활성화
- UNC/network path 비활성화
- hidden/system 항목 제외
- symlink, junction과 reparse point 비활성화
- 민감한 이름과 위치 차단
- 루트별 검색/읽기/폴더 열기 권한 분리

따라서 사용자가 로컬 관리 화면이나 정책에서 정확한 루트를 등록하고 해당 작업을 켜기
전에는 파일 도구가 성공하지 않는다.

## 검증 파이프라인

`WindowsPathValidator`는 다음 순서로 처리한다.

1. 입력 문자열을 trim하고 Unicode NFKC로 정규화한다.
2. `/`와 `\`를 Windows separator 의미로 통일한다.
3. `\\.\` device path와 모델이 제공한 `\\?\` extended path를 거부한다.
4. 정책이 허용하지 않는 `\\server\share` UNC/network path를 거부한다.
5. 어떤 구성 요소에라도 `..`가 있으면 거부한다.
6. 드라이브 접두사 외의 `:`를 alternate data stream으로 보고 거부한다.
7. `CON`, `PRN`, `AUX`, `NUL`, `CLOCK$`, `COM1..9`, `LPT1..9` 예약 이름을 거부한다.
8. 상대 경로를 거부한다.
9. `resolve(strict=...)`로 canonical path를 구한다.
10. 허용하지 않는 경우 경로 각 구성 요소의 symlink/junction/reparse 속성을 검사한다.
11. 대소문자 비구분 Windows 비교로 가장 구체적인 승인 루트 안에 있는지 확인한다.
12. 로컬 `denied_paths` 하위인지 확인한다.
13. 민감 파일/디렉터리 이름과 확장자를 확인한다.
14. 각 구성 요소의 hidden/system 속성을 정책과 대조한다.
15. 존재 여부, 파일/폴더 타입과 파일 크기를 확인한다.
16. 접근 직전 같은 검증을 반복하고 최초 객체 identity와 비교한다.

identity는 device, inode/file index에 해당하는 값, 크기와 수정 시각을 포함한다. 파일 읽기는
실제로 연 file descriptor의 identity도 비교한다. 검증 뒤 객체가 바뀌면 작업을 취소한다.

## `path_ref`

검색 결과와 폴더/파일 선택에는 원시 절대 경로보다 `path_ref`를 사용한다.

```text
<root_id>:<base64url(relative_path)>
```

Link는 `root_id`를 로컬 정책에서 다시 조회하고 relative path를 해당 루트와 결합한다.
디코딩 실패, 빈 root ID, 절대 relative path, 존재하지 않는 root ID는 거부한다. 복원된
경로도 전체 검증 파이프라인을 통과하므로 `path_ref` 자체는 권한 토큰이 아니다.

## 민감 파일 정책

기본 필터는 다음 범주를 거부하거나 검색 결과에서 제외한다.

- `.env`와 그 변형
- `.ssh`, `.gnupg`, `.aws`, `.azure`
- `id_rsa`, `id_ed25519` 등 private key 이름
- `.pem`, `.key`, `.p12`, `.pfx`, `.kdbx`
- 브라우저 `Login Data`, `Local State`, cookies
- credential/password manager 데이터
- token cache, API/auth token 이름
- Nivelle 인증·pairing secret 이름
- 이전 버전 인증 데이터의 호환 식별자

이 검사는 방어층이지 완전한 secret 탐지기가 아니다. 평범한 파일명 안에 있는 비밀을
판별할 수 없으므로 사용자는 비밀 저장소를 승인 루트에 포함하지 않아야 한다. 감사와 로그는
성공한 파일 내용 전체를 저장하지 않는다.

## 도구별 경로 규칙

### `open_folder`

- `path_ref` 하나를 우선 사용한다.
- 직접 경로는 로컬 `allow_direct_paths=true`일 때만 고려한다.
- 대상은 존재하는 폴더여야 한다.
- 루트의 `allow_open_folder`가 true여야 한다.
- 최종 재검증 뒤 Windows API로 연다.

### `search_files`

- `root_id` 하나에 한정한다.
- 파일 내용은 검색하지 않고 이름만 비교한다.
- 기본 50개, 최대 200개, 최대 깊이 8이다.
- 정렬된 순회 중 hidden/system/sensitive/reparse 항목을 건너뛴다.
- 취소와 제한 시간을 반복 확인한다.
- 반환 항목은 이름, relative path, 타입, 크기, 수정 시각과 새 `path_ref`뿐이다.
- 종료 시 루트 identity를 다시 확인한다.

### `read_text_file`

- 루트의 `allow_read`가 true여야 한다.
- 기본 파일 크기 1 MiB, 절대 최대 5 MiB 정책 범위 안에서 읽는다.
- 기본 32,000자이며 프로토콜 최대 100,000자다.
- UTF-8/BOM 기반 UTF-16·32를 우선하고, CP949/CP1252 fallback은 불확실 표시를 한다.
- NUL 또는 과도한 제어 바이트가 있는 파일은 binary로 거부한다.
- 파일 내용은 `trusted=false` 데이터로만 반환한다.

### `create_note`

- 모델이 목적지 경로 또는 확장자를 정할 수 없다.
- 형식은 `txt` 또는 `md`뿐이다.
- 제목은 NFKC 정규화하고 Windows 금지 문자와 예약 이름을 정리한다.
- 전용 디렉터리에서 UTF-8 임시 파일을 flush/fsync한다.
- hard-link 기반 create-only commit을 사용하고 충돌 시 `(1)`, `(2)` 접미사를 붙인다.
- 기존 파일을 덮어쓰거나 삭제하지 않는다.

### `open_application`

- 모델은 `application_id`만 제공한다.
- Link의 로컬 레지스트리가 ID를 절대 실행 파일 경로에 매핑한다.
- 경로는 존재하는 `.exe`이고 reparse point를 통과하지 않아야 한다.
- 정책 저장과 실행 직전에 shell, script host, installer와 일반 interpreter basename을
  fail-closed 목록으로 거부한다.
- 인자, URL, 환경 override와 shell string을 받지 않는다.
- `subprocess.Popen([canonical_executable], shell=False)` 형태로 실행한다.

단순히 `.exe`라는 사실만으로 안전한 애플리케이션은 아니므로 deny 목록과 등록/실행의
이중 검증을 회귀 테스트한다.

## 실패 코드

경로 관련 오류는 외부에 민감한 전체 경로를 노출하지 않는 안전한 메시지와 함께 다음
코드 중 하나를 사용한다.

- `path_not_allowed`
- `sensitive_path`
- `target_not_found`
- `result_too_large`
- `validation_failed`
- `permission_denied`
- `timed_out`
- `cancelled`
- `execution_failed`

검증 실패 후에는 파일 열기, 폴더 열기 또는 쓰기 같은 부작용이 없어야 한다.

## 필수 테스트

Windows에서 다음 케이스를 각각 허용/거부 결과와 무부작용까지 검사한다.

- 승인 루트 내부 정상 경로, 한국어/Unicode/긴 경로, 대소문자 차이
- 상대 경로, `..`, 혼합 separator, 루트 밖 절대 경로
- symlink, junction, reparse point와 검증 후 교체
- UNC, device path, extended path, alternate data stream
- 예약 이름, 숨김, system, 민감 이름
- 없는 대상, 잘못된 타입, 과대 파일, binary와 불확실 encoding
- 검색 깊이·항목·시간 제한과 취소
- 루트 자체가 실행 중 바뀌는 경우
- `path_ref` 변조, base64 오류와 다른 root ID 재사용

단위 테스트 외에 실제 NTFS junction과 Windows 속성을 사용하는 통합 테스트가 필요하다.
지원되지 않는 파일시스템에서 건너뛴 검사는 통과로 기록하지 않는다.
