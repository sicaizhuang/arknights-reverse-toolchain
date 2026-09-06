param(
    [ValidateSet('status','il2cpp','native','all')]
    [string]$Module = 'status',
    [string]$ProfilePath = '',
    [string]$Output = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
if (-not $ProfilePath) { $ProfilePath = Join-Path $root 'targets\arknights_pc\profile.json' }
$profile = Get-Content -LiteralPath $ProfilePath -Raw | ConvertFrom-Json
if ($profile.profile_id -ne 'arknights_pc') { throw 'This adapter only accepts arknights_pc.' }
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
if (-not $Output) { $Output = Join-Path $root "reports\arknights_pc_adapter_$stamp" }
if (Test-Path -LiteralPath $Output) { throw "Refusing existing output: $Output" }
New-Item -ItemType Directory -Path $Output -Force | Out-Null

function Get-HashRecord([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return [ordered]@{ path = $Path; status = 'missing' }
    }
    $item = Get-Item -LiteralPath $Path
    return [ordered]@{ path = $Path; status = 'present'; bytes = $item.Length; sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash }
}

$inputRecords = @($profile.inputs.PSObject.Properties | ForEach-Object { Get-HashRecord ([string]$_.Value) })
$missingInputs = @($inputRecords | Where-Object { $_.status -eq 'missing' })
$evidence = $profile.static_evidence
$feasibilityPath = [string]$evidence.feasibility_report
$feasibility = if (Test-Path -LiteralPath $feasibilityPath) { Get-Content -LiteralPath $feasibilityPath -Raw | ConvertFrom-Json } else { $null }
$moduleResults = [ordered]@{}
if ($Module -in @('status','il2cpp','all')) {
    $moduleResults.il2cpp = [ordered]@{
        status = if ($missingInputs.Count -eq 0 -and $feasibility -and $feasibility.verdict.csharp_structure_and_static_mapping -eq 'passed') { 'reused_verified' } else { 'blocked_provenance' }
        source_report = $feasibilityPath
        dump_cs = Get-HashRecord ([string]$evidence.dump_cs)
        script_json = Get-HashRecord ([string]$evidence.script_json)
        method_bodies = 'partial_native_required'
        runtime_verified = $false
    }
}
if ($Module -in @('status','native','all')) {
    $nativePath = [string]$evidence.native_results_tsv
    $nativeRows = if (Test-Path -LiteralPath $nativePath) { @(Import-Csv -LiteralPath $nativePath -Delimiter "`t") } else { @() }
    $moduleResults.native = [ordered]@{
        status = if ($nativeRows.Count -gt 0 -and @($nativeRows | Where-Object { $_.decompile_status -eq 'current_static_candidate_success' }).Count -gt 0) { 'reused_verified_static_candidates' } else { 'not_available' }
        current_candidates = $nativeRows.Count
        pseudocode_success = @($nativeRows | Where-Object { $_.decompile_status -eq 'current_static_candidate_success' }).Count
        results_tsv = Get-HashRecord $nativePath
        global_autoanalysis = 'not_run'
        runtime_verified = $false
    }
}
if ($Module -eq 'status' -or $Module -eq 'all') {
    $moduleResults.assets = [ordered]@{ status = 'not_yet_validated_for_pc'; android_assets_are_not_reused_as_pc_payload = $true; runtime_verified = $false }
}
if (-not $moduleResults.Contains('il2cpp')) {
    $moduleResults.il2cpp = [ordered]@{ status = 'not_run'; runtime_verified = $false }
}
if (-not $moduleResults.Contains('native')) {
    $moduleResults.native = [ordered]@{ status = 'not_run'; runtime_verified = $false }
}
if (-not $moduleResults.Contains('assets')) {
    $moduleResults.assets = [ordered]@{ status = 'not_run'; runtime_verified = $false }
}
$overall = if ($missingInputs.Count -gt 0) { 'blocked_missing_pc_input' } elseif ($moduleResults.il2cpp.status -like 'blocked*') { 'partial' } else { 'partial_static_only' }
$status = [ordered]@{
    schema_version = 1
    target = 'arknights_pc'
    platform = 'windows-x64'
    mode = 'offline_report_adapter'
    created_utc = (Get-Date).ToUniversalTime().ToString('o')
    overall_status = $overall
    process_memory_read = $false
    adb_started = $false
    game_started = $false
    anti_cheat_interaction = $false
    input_records = $inputRecords
    modules = $moduleResults
    cross_platform_policy = 'join by logical names/config IDs/tokens only; never reuse RVA/VA across PC and Android'
}
$statusPath = Join-Path $Output 'adapter_status.json'
$status | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $statusPath -Encoding UTF8
$matrix = [ordered]@{ target = 'arknights_pc'; overall_status = $overall; il2cpp = $moduleResults.il2cpp; native = $moduleResults.native; assets = $moduleResults.assets; runtime = 'unverified' }
$matrixPath = Join-Path $Output 'capability_matrix.json'
$matrix | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $matrixPath -Encoding UTF8
$hashTargets = @($statusPath,$matrixPath,$ProfilePath,$feasibilityPath,[string]$evidence.dump_cs,[string]$evidence.script_json,[string]$evidence.native_results_tsv) | Select-Object -Unique
$hashes = @($hashTargets | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | ForEach-Object { $i=Get-Item -LiteralPath $_; [ordered]@{path=$_.ToString();bytes=$i.Length;sha256=(Get-FileHash -Algorithm SHA256 -LiteralPath $_).Hash} })
[ordered]@{ schema_version=1; algorithm='SHA-256'; records=$hashes } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $Output 'sha256_manifest.json') -Encoding UTF8
($hashes | ForEach-Object { $_.sha256 + ' *' + $_.path }) | Set-Content -LiteralPath (Join-Path $Output 'KEY_SHA256SUMS.txt') -Encoding UTF8
[pscustomobject]@{ output = $Output; overall_status = $overall; module = $Module; missing_inputs = $missingInputs.Count; output_hash_records = $hashes.Count } | ConvertTo-Json -Depth 5
