param([string]$PythonCommand = 'python')
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$environmentRoot = Join-Path (Split-Path -Parent $repoRoot) '.venv'
& $PythonCommand -c 'import sys; assert sys.version_info >= (3, 13), "Python 3.13+ required"'
if ($LASTEXITCODE -ne 0) { throw 'Python version check failed' }
if (-not (Test-Path -LiteralPath (Join-Path $environmentRoot 'Scripts\python.exe'))) {
    & $PythonCommand -m venv $environmentRoot
    if ($LASTEXITCODE -ne 0) { throw 'Failed to create virtual environment' }
}
$trialPython = Join-Path $environmentRoot 'Scripts\python.exe'
& $trialPython -m pip install -r (Join-Path $PSScriptRoot 'requirements-tested.txt')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
& $trialPython -m pip install --no-deps -e $repoRoot
if ($LASTEXITCODE -ne 0) { throw 'Project installation failed' }
Write-Output "Environment ready: $trialPython"
