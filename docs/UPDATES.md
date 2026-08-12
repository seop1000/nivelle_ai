# Nivelle 업데이트와 롤백

Nivelle의 증분 패키지는 전체 프로그램 대신 변경된 코드만 교체합니다. 적용 전에
Nivelle Core, Link, Local과 Nivelle이 시작한 `llama-server`를 모두 종료하십시오.
실행 중인 관련 프로세스나 실행 잠금이 남아 있으면 파일을 바꾸지 않고 중단합니다.

## 일반 업데이트

1. `Nivelle-Update-<현재>-to-<대상>.zip`과 같은 폴더에 SHA-256 sidecar를 둡니다.
2. ZIP을 `Nivelle-Update.cmd` 위로 끌어다 놓거나, 두 파일을 같은 폴더에 두고
   `Nivelle-Update.cmd`를 실행합니다.
3. 온라인 릴리스에서 확인·다운로드·적용하려면 `Nivelle-Update-Online.cmd`를
   실행합니다.

명시적으로 패키지와 설치 루트를 지정할 수도 있습니다.

```powershell
.\Nivelle-Update.cmd `
  -PackagePath "D:\Downloads\Nivelle-Update-0.4.0-to-0.4.1.zip" `
  -TargetRoot "D:\Nivelle"
```

적용기는 패키지 구조, 대상 버전, payload 파일 크기와 SHA-256, 기존 코드의
SHA-256을 먼저 검사합니다. 검사가 실패하면 덮어쓰지 않습니다. `VERSION`은 나머지
코드가 모두 적용된 뒤 마지막에 교체되며, 중간 오류가 나면 이번 작업 전 상태로
자동 복구합니다. 기준 배포본과 다른 코드의 해시 불일치를 무시하는 강제 적용은
지원하지 않습니다.

## 보호 경로와 백업

업데이트와 롤백은 배포 코드만 대상으로 합니다. 다음 항목은 변경하지 않습니다.

- `runtime/**`의 Qwen 모델, llama.cpp, 다운로드 파일
- `.venv/**`, `.venv.broken-*`, `.env`
- 데이터베이스, 로그, GGUF, 임시 파일
- 사용자 설정과 Persona 파일
- `%LOCALAPPDATA%\Nivelle\NivelleCore/**`, `%LOCALAPPDATA%\Nivelle\NivelleLink/**`
- `NIVELLE_CORE_DATA_DIR`, `NIVELLE_LINK_DATA_DIR`로 지정한 폴더

교체·삭제되는 기존 코드는 다음 위치에 백업됩니다.

```text
%LOCALAPPDATA%\Nivelle\Updater\backups\<업데이트 작업 ID>\
```

이 백업은 사용자 문서·모델·DB를 포함하는 전체 PC 백업이 아닙니다. 중요한 데이터는
별도 백업 정책으로 관리하십시오.

## 롤백

Nivelle 앱과 `llama-server`를 모두 종료한 뒤 실행합니다.

```powershell
.\Nivelle-Rollback.cmd

.\Nivelle-Rollback.cmd `
  -BackupPath "$env:LOCALAPPDATA\Nivelle\Updater\backups\<업데이트 작업 ID>" `
  -TargetRoot "D:\Nivelle"
```

롤백은 현재 코드가 업데이트 뒤 다시 변경되지 않았는지 검사합니다. 충돌이 있으면
덮어쓰지 않고 중단하며, 롤백 도중 오류가 나면 롤백 직전 상태를 복구합니다.

## SHA-256 확인

```powershell
Get-FileHash ".\Nivelle-Update-0.4.0-to-0.4.1.zip" -Algorithm SHA256
Get-Content ".\Nivelle-Update-0.4.0-to-0.4.1.zip.sha256"
```

SHA-256은 손상과 자산 불일치를 확인하지만 제작자를 증명하는 서명은 아닙니다.

## 0.3.1 레거시 브리지

0.3.1에서 0.4.0으로 옮기는 단 한 번의 전환은 구 업데이터가 알아볼 수 있는 정확한
`Nozomi-Update-0.3.1-to-0.4.0.zip` 이름과 구 실행기를 호환 식별자로 유지합니다. 이들은
현재 제품 브랜딩이 아니며 전환 완료 후에는 `Nivelle-*` 이름만 사용합니다. 구
`%LOCALAPPDATA%\Nozomi\...`, `.nozomi`, `NOZOMI_*` 환경 변수와 `nozomi.db`도 구 데이터를
비파괴적으로 이전하고 롤백할 때만 읽는 0.3.1 호환 입력입니다.

## 개발자용 패키지 빌드

```powershell
powershell -NoLogo -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\build_update.ps1 `
  -BasePath .\dist\Nivelle-Portable-0.4.0.zip `
  -FromVersion 0.4.0 `
  -Force
```

기준 패키지에 `VERSION`이 있으면 `-FromVersion`을 생략할 수 있습니다. 빌드 결과는
`dist\Nivelle-Update-<from>-to-<to>.zip`과 SHA-256 sidecar로 생성됩니다.
