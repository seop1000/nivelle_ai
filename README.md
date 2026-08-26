# Nivelle Lethia

> P0 실행 이름: 클라이언트는 `Nivelle-Link.exe`, 서버는 `Nivelle-Core.exe`입니다.
> 현재 bootstrap은 Python `>=3.12,<3.15`를 선택하고, 복사되거나 손상된 `.venv`를
> 업그레이드하지 않습니다. 새 환경을 임시 경로에서 검증한 뒤 교체합니다.
> Link는 `gateway_endpoint`만 소유하고 Core는 `provider_endpoint`를 소유합니다.
> 자세한 내용은 [P0 Foundation](docs/architecture/p0-foundation.md)을 참고하십시오.

Nivelle 0.4.0은 Windows 두 대에서 동작하는 로컬 중심 개인 AI 비서입니다. 서버 PC의
`Nivelle Core`가 Gateway·SQLite·Qwen을 담당하고, 클라이언트 PC의 `Nivelle Link`는
PySide6 대화 UI를 제공합니다. Link는 `llama-server`가 아니라 Core의 Gateway에만
연결합니다. 장기 기억은 `Nivelle Archive`에서 사용자 승인 항목만 사용합니다.

## 설치와 실행

PowerShell에서 저장소 루트를 연 뒤 개발 환경을 준비하고 Core를 실행합니다.

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\setup_dev.ps1
.\scripts\run_server.ps1
```

Windows x64 포터블 배포본은 서버 PC에서 `Nivelle-Core.exe`, 클라이언트 PC에서
`Nivelle-Link.exe`를 각각 실행합니다. 한 PC에서 Core와 Link를 함께 사용하는 1PC
구성은 `Nivelle-Local.exe`를 실행합니다. 같은 역할의 `Nivelle-Core.cmd`,
`Nivelle-Link.cmd`, `Nivelle-Local.cmd`도 제공됩니다.

`Nivelle-Local`은 현재 Core 포트의 loopback 주소를 Link에 직접 전달하고 로컬 전용
페어링 경로로 자동 인증합니다. 따라서 처음 실행할 때 서버 주소나 6자리 코드를 별도로
입력할 필요가 없습니다. 자동 페어링은 `127.0.0.1`, `localhost`, `::1`에만 허용되며,
일반 `Nivelle-Link`의 LAN 연결과 수동 페어링 동작은 그대로 유지됩니다.

실행기는 시작할 때 Python 3.12 이상과 `.venv`를 실제로 검사합니다. Python이 없으면
WinGet으로 사용자 범위에 설치하고, WinGet을 사용할 수 없으면 python.org가 서명한
64비트 설치 파일을 사용합니다. 다른 PC에서 복사해 온 가상환경이 손상되었으면 기존
폴더를 `.venv.broken-날짜`로 보존한 뒤 새 환경을 구성합니다.

소스 모드의 통합 실행은 다음과 같습니다.

```powershell
.\.venv\Scripts\python.exe .\nivelle.py
```

두 PC를 사용할 때는 Core의 `로컬 보안 관리` 창에 표시된 6자리 페어링 코드를 Link에
입력합니다. 코드는 10분 동안만 유효합니다. Link에서 서버 PC의 사설 LAN 주소와
Gateway 포트를 등록한 뒤 연결하십시오.

```powershell
.\scripts\run_client.ps1
```

Core와 Local 실행기는 Qwen3.5-27B Q4_K_M 주 모델, Qwen3.5-9B Q4_K_M 대체 모델 및
고정 버전 llama.cpp Vulkan 빌드를 검증하고, 없거나 무결성 검사가 실패하면 `runtime`
아래에 다시 내려받습니다. 두 모델 파일은 합계 약 24.2GB이므로 다운로드 임시 파일과
실행 여유 공간을 추가로 확보해야 합니다. 중단된 다운로드는 `.part` 파일에서
재개됩니다. Link는 모델이나 llama.cpp를 설치하지 않습니다.

인증 토큰은 Windows Credential Manager 호환 keyring에 저장되며 YAML과 로그에는
기록하지 않습니다.

## 대화와 Persona

Link의 메인 화면에는 대화와 입력창만 표시됩니다. 왼쪽 위 `≡` 메뉴에서 새 대화,
저장된 대화 기록, Core 연결·관리, Archive와 Persona 설정을 엽니다. 대화 기록은 Core의
SQLite 데이터베이스에 저장되며, 이전 대화를 불러와 문맥을 이어갈 수 있습니다.

기본 Persona는 `Nivelle Lethia Persona v1.0`입니다. 표기 이름은 `레시아 니벨`,
호칭은 `니벨`이며, 활성 제품·창·로그·기본 설정에는 Nivelle 이름만 사용합니다.
기존 사용자가 직접 작성한 대화와 기억은 이름 변경 과정에서도 원문을 보존합니다.

## Core와 모델 관리

`Nivelle-Core.exe`, `Nivelle-Core.cmd` 또는 `scripts/run_server.ps1`로 Core를 시작하면
서버 PC에 별도의 `로컬 보안 관리` 창이 열립니다. 이 창에서 변경되지 않는 서버 ID와
실제 Gateway 주소, 일회용 페어링 코드, 인증된 Link 목록을 확인할 수 있습니다. 일반
클라이언트에 관리자 권한을 부여하거나 해제할 수 있고, 인증을 해제하면 저장된 토큰과
현재 WebSocket 연결이 함께 무효화됩니다. 마지막 활성 관리자의 권한과 인증은 실수로
해제할 수 없도록 보호됩니다. 최초로 페어링한 Link만 관리자이며 이후 Link는 일반
권한으로 등록되므로 필요한 장치만 이 로컬 UI에서 승격하십시오. 이 관리 기능은 원격
무인증 API를 만들지 않고 Core 프로세스 내부에서만 실행되며 원본 토큰도 표시하지
않습니다.

Link 메뉴의 Core 관리 화면에서는 Gateway와 `llama-server`의 실제 상태, 시스템 사용량,
모델 및 추론 설정을 확인할 수 있습니다. 온도, top-p, top-k, 반복 패널티, 최대 출력
토큰과 seed는 다음 추론부터 적용됩니다. 포트, 컨텍스트 크기, GPU 레이어, 스레드, 배치,
모델 경로 변경은 `pending_restart`로 표시되며 관련 프로세스를 재시작한 뒤 적용됩니다.

Link의 `≡` 메뉴에서 독립된 `오디오 분석` 창을 열어 WAV 또는 Core의 FFmpeg가
디코딩할 수 있는 파일을 선택하면 채널별 waveform, spectrogram, 기본 음향 metric을
확인하고 재생·seek·zoom할 수 있습니다. Core 관리 화면과 Core의 로컬 보안 관리 UI에는
오디오 기능이 없습니다. 분석은 Core worker에서 실행되고 파일 내용 hash로 캐시됩니다.
포맷과 한계는 [Core Audio Analysis](docs/AUDIO_ANALYSIS.md)를 참고하십시오.

`models.yaml`에서 `external_url`을 지정하면 Core는 로컬 llama 프로세스를 시작하지
않습니다. 로컬 `llama-server`는 반드시 loopback에만 바인딩하고, Gateway도 공용
인터넷에 직접 노출하지 마십시오. 두 PC가 다른 네트워크에 있으면 공개 포트 포워딩보다
사설 VPN을 사용하십시오.

## Nivelle Archive

Link 메뉴의 `장기 기억`에서 기억을 추가·검색·수정·비활성화·삭제할 수 있습니다.
활성 상태이며 사용자가 명시적으로 승인한 기억만 우선순위 순으로 최대
`prompt_top_k`개가 프롬프트에 포함됩니다. 자동 추출은 기본값과 실제 동작 모두
비활성화되어 있습니다. 대화 원문이나 이메일·전화번호·주민번호·IP·자격 증명 형태의
정보는 자동으로 기억하지 않습니다. 세부 동작은 `docs/PHASE2_MEMORY.md`를
참고하십시오.

## 업데이트와 롤백

Core, Link, Local 및 `llama-server`를 종료한 뒤 `Nivelle-Update.cmd` 또는
`Nivelle-Updater.exe`를 실행합니다. 온라인 업데이터는 GitHub Releases에서 현재
버전에 맞는 패치와 SHA-256 sidecar를 함께 내려받아 검증합니다. 변경되는 코드만
`%LOCALAPPDATA%\Nivelle\Updater\backups`에 백업하며 `runtime`, `.venv`, `.env`,
데이터베이스, 로그와 사용자 설정은 보존합니다. 최근 업데이트는 앱 종료 후
`Nivelle-Rollback.cmd`로 되돌릴 수 있습니다.

0.3.1에서 0.4.0으로 넘어오는 한 번의 전환에만 이전 제품명이 들어간 브리지 자산과
실행기 이름을 호환용으로 인식합니다. 새 0.4.0 배포와 이후 업데이트는 Nivelle 이름을
사용합니다. 자세한 절차는 [온라인 업데이트](docs/ONLINE_UPDATES.md)와
[이름 변경 마이그레이션](docs/NIVELLE_RENAME_MIGRATION.md)을 참고하십시오. 기존 저장소
주소는 배포 호환성을 위해 유지됩니다:
[GitHub Releases](https://github.com/seop1000/nozomi_ai/releases/latest).

개발용 Windows x64 실행 파일과 포터블 ZIP은 다음 명령으로 생성합니다.

```powershell
.\scripts\build_executables.ps1
.\scripts\build_portable.ps1 -Force
```

## 데이터, 백업과 검사

0.4.0의 기본 데이터 경로는 다음과 같습니다.

- Core: `%LOCALAPPDATA%\Nivelle\NivelleCore`
- Link: `%LOCALAPPDATA%\Nivelle\NivelleLink`
- Updater: `%LOCALAPPDATA%\Nivelle\Updater`
- Core DB: `%LOCALAPPDATA%\Nivelle\NivelleCore\database\nivelle.db`

`NIVELLE_CORE_DATA_DIR`와 `NIVELLE_LINK_DATA_DIR`로 데이터 경로를 재지정할 수 있습니다.
수동 백업과 환경 검사는 다음과 같습니다.

```powershell
.\scripts\backup_nivelle_data.ps1
.\scripts\check_environment.ps1
.\scripts\run_tests.ps1
```

현재 텍스트 대화, 페어링·인증, 대화 저장, 설정 검증·이력·롤백, Core 상태 관리와
Nivelle Archive를 제공합니다. Phase 3 도구 실행은 별도의 보안 게이트와 승인 정책을
통과한 기능만 활성화합니다.
