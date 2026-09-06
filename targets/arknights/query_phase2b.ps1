param(
    [Parameter(Mandatory=$true)][string]$Query,
    [ValidateSet('all','role','resource','class','method','string','native','unity','java','correlate')][string]$QueryKind = 'all',
    [int]$Limit = 20,
    [switch]$Json,
    [string]$Output = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$profilePath = Join-Path $PSScriptRoot 'profile.json'
$profile = Get-Content -LiteralPath $profilePath -Raw -Encoding UTF8 | ConvertFrom-Json
$args = @(
    (Join-Path $PSScriptRoot 'query_workbench.py'),
    '--profile', $profilePath,
    '--query', $Query,
    '--kind', $QueryKind,
    '--limit', [string]$Limit,
    '--format', $(if($Json){'json'}else{'text'})
)
if ($Output) { $args += @('--output',$Output) }
& ([string]$profile.phase1_static.python) @args
exit $LASTEXITCODE
