@echo off
rem Nozomi 0.3.1 compatibility bridge; use Nivelle-Link.cmd for new installs.
call "%~dp0Nivelle-Link.cmd" %*
exit /b %errorlevel%
