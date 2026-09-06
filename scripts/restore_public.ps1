param([switch]$SkipInstall)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $root
if (-not (Get-Command python -ErrorAction SilentlyContinue)) { throw 'Python 3.11+ is required.' }
if (-not (Test-Path (Join-Path $root '.venv\Scripts\python.exe'))) {
    & python -m venv (Join-Path $root '.venv')
}
if (-not $SkipInstall) {
    & (Join-Path $root '.venv\Scripts\python.exe') -m pip install --disable-pip-version-check -r (Join-Path $root 'third_party\unitypy_requirements.lock.txt')
}
Write-Output "Restored public toolchain at $root"
