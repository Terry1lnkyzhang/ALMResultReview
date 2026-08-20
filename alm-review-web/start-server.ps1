param(
    [string]$BindAddress = "0.0.0.0",
    [int]$Port = 8090
)

$ErrorActionPreference = "Stop"

$projectDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $projectDirectory ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    throw "Virtual environment not found: $python"
}

$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    $process = Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue
    $processName = if ($process) { $process.ProcessName } else { "unknown process" }
    throw "Port $Port is already in use by $processName (PID $($listener.OwningProcess))."
}

# ALM, AI and MySQL hosts are reachable directly; httpx would otherwise pick up the
# Windows registry proxy and fail with WinError 10061 whenever that proxy is down.
if (-not $env:NO_PROXY) {
    $env:NO_PROXY = "*"
}

Set-Location $projectDirectory
& $python -m uvicorn app.main:app --host $BindAddress --port $Port