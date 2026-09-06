param(
    [string]$ProfilePath = '',
    [Parameter(Mandatory=$true)][string]$InputPath,
    [string[]]$Types = @(),
    [string]$PathPattern = '',
    [int]$MaxBundles = 10,
    [int]$PerBundleTimeoutSeconds = 120,
    [int64]$DiskLimitBytes = 1073741824,
    [switch]$Resume,
    [string]$Output = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
$root=(Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
if(-not $ProfilePath){$ProfilePath=Join-Path $root 'targets\arknights\profile.json'}
. (Join-Path $root 'scripts\common.ps1')
$profileFile=(Resolve-Path -LiteralPath $ProfilePath).Path
$profile=Get-Content -LiteralPath $profileFile -Raw -Encoding UTF8|ConvertFrom-Json
if($profile.profile_id -ne 'arknights'){throw 'Batch export accepts only the Arknights profile.'}
$capture=(Resolve-Path -LiteralPath $profile.storage.capture).Path
$bundleRoot=$capture
$resolvedInput=(Resolve-Path -LiteralPath $InputPath).Path
$prefix=$bundleRoot.TrimEnd('\')+'\'
if(-not($resolvedInput.Equals($bundleRoot,[StringComparison]::OrdinalIgnoreCase)-or $resolvedInput.StartsWith($prefix,[StringComparison]::OrdinalIgnoreCase))){throw "Batch input must remain inside: $bundleRoot"}
if(-not $Output){$Output=Join-Path $root ("reports\arknights_animestudio_batch_{0}" -f (Get-Date -Format 'yyyyMMdd_HHmmss'))}
if(Test-Path -LiteralPath $Output){if(-not $Resume){throw "Refusing existing batch output without -Resume: $Output"}}else{New-Item -ItemType Directory -Path $Output|Out-Null}
$bundles=if(Test-Path -LiteralPath $resolvedInput -PathType Leaf){@(Get-Item -LiteralPath $resolvedInput)}else{@(Get-ChildItem -LiteralPath $resolvedInput -Recurse -File -Filter '*.ab'|Sort-Object FullName)}
if($PathPattern){$bundles=@($bundles|Where-Object{$_.FullName.Substring($prefix.Length).Replace('\','/') -match $PathPattern})}
if($MaxBundles -gt 0){$bundles=@($bundles|Select-Object -First $MaxBundles)}
if($Types.Count -eq 0){$Types=@($profile.assets_adapter.export.default_types|ForEach-Object{[string]$_})}
$checkpointPath=Join-Path $Output 'batch_checkpoint.json'
$results=@()
if($Resume -and (Test-Path -LiteralPath $checkpointPath)){$results=@((Get-Content -LiteralPath $checkpointPath -Raw -Encoding UTF8|ConvertFrom-Json).results)}
$done=@{};foreach($item in $results){$done[[string]$item.relative_path]=$true}
$pwsh="$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$index=0
foreach($bundle in $bundles){
    $index++;$relative=$bundle.FullName.Substring($prefix.Length).Replace('\','/')
    if($done.ContainsKey($relative)){continue}
    $usedFiles=@(Get-ChildItem -LiteralPath $Output -Recurse -File -ErrorAction SilentlyContinue)
    $used=if($usedFiles.Count -eq 0){0}else{[int64](($usedFiles|Measure-Object -Property Length -Sum).Sum)}
    if($used -ge $DiskLimitBytes){$results+=[ordered]@{relative_path=$relative;status='stopped_disk_limit';report=$null;command=$null};break}
    $entryOut=Join-Path $Output ("bundles\{0:D6}" -f $index)
    $args=@('-NoProfile','-ExecutionPolicy','Bypass','-File',(Join-Path $PSScriptRoot 'run_animestudio_assets.ps1'),'-ProfilePath',$profileFile,'-InputBundle',$bundle.FullName,'-Output',$entryOut,'-TimeoutSeconds',[string]$PerBundleTimeoutSeconds,'-TypesCsv',($Types -join ','))
    $run=Invoke-CapturedCommand -FilePath $pwsh -Arguments $args -TimeoutSeconds ($PerBundleTimeoutSeconds+30)
    $command=Save-CommandResult $run (Join-Path $Output 'logs') ("bundle_{0:D6}" -f $index)
    $reportPath=Join-Path $entryOut 'asset_analysis.json'
    $parsed=if(Test-Path -LiteralPath $reportPath){Get-Content -LiteralPath $reportPath -Raw -Encoding UTF8|ConvertFrom-Json}else{$null}
    $results+=[ordered]@{relative_path=$relative;status=if($parsed){$parsed.status}else{'failed'};exit_code=$run.exit_code;report=if($parsed){$reportPath}else{$null};command=$command}
    [ordered]@{schema_version=1;updated_utc=[DateTime]::UtcNow.ToString('o');results=$results}|ConvertTo-Json -Depth 12|Set-Content -LiteralPath $checkpointPath -Encoding UTF8
}
$failed=@($results|Where-Object{$_.status -in @('failed','unsupported_type','stopped_disk_limit')}).Count
$partial=@($results|Where-Object{$_.status -eq 'partial'}).Count
$status=if($failed -gt 0 -or $partial -gt 0){'partial'}else{'completed'}
$report=[ordered]@{schema_version=1;status=$status;mode='bounded_batch_export';input=$resolvedInput;requested_types=$Types;selected_bundle_count=$bundles.Count;processed_count=$results.Count;max_bundles=$MaxBundles;per_bundle_timeout_seconds=$PerBundleTimeoutSeconds;disk_limit_bytes=$DiskLimitBytes;results=$results;limitations=@('Batch export is opt-in and bounded.','No claim is made for Bundles not processed.')}
$reportPath=Join-Path $Output 'batch_report.json';$report|ConvertTo-Json -Depth 14|Set-Content -LiteralPath $reportPath -Encoding UTF8
$hashPaths=@($profileFile,$PSCommandPath,(Join-Path $PSScriptRoot 'run_animestudio_assets.ps1'),$reportPath,$checkpointPath)|Where-Object{Test-Path -LiteralPath $_ -PathType Leaf}
$(foreach($path in $hashPaths|Select-Object -Unique){"$(Get-Sha256 $path)  $path"})|Set-Content -LiteralPath (Join-Path $Output 'KEY_SHA256SUMS.txt') -Encoding UTF8
if($status -eq 'partial'){exit 3};exit 0
