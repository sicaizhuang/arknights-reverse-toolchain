param([switch]$Push)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $root
& git pull --ff-only
if ($LASTEXITCODE -ne 0) { throw 'git pull failed; resolve the branch state manually.' }
& python scripts/public_audit.py .
if ($LASTEXITCODE -ne 0) { throw 'Public audit failed.' }
& python -m compileall -q targets scripts
if ($LASTEXITCODE -ne 0) { throw 'Python syntax check failed.' }
if ($Push) {
    Write-Warning 'Push requested; review git diff and credentials before confirming.'
    & git push
}
