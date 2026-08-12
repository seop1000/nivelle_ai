@echo off
rem Nozomi 0.3.1 compatibility bridge; use Nivelle-Core.cmd for new installs.
call "%~dp0Nivelle-Core.cmd" %*
exit /b %errorlevel%
