@echo off
rem Nozomi 0.3.1 compatibility bridge; retained for old update packages.
call "%~dp0Nivelle-Rollback.cmd" %*
exit /b %errorlevel%
