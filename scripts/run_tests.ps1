$ErrorActionPreference = 'Stop'
& .\.venv\Scripts\python.exe -m pytest
if ($LASTEXITCODE) { exit $LASTEXITCODE }
& .\.venv\Scripts\python.exe -m ruff check .
if ($LASTEXITCODE) { exit $LASTEXITCODE }
& .\.venv\Scripts\python.exe -m mypy
exit $LASTEXITCODE
