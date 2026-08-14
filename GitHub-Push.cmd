@echo off
cd /d "%~dp0"
echo.
echo ==== Git status ====
git status --short
echo.
set /p OK="? ????? GitHub? ????? (y/n): "
if /I not "%OK%"=="y" exit /b
git add .
git diff --cached --check
if errorlevel 1 (
    echo.
    echo [ERROR] git diff --cached --check failed.
    pause
    exit /b 1
)
git commit -m "Update Nivelle"
if errorlevel 1 (
    echo.
    echo Commit? ????? ??? ??? ??????.
    pause
    exit /b 1
)
git push origin main
echo.
echo ==== ?? ====
pause
