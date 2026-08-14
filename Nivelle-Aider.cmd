@echo off
setlocal

cd /d D:\nozomi

for /f "usebackq delims=" %%A in (`powershell -NoProfile -Command "[Environment]::GetEnvironmentVariable('NVIDIA_API_KEY','User')"`) do set "NVIDIA_API_KEY=%%A"

if "%NVIDIA_API_KEY%"=="" (
    echo NVIDIA_API_KEY is not configured.
    pause
    exit /b 1
)

set "OPENAI_API_KEY=%NVIDIA_API_KEY%"
set "OPENAI_API_BASE=https://integrate.api.nvidia.com/v1"

set "PATH=%USERPROFILE%\.local\bin;%PATH%"

aider

endlocal
