param(
    [string]$ProfilePath = '',
    [string]$InputPath = '',
    [string[]]$Types = @(),
    [string]$PathPattern = '',
    [switch]$Deep,
    [int]$MaxBundles = 0,
    [int]$PerBundleTimeoutSeconds = 60,
    [int64]$DiskLimitBytes = 1073741824,
    [switch]$Resume,
    [string]$Output = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
if (-not $ProfilePath) { $ProfilePath = Join-Path $root 'targets\arknights\profile.json' }
. (Join-Path $root 'scripts\common.ps1')
. (Join-Path $PSScriptRoot 'animestudio_export_validation.ps1')

$profileFile = (Resolve-Path -LiteralPath $ProfilePath).Path
$profile = Get-Content -LiteralPath $profileFile -Raw -Encoding UTF8 | ConvertFrom-Json
if ($profile.profile_id -ne 'arknights') { throw 'Inventory accepts only the Arknights profile.' }
$capture = (Resolve-Path -LiteralPath $profile.storage.capture).Path
$bundleRoot = $capture
if (-not $InputPath) { $InputPath = $bundleRoot }
$resolvedInput = (Resolve-Path -LiteralPath $InputPath).Path
$prefix = $bundleRoot.TrimEnd('\') + '\'
if (-not ($resolvedInput.Equals($bundleRoot,[StringComparison]::OrdinalIgnoreCase) -or $resolvedInput.StartsWith($prefix,[StringComparison]::OrdinalIgnoreCase))) {
    throw "Inventory input must remain inside: $bundleRoot"
}
if (-not $Output) { $Output = Join-Path $root ("reports\arknights_animestudio_inventory_{0}" -f (Get-Date -Format 'yyyyMMdd_HHmmss')) }
if (Test-Path -LiteralPath $Output) {
    if (-not $Resume) { throw "Refusing existing inventory output without -Resume: $Output" }
} else { New-Item -ItemType Directory -Path $Output | Out-Null }

$candidateFiles = if (Test-Path -LiteralPath $resolvedInput -PathType Leaf) {
    if ([IO.Path]::GetExtension($resolvedInput) -ne '.ab') { throw 'Inventory file input must be one .ab Bundle.' }
    @(Get-Item -LiteralPath $resolvedInput)
} else {
    @(Get-ChildItem -LiteralPath $resolvedInput -Recurse -File -Filter '*.ab' | Sort-Object FullName)
}
if ($PathPattern) { $candidateFiles = @($candidateFiles | Where-Object { $_.FullName.Substring($prefix.Length).Replace('\','/') -match $PathPattern }) }

$captureManifestPath = Join-Path $capture 'capture.json'
$captureManifest = Get-Content -LiteralPath $captureManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
$manifestByRelative = @{}
foreach ($record in @($captureManifest.files | Where-Object { $_.path -match '/Bundles/.+\.ab$' })) {
    $relative = ([string]$record.path -replace '^.*?/Bundles/','').Replace('\','/')
    $manifestByRelative[$relative] = $record
}

$bundleRecords = @()
foreach ($file in $candidateFiles) {
    $relative = $file.FullName.Substring($prefix.Length).Replace('\','/')
    $manifest = if ($manifestByRelative.ContainsKey($relative)) { $manifestByRelative[$relative] } else { $null }
    $provenance = if (-not $manifest) { 'missing_from_capture_manifest' } elseif ([int64]$manifest.size -ne $file.Length) { 'capture_manifest_size_mismatch' } else { 'capture_manifest' }
    $bundleRecords += [pscustomobject][ordered]@{
        relative_path=$relative
        path=$file.FullName
        category=($relative -split '/')[0]
        size=$file.Length
        last_write_utc=$file.LastWriteTimeUtc.ToString('o')
        sha256=if($manifest){[string]$manifest.sha256}else{$null}
        sha256_provenance=$provenance
        type_inventory_status='not_scanned'
        asset_count=$null
        type_counts=$null
        deep_report=$null
    }
}

$selected = @($bundleRecords)
if ($MaxBundles -gt 0) { $selected = @($selected | Select-Object -First $MaxBundles) }
$completedRelative = @{}
$checkpointPath = Join-Path $Output 'inventory_checkpoint.json'
if ($Resume -and (Test-Path -LiteralPath $checkpointPath)) {
    $oldCheckpoint = Get-Content -LiteralPath $checkpointPath -Raw -Encoding UTF8 | ConvertFrom-Json
    foreach ($item in @($oldCheckpoint.results)) { $completedRelative[[string]$item.relative_path] = $item }
}
$deepResults = @($completedRelative.Values)
$tool = (Resolve-Path -LiteralPath $profile.assets_adapter.tool_path).Path
$toolDirectory = Split-Path -Parent $tool

if ($Deep) {
    $index = 0
    foreach ($bundle in $selected) {
        $index++
        if ($completedRelative.ContainsKey([string]$bundle.relative_path)) { continue }
        $usedFiles = @(Get-ChildItem -LiteralPath $Output -Recurse -File -ErrorAction SilentlyContinue)
        $usedBytes = if ($usedFiles.Count -eq 0) { 0 } else { [int64](($usedFiles | Measure-Object -Property Length -Sum).Sum) }
        if ($usedBytes -ge $DiskLimitBytes) {
            $deepResults += [ordered]@{relative_path=$bundle.relative_path;status='stopped_disk_limit';asset_count=$null;type_counts=$null;map_path=$null;command=$null}
            break
        }
        $entryOut = Join-Path $Output ("maps\{0:D6}" -f $index)
        New-Item -ItemType Directory -Path $entryOut -Force | Out-Null
        $arguments = @($bundle.path,$entryOut,'--game',[string]$profile.assets_adapter.game,'--map_op','AssetMap','--map_type','JSON','--map_name','asset_inventory')
        if ($Types.Count -gt 0) { $arguments += '--types'; $arguments += $Types }
        $result = Invoke-CapturedCommand -FilePath $tool -Arguments $arguments -WorkingDirectory $toolDirectory -TimeoutSeconds $PerBundleTimeoutSeconds
        $command = Save-CommandResult $result (Join-Path $entryOut 'logs') 'inventory'
        $mapPath = Join-Path $entryOut 'asset_inventory.json'
        $entries = @()
        $parseError = $null
        if (Test-Path -LiteralPath $mapPath) {
            try { $entries = @((Get-Content -LiteralPath $mapPath -Raw -Encoding UTF8 | ConvertFrom-Json).AssetEntries) } catch { $parseError = $_.Exception.Message }
        }
        $log = Get-AnimeStudioLogSummary -Stdout ([string]$result.stdout) -Stderr ([string]$result.stderr)
        $status = if ($result.launch_error -like 'timed out*') { 'timeout' } elseif ($result.exit_code -ne 0 -or $parseError -or -not (Test-Path -LiteralPath $mapPath)) { 'failed' } elseif ($log.error_line_count -gt 0) { 'partial' } else { 'parsed' }
        $typeCounts = @($entries | Group-Object Type | Sort-Object Name | ForEach-Object { [ordered]@{type=$_.Name;count=$_.Count} })
        $deepRecord = [ordered]@{relative_path=$bundle.relative_path;status=$status;asset_count=$entries.Count;type_counts=$typeCounts;map_path=if(Test-Path -LiteralPath $mapPath){$mapPath}else{$null};parse_error=$parseError;command=$command}
        $deepResults += $deepRecord
        $completedRelative[[string]$bundle.relative_path] = $deepRecord
        [ordered]@{schema_version=1;updated_utc=[DateTime]::UtcNow.ToString('o');results=$deepResults}|ConvertTo-Json -Depth 12|Set-Content -LiteralPath $checkpointPath -Encoding UTF8
    }
}

$deepByRelative=@{}
foreach($item in $deepResults){$deepByRelative[[string]$item.relative_path]=$item}
foreach($bundle in $bundleRecords){if($deepByRelative.ContainsKey([string]$bundle.relative_path)){$item=$deepByRelative[[string]$bundle.relative_path];$bundle.type_inventory_status=$item.status;$bundle.asset_count=$item.asset_count;$bundle.type_counts=$item.type_counts;$bundle.deep_report=$item.map_path}}
$parsedCount=@($deepResults|Where-Object{$_.status -eq 'parsed'}).Count
$failedCount=@($deepResults|Where-Object{$_.status -in @('failed','timeout','partial','stopped_disk_limit')}).Count
$status=if(-not $Deep){'inventory_only'}elseif($failedCount -gt 0){'partial'}else{'completed'}
$report=[ordered]@{
    schema_version=1
    status=$status
    mode=if($Deep){'deep_asset_map'}else{'filesystem_only'}
    input=$resolvedInput
    capture=$capture
    capture_manifest=$captureManifestPath
    bundle_count=$bundleRecords.Count
    selected_for_deep_count=if($Deep){$selected.Count}else{0}
    parsed_count=$parsedCount
    failed_or_stopped_count=$failedCount
    disk_limit_bytes=$DiskLimitBytes
    bundles=$bundleRecords
    deep_results=$deepResults
    limitations=@('Filesystem-only inventory does not claim object types.','Deep inventory is explicit, resumable, timeout bounded, and does not export asset payloads.')
}
$reportPath=Join-Path $Output 'bundle_inventory.json'
$report|ConvertTo-Json -Depth 14|Set-Content -LiteralPath $reportPath -Encoding UTF8
$hashPaths=@($profileFile,$PSCommandPath,(Join-Path $PSScriptRoot 'animestudio_export_validation.ps1'),$captureManifestPath,$reportPath)
$(foreach($path in $hashPaths|Select-Object -Unique){"$(Get-Sha256 $path)  $path"})|Set-Content -LiteralPath (Join-Path $Output 'KEY_SHA256SUMS.txt') -Encoding UTF8
if($status -eq 'partial'){exit 3}
exit 0
