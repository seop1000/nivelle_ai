@echo off
rem Nozomi 0.3.1 compatibility bridge; use Nivelle-Local.cmd for new installs.
call "%~dp0Nivelle-Local.cmd" %*
exit /b %errorlevel%
