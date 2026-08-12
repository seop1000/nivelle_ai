@echo off
rem Nozomi 0.3.1 compatibility bootstrap; retained only for the 0.3.1-to-0.4.0 bridge.
setlocal
if exist "%~dp0apply_update.ps1" (
    powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0apply_update.ps1" %*
    exit /b %errorlevel%
)
call "%~dp0Nivelle-Update.cmd" %*
exit /b %errorlevel%
