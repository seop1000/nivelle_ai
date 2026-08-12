@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\run_locked.ps1" -ProjectRoot "%~dp0" -Mode all %*
if errorlevel 1 goto failed
exit /b 0

:failed
echo Nivelle stopped because an error occurred.
pause
exit /b 1
