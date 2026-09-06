param(
    [Parameter(Mandatory=$true)]
    [string]$ValidatedOutput,
    [string]$Output = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
if (-not $Output) { $Output = Join-Path $root ('tests\arknights_effects_' + (Get-Date -Format 'yyyyMMdd_HHmmss')) }
if (Test-Path -LiteralPath $Output) { throw "Output already exists: $Output" }
New-Item -ItemType Directory -Path $Output | Out-Null

$profilePath = Join-Path $root 'targets\arknights\profile.json'
$cliPath = Join-Path $root 'scripts\toolchain.ps1'
$profile = Get-Content -LiteralPath $profilePath -Raw | ConvertFrom-Json
$cli = Get-Content -LiteralPath $cliPath -Raw
$validated = (Resolve-Path -LiteralPath $ValidatedOutput).Path
$adapterReport = Get-Content -LiteralPath (Join-Path $validated 'effects_adapter_report.json') -Raw | ConvertFrom-Json
$renderReport = Get-Content -LiteralPath (Join-Path $validated 'effect_render.json') -Raw | ConvertFrom-Json
$stageReport = Get-Content -LiteralPath (Join-Path $validated 'effect_stage.json') -Raw | ConvertFrom-Json

$tests = [Collections.Generic.List[object]]::new()
function Add-Test([string]$Name, [bool]$Passed, [object]$Observed) {
    $tests.Add([ordered]@{ name=$Name; passed=$Passed; observed=$Observed })
}

Add-Test 'active_profile_uses_effects_adapter' ($profile.effects_adapter.enabled -eq $true) $profile.effects_adapter.status
$profileFields = @($profile.PSObject.Properties.Name)
Add-Test 'old_profile_fields_removed' (($profileFields -notcontains 'ulpianus_s2_identity') -and ($profileFields -notcontains 'ulpianus_s2_preview')) 'both absent'
Add-Test 'old_cli_cases_removed' (($cli -notmatch "'ulpianus-s2'\s*\{") -and ($cli -notmatch "'ulpianus-s2-preview'\s*\{")) 'no active switch cases'
$legacyPath = [string]$profile.legacy_pending_delete.ulpianus_s2_offline_preview.path
Add-Test 'legacy_code_quarantined' ((Test-Path -LiteralPath $legacyPath) -and $profile.legacy_pending_delete.ulpianus_s2_offline_preview.active_entrypoint -eq $false) $legacyPath
Add-Test 'converter_hash_matches_profile' ((Get-FileHash -LiteralPath $profile.effects_adapter.converter -Algorithm SHA256).Hash -eq $profile.effects_adapter.converter_sha256) $profile.effects_adapter.converter_sha256
Add-Test 'formal_route_passed' ($adapterReport.status -eq 'passed_single_frame') $adapterReport.status
Add-Test 'exact_bundle_hash_matches' ($adapterReport.input_sha256 -eq $profile.effects_adapter.validated_sample.bundle_sha256) $adapterReport.input_sha256
Add-Test 'dependency_closure_complete' (($stageReport.unresolved_count -eq 0) -and ($renderReport.failed_bundle_count -eq 0)) ([ordered]@{resolved=$stageReport.resolved_cab_count;unresolved=$stageReport.unresolved_count;loaded=$renderReport.loaded_bundle_count;failed=$renderReport.failed_bundle_count})
Add-Test 'authenticity_gate_no_replacements' (($adapterReport.authenticity_gates.replacement_shader_used -eq $false) -and ($adapterReport.authenticity_gates.generated_texture_used -eq $false) -and ($adapterReport.authenticity_gates.synthetic_particle_used -eq $false)) $adapterReport.authenticity_gates
Add-Test 'vulkan_shaders_supported' (($renderReport.graphics_device -eq 'Vulkan') -and ($renderReport.unsupported_non_null_shader_count -eq 0)) ([ordered]@{graphics=$renderReport.graphics_device;unsupported=$renderReport.unsupported_non_null_shader_count})

$png = [IO.File]::ReadAllBytes((Join-Path $validated 'effect_frame.png'))
$width = [Net.IPAddress]::NetworkToHostOrder([BitConverter]::ToInt32($png,16))
$height = [Net.IPAddress]::NetworkToHostOrder([BitConverter]::ToInt32($png,20))
$colorType = $png[25]
Add-Test 'png_contract' (($png.Length -ge 26) -and $png[0] -eq 0x89 -and $width -eq 512 -and $height -eq 512 -and $colorType -eq 6) ([ordered]@{width=$width;height=$height;color_type=$colorType;bytes=$png.Length})

$hashFailures = @()
foreach ($line in Get-Content -LiteralPath (Join-Path $validated 'KEY_SHA256SUMS.txt')) {
    if (-not $line.Trim()) { continue }
    if ($line -notmatch '^([0-9A-Fa-f]{64}) \*(.+)$') { $hashFailures += "malformed:$line"; continue }
    $path = Join-Path $validated $Matches[2].Replace('/','\')
    if (-not (Test-Path -LiteralPath $path)) { $hashFailures += "missing:$($Matches[2])"; continue }
    if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash -ne $Matches[1].ToUpperInvariant()) { $hashFailures += "mismatch:$($Matches[2])" }
}
Add-Test 'formal_hash_manifest' ($hashFailures.Count -eq 0) $hashFailures

$sourceBundle = [string]$profile.effects_adapter.validated_sample.bundle
$sourceAsset = [string]$profile.effects_adapter.validated_sample.asset
$windowsPowerShell = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
function Invoke-Rejection([string]$Name, [string[]]$Arguments, [string]$ExpectedOutput) {
    $stdout = Join-Path $Output ($Name + '.stdout.txt')
    $stderr = Join-Path $Output ($Name + '.stderr.txt')
    $process = Start-Process -FilePath $windowsPowerShell -ArgumentList $Arguments -Wait -PassThru -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    Add-Test $Name (($process.ExitCode -ne 0) -and (-not (Test-Path -LiteralPath $ExpectedOutput))) ([ordered]@{exit_code=$process.ExitCode;output_created=(Test-Path -LiteralPath $ExpectedOutput);stdout=$stdout;stderr=$stderr})
}

$pvzOutput = Join-Path $Output 'forbidden_pvz2_output'
Invoke-Rejection 'pvz2_effects_rejected_without_output' @('-NoProfile','-ExecutionPolicy','Bypass','-File',$cliPath,'effects','-Profile','pvz2','-InputBundle',$sourceBundle,'-EffectAsset',$sourceAsset,'-Output',$pvzOutput) $pvzOutput
$oldOutput = Join-Path $Output 'forbidden_legacy_output'
Invoke-Rejection 'old_ulpianus_route_rejected_without_output' @('-NoProfile','-ExecutionPolicy','Bypass','-File',$cliPath,'ulpianus-s2','-Profile','arknights','-Output',$oldOutput) $oldOutput

$passedCount = @($tests | Where-Object passed).Count
$report = [ordered]@{
    schema_version = 1
    created_utc = [DateTime]::UtcNow.ToString('o')
    status = if ($passedCount -eq $tests.Count) { 'passed' } else { 'failed' }
    validated_output = $validated
    test_count = $tests.Count
    passed = $passedCount
    failed = $tests.Count - $passedCount
    tests = $tests
}
$reportPath = Join-Path $Output 'effects_adapter_tests.json'
[IO.File]::WriteAllText($reportPath, (($report | ConvertTo-Json -Depth 12) + "`n"), [Text.UTF8Encoding]::new($false))
$hashes = foreach ($file in Get-ChildItem -LiteralPath $Output -File | Where-Object Name -ne 'KEY_SHA256SUMS.txt' | Sort-Object Name) {
    '{0} *{1}' -f (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash, $file.Name
}
[IO.File]::WriteAllText((Join-Path $Output 'KEY_SHA256SUMS.txt'), (($hashes -join "`n") + "`n"), [Text.UTF8Encoding]::new($false))
Write-Output ($report | ConvertTo-Json -Depth 12)
if ($report.status -ne 'passed') { exit 1 }
exit 0
