# Run from any directory; requires Python 3.11+ and an authenticated OpenCode installation.
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
foreach ($command in @('py', 'opencode')) {
    if (-not (Get-Command $command -ErrorAction SilentlyContinue)) {
        throw "Required command missing: $command"
    }
}
Push-Location $repo
try {
    & py -3 -c "import sys; assert sys.version_info >= (3,11), 'Python 3.11+ required'"
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.11+ is required.' }
    $python = Join-Path $repo '.venv\Scripts\python.exe'
    if (-not (Test-Path $python)) {
        & py -3 -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed.' }
    }
    & $python -m pip install -e .
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
    & $python (Join-Path $PSScriptRoot 'run-live-demo.py')
    if ($LASTEXITCODE -ne 0) { throw 'Live demo failed. See the output above and opencode-demo.log.' }
}
finally {
    Pop-Location
}
