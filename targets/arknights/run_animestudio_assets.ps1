param(
    [string]$ProfilePath = '',
    [Parameter(Mandatory=$true)][string]$InputBundle,
    [string[]]$Types = @(),
    [string]$TypesCsv = '',
    [ValidateSet('ByType','ByContainer','BySource','None')][string]$GroupAssets = 'ByType',
    [ValidateSet('Convert','Dump','JSON','Raw')][string]$ExportType = 'Convert',
    [string]$Output = '',
    [int]$TimeoutSeconds = 120,
    [switch]$NoDependencies
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
if (-not $ProfilePath) { $ProfilePath = Join-Path $root 'targets\arknights\profile.json' }
. (Join-Path $root 'scripts\common.ps1')
. (Join-Path $PSScriptRoot 'animestudio_export_validation.ps1')

$profileFile = (Resolve-Path -LiteralPath $ProfilePath).Path
$profile = Get-Content -LiteralPath $profileFile -Raw -Encoding UTF8 | ConvertFrom-Json
if ($profile.profile_id -ne 'arknights' -or -not $profile.assets_adapter.enabled) {
    throw 'The supplied profile does not enable the Arknights AnimeStudio assets adapter.'
}

$capture = (Resolve-Path -LiteralPath $profile.storage.capture).Path
$bundleRoot = $capture
$resolvedInput = (Resolve-Path -LiteralPath $InputBundle).Path
$bundlePrefix = $bundleRoot.TrimEnd('\') + '\'
if (-not $resolvedInput.StartsWith($bundlePrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Input must remain inside the retained Arknights capture Bundle tree: $bundleRoot"
}
if ([IO.Path]::GetExtension($resolvedInput) -ne '.ab') { throw 'Input must be one .ab Bundle.' }
if ($TypesCsv) { $Types += @($TypesCsv -split ',') }
if ($Types.Count -eq 0) { $Types = @($profile.assets_adapter.export.default_types | ForEach-Object { [string]$_ }) }
$Types = @($Types | ForEach-Object { [string]$_ } | Where-Object { $_ } | Select-Object -Unique)

$implemented = @($profile.assets_adapter.export.source_convert_implementations | ForEach-Object { [string]$_ })
$unsupported = @($Types | Where-Object { $_ -notin $implemented })
if (-not $Output) { $Output = Join-Path $root ("reports\arknights_animestudio_assets_{0}" -f (Get-Date -Format 'yyyyMMdd_HHmmss')) }
if (Test-Path -LiteralPath $Output) { throw "Refusing existing adapter output: $Output" }
New-Item -ItemType Directory -Path $Output | Out-Null
$logs = Join-Path $Output 'logs'
$exportRoot = Join-Path $Output 'exports'

$sourceTool = (Resolve-Path -LiteralPath $profile.assets_adapter.tool_path).Path
$sourceToolDirectory = Split-Path -Parent $sourceTool
$tool = $sourceTool
$toolDirectory = $sourceToolDirectory
$runtimeDependencies = @()
$bridgeFiles = @()
$dependencyResolution = [ordered]@{
    requested = (-not $NoDependencies)
    status = if ($NoDependencies) { 'disabled_by_caller' } else { 'pending' }
    method = 'AnimeStudio compiled CLI CABMap option loading a merged Phase 1 map'
    compiled_cli_option = 'CABMap'
    source_enum_note = 'The retained executable loads an existing map for CABMap; this was verified from process output and differs from the adjacent current source enum semantics.'
    map_report = $null
    map_path = $null
    command = $null
}
if ($profile.assets_adapter.PSObject.Properties.Name -contains 'runtime_dependencies' -and @($profile.assets_adapter.runtime_dependencies).Count -gt 0) {
    $toolDirectory = Join-Path $Output 'tool_env\AnimeStudio.CLI'
    Copy-Item -LiteralPath $sourceToolDirectory -Destination $toolDirectory -Recurse -Force
    $tool = Join-Path $toolDirectory (Split-Path -Leaf $sourceTool)
    foreach ($dependency in @($profile.assets_adapter.runtime_dependencies)) {
        $source = (Resolve-Path -LiteralPath ([string]$dependency.source)).Path
        $target = Join-Path $toolDirectory ([string]$dependency.relative_target)
        New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
        Copy-Item -LiteralPath $source -Destination $target -Force
        $runtimeDependencies += [ordered]@{
            source=$source
            source_sha256=Get-Sha256 $source
            target=$target
            target_sha256=Get-Sha256 $target
            purpose=[string]$dependency.purpose
            isolated_copy=$true
        }
    }
}
$bridge = $null
if (-not $NoDependencies -and $profile.assets_adapter.PSObject.Properties.Name -contains 'phase2b_bridge') {
    $bridge = $profile.assets_adapter.phase2b_bridge
    $bridgeRoot = (Resolve-Path -LiteralPath ([string]$bridge.build_output)).Path
    foreach ($file in Get-ChildItem -LiteralPath $bridgeRoot -File -Filter 'AnimeStudio.Phase2B.*') {
        $target = Join-Path $toolDirectory $file.Name
        Copy-Item -LiteralPath $file.FullName -Destination $target -Force
        $bridgeFiles += [ordered]@{source=$file.FullName;source_sha256=Get-Sha256 $file.FullName;target=$target;target_sha256=Get-Sha256 $target}
    }
    $bridgeExecutable = Join-Path $toolDirectory ([string]$bridge.executable)
    if (-not (Test-Path -LiteralPath $bridgeExecutable -PathType Leaf)) { throw "Phase 2B bridge executable missing after copy: $bridgeExecutable" }
    $tool = $bridgeExecutable
}
$dependencyMapHashPaths = @()
if (-not $NoDependencies) {
    $mapConfig = $profile.assets_adapter.dependency_map_sources
    $mapDirectory = Join-Path $toolDirectory 'Maps'
    New-Item -ItemType Directory -Path $mapDirectory -Force | Out-Null
    $mapPath = Join-Path $mapDirectory 'arknights_capture.bin'
    $mapReport = Join-Path $Output 'dependency_map.json'
    $mapArguments = @(
        [string]$mapConfig.builder,
        '--inventory-root', [string]$mapConfig.phase1_inventory_root,
        '--bundle-root', $bundleRoot,
        '--output-map', $mapPath,
        '--report', $mapReport
    )
    $mapResult = Invoke-CapturedCommand -FilePath ([string]$mapConfig.python) -Arguments $mapArguments -WorkingDirectory $Output -TimeoutSeconds 60
    $mapCommand = Save-CommandResult $mapResult $logs 'dependency_map_build'
    $dependencyResolution.command = $mapCommand
    $dependencyResolution.map_report = $mapReport
    if ($mapResult.exit_code -eq 0 -and -not $mapResult.launch_error -and (Test-Path -LiteralPath $mapPath)) {
        $dependencyResolution.status = 'ready'
        $dependencyResolution.map_path = $mapPath
        $dependencyMapHashPaths += @($mapPath,$mapReport,$mapCommand.stdout_path,$mapCommand.stderr_path,$mapCommand.command_record)
    } else {
        $dependencyResolution.status = 'failed'
        $dependencyMapHashPaths += @($mapReport,$mapCommand.stdout_path,$mapCommand.stderr_path,$mapCommand.command_record) | Where-Object { Test-Path -LiteralPath $_ }
    }
}
$result = $null
$command = $null
if ($unsupported.Count -eq 0) {
    $arguments = @(
        $resolvedInput,
        $exportRoot,
        '--game', [string]$profile.assets_adapter.game,
        '--group_assets', $GroupAssets,
        '--export_type', $ExportType
    )
    if ($dependencyResolution.status -eq 'ready') {
        $arguments += @('--dependency_map','arknights_capture')
    }
    $arguments += @('--types') + $Types
    $result = Invoke-CapturedCommand -FilePath $tool -Arguments $arguments -WorkingDirectory $toolDirectory -TimeoutSeconds $TimeoutSeconds
    $command = Save-CommandResult $result $logs 'animestudio_export'
} else {
    New-Item -ItemType Directory -Path $logs | Out-Null
    $stdoutPath = Join-Path $logs 'animestudio_export.stdout.txt'
    $stderrPath = Join-Path $logs 'animestudio_export.stderr.txt'
    [IO.File]::WriteAllText($stdoutPath,'',[Text.UTF8Encoding]::new($false))
    [IO.File]::WriteAllText($stderrPath,("Unsupported Convert type request: {0}" -f ($unsupported -join ', ')),[Text.UTF8Encoding]::new($false))
    $command = [ordered]@{ executable=$tool; arguments=@(); command_line=$null; started_utc=$null; duration_ms=0; exit_code=$null; launch_error='unsupported_type_request'; stdout_path=$stdoutPath; stderr_path=$stderrPath; command_record=$null }
}

$stdout = if ($result) { [string]$result.stdout } else { '' }
$stderr = if ($result) { [string]$result.stderr } else { Get-Content -LiteralPath $command.stderr_path -Raw -Encoding UTF8 }
$logSummary = Get-AnimeStudioLogSummary -Stdout $stdout -Stderr $stderr
$files = @(Get-AnimeStudioExportFileRecords -ExportRoot $exportRoot -SourceBundle $resolvedInput -RequestedTypes $Types -GroupAssets $GroupAssets -BundleRoot $bundleRoot)
$invalidFiles = @($files | Where-Object { -not $_.valid })
$hasErrors = $logSummary.error_line_count -gt 0 -or $invalidFiles.Count -gt 0
$missingResourceLines = @((@($stdout,$stderr) -join "`n") -split "`r?`n" | Where-Object { $_ -match "(?i)can't find the resource file|filenotfoundexception.*resource" })
$typeStatus = @()
foreach ($type in $Types) {
    $typeFiles = @($files | Where-Object { $_.unity_type -eq $type })
    $typeInvalid = @($typeFiles | Where-Object { -not $_.valid })
    $typeErrorCount = @($logSummary.error_lines | Where-Object { $_ -match ("(?i)Export\s+{0}:" -f [regex]::Escape($type)) }).Count
    $state = if ($type -in $unsupported) {
        'unsupported'
    } elseif ($typeFiles.Count -gt 0 -and $typeInvalid.Count -gt 0) {
        'partial'
    } elseif ($typeFiles.Count -gt 0 -and $typeErrorCount -gt 0) {
        'partial'
    } elseif ($typeFiles.Count -gt 0) {
        'exported'
    } elseif ($typeErrorCount -gt 0) {
        'failed'
    } else {
        'not_observed_or_not_extractable'
    }
    $typeStatus += [ordered]@{ type=$type; status=$state; output_file_count=$typeFiles.Count; invalid_file_count=$typeInvalid.Count; error_count=$typeErrorCount }
}

$dependencyCommandOk = $NoDependencies -or ($dependencyResolution.status -eq 'ready' -and $stdout -match 'Loaded arknights_capture' -and $stdout -match 'Resolving Dependencies' -and $stdout -match '\[Phase2B\] Exporting with dependency resolution retained in-process')
$commandOk = $result -and $result.exit_code -eq 0 -and -not $result.launch_error -and $logSummary.scan_started -and -not $logSummary.invalid_type_log_present -and $dependencyCommandOk
$hasFiles = $files.Count -gt 0
$status = if ($unsupported.Count -gt 0) {
    'unsupported_type'
} elseif ($hasFiles -and (-not $commandOk -or $hasErrors -or $logSummary.reported_skipped_assets -gt 0)) {
    'partial'
} elseif (-not $commandOk) {
    'failed'
} elseif (-not $hasFiles -and $hasErrors) {
    'failed'
} elseif (-not $hasFiles -and $logSummary.nothing_exported_log_present) {
    'no_output'
} elseif (-not $hasFiles) {
    'failed'
} else {
    'exported'
}

$report = [ordered]@{
    schema_version = 2
    status = $status
    capability_status = if ($status -eq 'exported') { 'supported_for_this_bundle_and_types' } elseif ($status -eq 'partial') { 'partial' } elseif ($status -eq 'unsupported_type') { 'unsupported' } else { $status }
    adapter = 'arknights_animestudio_assets_export'
    mode = 'export'
    input = [ordered]@{
        path = $resolvedInput
        relative_to_bundle_root = $resolvedInput.Substring($bundlePrefix.Length).Replace('\','/')
        size = (Get-Item -LiteralPath $resolvedInput).Length
        sha256 = Get-Sha256 $resolvedInput
        bundle_root = $bundleRoot
        inside_retained_capture = $true
    }
    request = [ordered]@{ types=$Types; group_assets=$GroupAssets; export_type=$ExportType; timeout_seconds=$TimeoutSeconds }
    unsupported_requested_types = $unsupported
    tool = [ordered]@{
        path=$tool
        sha256=Get-Sha256 $tool
        source_path=$sourceTool
        source_sha256=Get-Sha256 $sourceTool
        working_directory=$toolDirectory
        isolated_runtime=($tool -ne $sourceTool)
        runtime_dependencies=$runtimeDependencies
        phase2b_bridge_files=$bridgeFiles
    }
    dependency_resolution = $dependencyResolution
    streamed_data_resolution = [ordered]@{
        strategy='AnimeStudio ResourceReader embedded reader, then recursive lookup by resource file name below the source assets-file directory'
        missing_resource_error_count=$missingResourceLines.Count
        missing_resource_error_lines=$missingResourceLines
        runtime_verified=$false
    }
    command = $command
    log_summary = $logSummary
    output = [ordered]@{
        root = $exportRoot
        file_count = $files.Count
        invalid_file_count = $invalidFiles.Count
        files = $files
    }
    type_status = $typeStatus
    regression_baseline_applied = $false
    limitations = @(
        'This result applies only to the input Bundle and requested types.',
        'A requested type with no output is not claimed absent; it remains not observed or not extractable in this run.',
        'No runtime address or game behavior is validated by static asset export.'
    )
}
$reportPath = Join-Path $Output 'asset_analysis.json'
$report | ConvertTo-Json -Depth 16 | Set-Content -LiteralPath $reportPath -Encoding UTF8

$hashPaths = @($profileFile,$PSCommandPath,(Join-Path $PSScriptRoot 'animestudio_export_validation.ps1'),(Join-Path $PSScriptRoot 'build_dependency_map.py'),$resolvedInput,$sourceTool,$tool,$command.stdout_path,$command.stderr_path,$reportPath)
$hashPaths += $dependencyMapHashPaths
$hashPaths += @($runtimeDependencies | ForEach-Object { $_.target })
$hashPaths += @($bridgeFiles | ForEach-Object { $_.source; $_.target })
if ($command.command_record) { $hashPaths += $command.command_record }
$hashPaths += @($files | ForEach-Object { $_.output_path })
$(foreach ($path in $hashPaths | Select-Object -Unique) { "$(Get-Sha256 $path)  $path" }) |
    Set-Content -LiteralPath (Join-Path $Output 'KEY_SHA256SUMS.txt') -Encoding UTF8

$report | ConvertTo-Json -Depth 6 -Compress | Write-Output
if ($status -notin @('exported','no_output','partial')) { exit 4 }
exit 0
