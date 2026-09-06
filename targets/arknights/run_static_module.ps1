param(
    [Parameter(Mandatory=$true)]
    [ValidateSet('unity','java','java-errors','native','correlate')]
    [string]$Module,
    [string]$ProfilePath = '',
    [string]$Output = '',
    [switch]$Resume,
    [int]$MaxBundles = 0,
    [int]$BatchSize = 50,
    [int]$BatchTimeoutSeconds = 900,
    [int]$FileTimeoutSeconds = 60,
    [int64]$DiskLimitBytes = 8589934592,
    [switch]$SmokeTest
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
if (-not $ProfilePath) { $ProfilePath = Join-Path $root 'targets\arknights\profile.json' }
$profile = Get-Content -LiteralPath $ProfilePath -Raw | ConvertFrom-Json
if ($profile.profile_id -ne 'arknights') { throw 'The static module runner only accepts the Arknights profile.' }
$static = $profile.phase1_static
$python = [string]$static.python
if (-not [IO.Path]::IsPathRooted($python)) { $python = Join-Path $root $python }
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw "Python runtime missing: $python" }

$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
if (-not $Output) {
    $Output = switch ($Module) {
        'unity'       { Join-Path $root "reports\arknights_unity_deep_inventory_$stamp" }
        'java'        { Join-Path $root "java\com.hypergryph.arknights_2.7.61_static_index_$stamp" }
        'java-errors' { Join-Path $root "java\com.hypergryph.arknights_2.7.61_error_correction_$stamp" }
        'native'      { Join-Path $root "native\current_structure_$stamp" }
        'correlate'   { Join-Path $root "reports\cross_layer_static_$stamp" }
    }
}
if ((Test-Path -LiteralPath $Output) -and -not ($Module -eq 'unity' -and $Resume)) {
    throw "Refusing existing output: $Output"
}

if ($SmokeTest) {
    if ($Resume) { throw '-Resume is not valid with -SmokeTest.' }
    $smokeScript = [string]$static.scripts.smoke
    if (-not (Test-Path -LiteralPath $smokeScript -PathType Leaf)) { throw "Static smoke entrypoint missing: $smokeScript" }
    & $python $smokeScript --module $Module --profile $ProfilePath --output $Output
    exit $LASTEXITCODE
}

$script = ''
$arguments = @()
switch ($Module) {
    'unity' {
        $script = [string]$static.scripts.unity
        if (-not [IO.Path]::IsPathRooted($script)) { $script = Join-Path $root $script }
        $arguments = @($script, '--profile', $ProfilePath, '--output', $Output, '--batch-size', $BatchSize, '--batch-timeout', $BatchTimeoutSeconds, '--file-timeout', $FileTimeoutSeconds, '--disk-limit-bytes', $DiskLimitBytes, '--max-bundles', $MaxBundles)
        if ($Resume) { $arguments += '--resume' }
    }
    'java' {
        $script = [string]$static.scripts.java
        if (-not [IO.Path]::IsPathRooted($script)) { $script = Join-Path $root $script }
        $arguments = @($script, '--jadx-root', [string]$static.inputs.jadx_root, '--base-apk', [string]$static.inputs.base_apk, '--jadx-command', [string]$static.inputs.jadx_command, '--jadx-stdout', [string]$static.inputs.jadx_stdout, '--jadx-stderr', [string]$static.inputs.jadx_stderr, '--output', $Output)
    }
    'java-errors' {
        $script = [string]$static.scripts.java_errors
        if (-not [IO.Path]::IsPathRooted($script)) { $script = Join-Path $root $script }
        $arguments = @($script, '--java-index', [string]$static.outputs.java_index, '--java-analysis', [string]$static.outputs.java_analysis, '--jadx-stdout', [string]$static.inputs.jadx_stdout, '--jadx-stderr', [string]$static.inputs.jadx_stderr, '--output', $Output)
    }
    'native' {
        $script = [string]$static.scripts.native
        if (-not [IO.Path]::IsPathRooted($script)) { $script = Join-Path $root $script }
        $arguments = @($script, '--primary-elf', [string]$static.inputs.current_elf, '--elf-root', [string]$static.inputs.decoded_arm64_root, '--provenance-report', [string]$static.inputs.apk_provenance, '--output', $Output)
    }
    'correlate' {
        $script = [string]$static.scripts.correlate
        if (-not [IO.Path]::IsPathRooted($script)) { $script = Join-Path $root $script }
        $arguments = @($script, '--unity-index', [string]$static.outputs.unity_index, '--java-index', [string]$static.outputs.java_index, '--jadx-root', [string]$static.inputs.jadx_root, '--native-index', [string]$static.outputs.native_index, '--legacy-dump', [string]$static.inputs.legacy_dump_cs, '--il2cpp-provenance', [string]$static.outputs.il2cpp_provenance, '--output', $Output)
    }
}
if (-not (Test-Path -LiteralPath $script -PathType Leaf)) { throw "Static module entrypoint missing: $script" }
& $python @arguments
exit $LASTEXITCODE
