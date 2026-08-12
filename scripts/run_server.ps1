$ErrorActionPreference = 'Stop'
& .\scripts\run_locked.ps1 -ProjectRoot . -Mode server
exit $LASTEXITCODE
