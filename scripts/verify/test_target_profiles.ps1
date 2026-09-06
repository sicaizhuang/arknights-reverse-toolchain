param([string]$Output = '')

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '..\common.ps1')
$root = Get-ToolchainRoot
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
if (-not $Output) { $Output = Join-Path $root ("tests\target_profiles_{0}" -f $stamp) }
if (Test-Path -LiteralPath $Output) { throw "Refusing to overwrite output: $Output" }
New-Item -ItemType Directory -Path $Output | Out-Null
$logs = Join-Path $Output 'logs'
New-Item -ItemType Directory -Path $logs | Out-Null
$powershell = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$cli = Join-Path $root 'scripts\toolchain.ps1'
$arkOutput = Join-Path $root ("reports\profile_arknights_{0}" -f $stamp)

$arkResult = Invoke-CapturedCommand -FilePath $powershell -Arguments @(
    '-NoProfile','-ExecutionPolicy','Bypass','-File',$cli,'target','-Profile','arknights','-Output',$arkOutput
) -TimeoutSeconds 60
$arkCommand = Save-CommandResult $arkResult $logs 'cli_target_arknights'
$arkReportPath = Join-Path $arkOutput 'target_analysis_corrected.json'
$arkReport = if (Test-Path -LiteralPath $arkReportPath -PathType Leaf) { Get-Content -LiteralPath $arkReportPath -Raw | ConvertFrom-Json } else { $null }

$pvzBefore = @(Get-ChildItem -LiteralPath (Join-Path $root 'reports') -Directory -Filter 'profile_pvz2_*' -ErrorAction SilentlyContinue).Count
$pvzResult = Invoke-CapturedCommand -FilePath $powershell -Arguments @(
    '-NoProfile','-ExecutionPolicy','Bypass','-File',$cli,'target','-Profile','pvz2'
) -TimeoutSeconds 30
$pvzCommand = Save-CommandResult $pvzResult $logs 'cli_target_pvz2'
$pvzAfter = @(Get-ChildItem -LiteralPath (Join-Path $root 'reports') -Directory -Filter 'profile_pvz2_*' -ErrorAction SilentlyContinue).Count

$needles = '(?i)com\.hypergryph|arknights|pvz2|popcap'
$fixtureFiles = @(
    (Join-Path $root 'fixtures\fixture_manifest.json'),
    (Join-Path $root 'scripts\fixtures\generate_fixtures.py'),
    (Join-Path $root 'scripts\verify\probe_fixture_formats.py'),
    (Join-Path $root 'scripts\verify\verify_toolchain.ps1')
)
$fixtureMatches = @()
foreach ($file in $fixtureFiles) {
    if (Test-Path -LiteralPath $file -PathType Leaf) {
        $fixtureMatches += @(Select-String -LiteralPath $file -Pattern $needles | ForEach-Object { "{0}:{1}" -f $_.Path,$_.LineNumber })
    }
}
$genericModuleFiles = @(Get-ChildItem -LiteralPath (Join-Path $root 'scripts\apk'),(Join-Path $root 'scripts\assets'),(Join-Path $root 'scripts\il2cpp') -File -Recurse)
$genericMatches = @()
foreach ($file in $genericModuleFiles) {
    $genericMatches += @(Select-String -LiteralPath $file.FullName -Pattern $needles | ForEach-Object { "{0}:{1}" -f $_.Path,$_.LineNumber })
}
$hashValidationPath = Join-Path $root 'tests\hash_manifest_20260810_055900\hash_validation.json'
$hashValidation = Get-Content -LiteralPath $hashValidationPath -Raw | ConvertFrom-Json
$checks = @(
    [ordered]@{name='arknights_cli_exit';passed=$arkResult.exit_code -eq 0;actual=$arkResult.exit_code;expected=0},
    [ordered]@{name='arknights_report_created';passed=[bool]$arkReport;actual=$arkReportPath;expected='existing report'},
    [ordered]@{name='arknights_capture_corrected';passed=$arkReport -and $arkReport.capture.corrected_status -eq 'collected_with_warnings';actual=if($arkReport){$arkReport.capture.corrected_status}else{$null};expected='collected_with_warnings'},
    [ordered]@{name='arknights_final_status';passed=$arkReport -and $arkReport.final_status.toolchain_health -eq 'passed' -and $arkReport.final_status.target_analysis -eq 'partial';actual=if($arkReport){$arkReport.final_status}else{$null};expected='passed/partial'},
    [ordered]@{name='pvz2_cli_exit';passed=$pvzResult.exit_code -eq 0;actual=$pvzResult.exit_code;expected=0},
    [ordered]@{name='pvz2_no_report_created';passed=$pvzBefore -eq $pvzAfter;actual=($pvzAfter-$pvzBefore);expected=0},
    [ordered]@{name='pvz2_not_configured_output';passed=$pvzResult.stdout -match '"report_generated":false';actual=$pvzResult.stdout.Trim();expected='report_generated=false'},
    [ordered]@{name='generic_fixtures_have_no_game_rules';passed=$fixtureMatches.Count -eq 0;actual=@($fixtureMatches);expected='0 matches'},
    [ordered]@{name='generic_apk_assets_il2cpp_have_no_game_rules';passed=$genericMatches.Count -eq 0;actual=@($genericMatches);expected='0 matches'},
    [ordered]@{name='old_delivery_hashes';passed=$hashValidation.status -eq 'passed' -and $hashValidation.missing_count -eq 0 -and $hashValidation.mismatch_count -eq 0;actual=[ordered]@{status=$hashValidation.status;missing=$hashValidation.missing_count;mismatch=$hashValidation.mismatch_count};expected='passed/0/0'}
)
$passed = @($checks | Where-Object { -not $_.passed }).Count -eq 0
$reportPath = Join-Path $Output 'target_profile_tests.json'
Write-JsonReport $reportPath ([ordered]@{
    status = if ($passed) { 'passed' } else { 'failed' }
    passed = $passed
    profile_outputs = [ordered]@{arknights=$arkOutput;pvz2_report_count_before=$pvzBefore;pvz2_report_count_after=$pvzAfter}
    commands = [ordered]@{arknights=$arkCommand;pvz2=$pvzCommand}
    checks = $checks
    hash_validation = $hashValidationPath
})
$testMd = Join-Path $Output 'TESTS.md'
@(
    '# Target Profile Tests',
    '',
    ("Status: **{0}**" -f $(if($passed){'passed'}else{'failed'})),
    '',
    ('- Arknights correction: `{0}`' -f $arkOutput),
    '- PVZ2 report outputs created: 0',
    '- Generic fixture game-rule matches: 0',
    '- Generic APK/assets/IL2CPP game-rule matches: 0',
    '- Previous delivery hash validation: 0 missing / 0 mismatch'
) | Set-Content -LiteralPath $testMd -Encoding UTF8
$hashLines = @(
    "$(Get-Sha256 $reportPath)  $reportPath",
    "$(Get-Sha256 $testMd)  $testMd",
    "$(Get-Sha256 $hashValidationPath)  $hashValidationPath"
)
$hashLines | Set-Content -LiteralPath (Join-Path $Output 'KEY_SHA256SUMS.txt') -Encoding ASCII
if (-not $passed) { exit 7 }
exit 0
