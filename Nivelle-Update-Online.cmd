@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\update_from_github.ps1" %*
if errorlevel 1 goto failed

echo.
echo Nivelle online update completed successfully.
if /I "%NIVELLE_NO_PAUSE%"=="1" exit /b 0
rem NOZOMI_NO_PAUSE is accepted only from 0.3.1 automation.
if /I "%NOZOMI_NO_PAUSE%"=="1" exit /b 0
pause
exit /b 0

:failed
echo.
echo Nivelle online update failed. Review the error above.
if /I "%NIVELLE_NO_PAUSE%"=="1" exit /b 1
rem NOZOMI_NO_PAUSE is accepted only from 0.3.1 automation.
if /I "%NOZOMI_NO_PAUSE%"=="1" exit /b 1
pause
exit /b 1
