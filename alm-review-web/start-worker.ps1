$ErrorActionPreference = "Stop"

$projectDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $projectDirectory ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    throw "Virtual environment not found: $python"
}

Set-Location $projectDirectory
& $python -m app.worker