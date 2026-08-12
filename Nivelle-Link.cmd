@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\run_locked.ps1" -ProjectRoot "%~dp0" -Mode client %*
if errorlevel 1 goto failed
exit /b 0

:failed
echo Nivelle Link stopped because an error occurred.
pause
exit /b 1
