@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0bootstrap_python.ps1" -ProjectRoot "%~dp0.."
exit /b %errorlevel%
