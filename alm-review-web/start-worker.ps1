$ErrorActionPreference = "Stop"

$projectDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $projectDirectory ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    throw "Virtual environment not found: $python"
}

# ALM, AI and MySQL hosts are reachable directly; httpx would otherwise pick up the
# Windows registry proxy and fail with WinError 10061 whenever that proxy is down.
if (-not $env:NO_PROXY) {
    $env:NO_PROXY = "*"
}

Set-Location $projectDirectory
& $python -m app.worker