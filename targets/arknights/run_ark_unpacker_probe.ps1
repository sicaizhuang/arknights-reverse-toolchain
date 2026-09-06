param(
    [Parameter(Mandatory=$true)]
    [string[]]$InputBundle,
    [string]$Output = '',
    [string]$ProfilePath = '',
    [string[]]$Match = @('ulpia','skill_02','s2','wave','flow','shuilang'),
    [string[]]$RetainPathId = @()
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
if (-not $ProfilePath) { $ProfilePath = Join-Path $root 'targets\arknights\profile.json' }
$profile = Get-Content -LiteralPath $ProfilePath -Raw | ConvertFrom-Json
if ($profile.profile_id -ne 'arknights') { throw 'The LZ4AK adapter only accepts the Arknights profile.' }
$config = $profile.lz4ak_adapter
if (-not $config.enabled) { throw 'The Arknights LZ4AK adapter is disabled.' }
$python = [string]$config.python
$script = [string]$config.entrypoint
if (-not [IO.Path]::IsPathRooted($python)) { $python = Join-Path $root $python }
if (-not [IO.Path]::IsPathRooted($script)) { $script = Join-Path $root $script }
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw "Python runtime missing: $python" }
if (-not (Test-Path -LiteralPath $script -PathType Leaf)) { throw "LZ4AK adapter missing: $script" }

$allowedRoots = @($config.allowed_roots | ForEach-Object {
    $value = [string]$_
    if (-not [IO.Path]::IsPathRooted($value)) { $value = Join-Path $root $value }
    [IO.Path]::GetFullPath($value).TrimEnd('\') + '\'
})
$resolvedInputs = @()
foreach ($item in $InputBundle) {
    if (-not (Test-Path -LiteralPath $item -PathType Leaf)) { throw "Input Bundle missing: $item" }
    $full = [IO.Path]::GetFullPath((Resolve-Path -LiteralPath $item).Path)
    if (-not ($allowedRoots | Where-Object { $full.StartsWith($_, [StringComparison]::OrdinalIgnoreCase) })) {
        throw "Input is outside the retained Arknights roots: $full"
    }
    $resolvedInputs += $full
}

if (-not $Output) {
    $Output = Join-Path $root ('reports\arknights_lz4ak_probe_' + (Get-Date -Format 'yyyyMMdd_HHmmss'))
}
if (Test-Path -LiteralPath $Output) { throw "Refusing existing output: $Output" }

$arguments = @($script, '--output', $Output)
foreach ($item in $resolvedInputs) { $arguments += @('--input', $item) }
foreach ($term in $Match) { if ($term) { $arguments += @('--match', $term) } }
foreach ($pathId in $RetainPathId) { if ($pathId) { $arguments += @('--retain-path-id', $pathId) } }
$parent = Split-Path -Parent $Output
New-Item -ItemType Directory -Path $parent -Force | Out-Null
$logRoot = Join-Path $parent ((Split-Path -Leaf $Output) + '_launcher_logs')
if (Test-Path -LiteralPath $logRoot) { throw "Refusing existing launcher log directory: $logRoot" }
New-Item -ItemType Directory -Path $logRoot | Out-Null
$stdout = Join-Path $logRoot 'stdout.txt'
$stderr = Join-Path $logRoot 'stderr.txt'
$started = [DateTimeOffset]::UtcNow
$watch = [Diagnostics.Stopwatch]::StartNew()
& $python @arguments 1> $stdout 2> $stderr
$exitCode = $LASTEXITCODE
$watch.Stop()
$command = [ordered]@{
    schema_version = 1
    started_utc = $started.ToString('o')
    duration_ms = $watch.ElapsedMilliseconds
    executable = $python
    arguments = $arguments
    exit_code = $exitCode
    stdout = $stdout
    stderr = $stderr
}
$commandJson = $command | ConvertTo-Json -Depth 6
[IO.File]::WriteAllText(
    (Join-Path $logRoot 'command.json'),
    $commandJson,
    [Text.UTF8Encoding]::new($true)
)
if ($exitCode -ne 0) { exit $exitCode }
Get-Content -LiteralPath $stdout
exit 0
