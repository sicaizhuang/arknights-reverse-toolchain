param(
    [Parameter(Mandatory=$true)]
    [string]$ProfilePath,
    [Parameter(Mandatory=$true)]
    [string]$InputBundle,
    [Parameter(Mandatory=$true)]
    [string]$EffectAsset,
    [double]$SampleTimeSeconds = 0.1,
    [string]$Output = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-Utf8NoBom {
    param([Parameter(Mandatory=$true)][string]$Path, [Parameter(Mandatory=$true)][string]$Text)
    [IO.File]::WriteAllText($Path, $Text, [Text.UTF8Encoding]::new($false))
}

function Get-CommandText {
    param([Parameter(Mandatory=$true)][string]$Executable, [Parameter(Mandatory=$true)][string[]]$Arguments)
    $quoted = foreach ($argument in $Arguments) {
        if ($argument -match '[\s"]') { '"' + $argument.Replace('"', '\"') + '"' } else { $argument }
    }
    return (($Executable) + ' ' + ($quoted -join ' ')).Trim()
}

function Invoke-RecordedProcess {
    param(
        [Parameter(Mandatory=$true)][string]$Executable,
        [Parameter(Mandatory=$true)][string[]]$Arguments,
        [Parameter(Mandatory=$true)][string]$StdoutPath,
        [Parameter(Mandatory=$true)][string]$StderrPath
    )
    $started = [DateTime]::UtcNow
    $process = Start-Process -FilePath $Executable -ArgumentList $Arguments -Wait -PassThru `
        -RedirectStandardOutput $StdoutPath -RedirectStandardError $StderrPath
    return [ordered]@{
        executable = $Executable
        arguments = @($Arguments)
        command_text = Get-CommandText -Executable $Executable -Arguments $Arguments
        started_utc = $started.ToString('o')
        completed_utc = [DateTime]::UtcNow.ToString('o')
        exit_code = $process.ExitCode
        stdout = $StdoutPath
        stderr = $StderrPath
    }
}

$profileFile = (Resolve-Path -LiteralPath $ProfilePath).Path
$profile = Get-Content -LiteralPath $profileFile -Raw | ConvertFrom-Json
if ($profile.profile_id -ne 'arknights') { throw 'The effects adapter accepts only the isolated Arknights Android profile.' }
if (-not $profile.effects_adapter.enabled) { throw 'The Arknights effects adapter is disabled.' }
if ($SampleTimeSeconds -lt 0 -or $SampleTimeSeconds -gt 60) { throw 'SampleTimeSeconds must be between 0 and 60.' }

$inputFile = (Resolve-Path -LiteralPath $InputBundle).Path
if (-not $Output) {
    $root = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
    $Output = Join-Path $root ('reports\arknights_effects_' + (Get-Date -Format 'yyyyMMdd_HHmmss'))
}
$outputFull = [IO.Path]::GetFullPath($Output)
if (Test-Path -LiteralPath $outputFull) { throw "Output already exists: $outputFull" }

$temporary = Join-Path ([IO.Path]::GetTempPath()) ('arknights_effects_' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $temporary | Out-Null
$commands = @()
try {
    $stageStdout = Join-Path $temporary 'stage.stdout.txt'
    $stageStderr = Join-Path $temporary 'stage.stderr.txt'
    $stageArguments = @(
        [string]$profile.effects_adapter.preparer,
        '--profile', $profileFile,
        '--input-bundle', $inputFile,
        '--asset', $EffectAsset,
        '--sample-time', $SampleTimeSeconds.ToString([Globalization.CultureInfo]::InvariantCulture),
        '--output', $outputFull
    )
    $stageCommand = Invoke-RecordedProcess -Executable ([string]$profile.effects_adapter.python) -Arguments $stageArguments -StdoutPath $stageStdout -StderrPath $stageStderr
    $commands += $stageCommand

    if (-not (Test-Path -LiteralPath $outputFull)) { New-Item -ItemType Directory -Path $outputFull | Out-Null }
    $logs = Join-Path $outputFull 'logs'
    New-Item -ItemType Directory -Path $logs -Force | Out-Null
    Copy-Item -LiteralPath $stageStdout -Destination (Join-Path $logs 'stage.stdout.txt') -Force
    Copy-Item -LiteralPath $stageStderr -Destination (Join-Path $logs 'stage.stderr.txt') -Force
    $stageCommand.stdout = Join-Path $logs 'stage.stdout.txt'
    $stageCommand.stderr = Join-Path $logs 'stage.stderr.txt'
    Write-Utf8NoBom -Path (Join-Path $logs 'stage.command.json') -Text (($stageCommand | ConvertTo-Json -Depth 8) + "`n")

    if ($stageCommand.exit_code -ne 0) {
        Write-Utf8NoBom -Path (Join-Path $outputFull 'effects_adapter_report.json') -Text (([ordered]@{
            schema_version = 1
            created_utc = [DateTime]::UtcNow.ToString('o')
            status = 'blocked_stage_preparation'
            profile = 'arknights'
            input_bundle = $inputFile
            requested_asset = $EffectAsset
            stage_exit_code = $stageCommand.exit_code
            render_started = $false
        } | ConvertTo-Json -Depth 8) + "`n")
        exit $stageCommand.exit_code
    }

    $stageReportPath = Join-Path $outputFull 'effect_stage.json'
    $stageReport = Get-Content -LiteralPath $stageReportPath -Raw | ConvertFrom-Json
    if ($stageReport.status -ne 'passed') { throw "Effect staging did not pass: $($stageReport.status)" }

    $unityStdout = Join-Path $logs 'unity.stdout.txt'
    $unityStderr = Join-Path $logs 'unity.stderr.txt'
    $unityLog = Join-Path $logs 'unity.editor.log'
    $unityArguments = @(
        '-batchmode',
        '-force-vulkan',
        '-projectPath', [string]$profile.effects_adapter.unity_project,
        '-executeMethod', 'ArknightsEffectRenderer.Run',
        '-stageInput', [string]$stageReport.unity_input,
        '-logFile', $unityLog
    )
    $unityCommand = Invoke-RecordedProcess -Executable ([string]$profile.effects_adapter.unity_editor) -Arguments $unityArguments -StdoutPath $unityStdout -StderrPath $unityStderr
    $commands += $unityCommand
    Write-Utf8NoBom -Path (Join-Path $logs 'unity.command.json') -Text (($unityCommand | ConvertTo-Json -Depth 8) + "`n")

    $renderReportPath = Join-Path $outputFull 'effect_render.json'
    $render = if (Test-Path -LiteralPath $renderReportPath) { Get-Content -LiteralPath $renderReportPath -Raw | ConvertFrom-Json } else { $null }
    $imagePath = Join-Path $outputFull 'effect_frame.png'
    $pngValid = $false
    if (Test-Path -LiteralPath $imagePath) {
        $header = [IO.File]::ReadAllBytes($imagePath)
        $pngValid = $header.Length -ge 24 -and $header[0] -eq 0x89 -and $header[1] -eq 0x50 -and $header[2] -eq 0x4E -and $header[3] -eq 0x47
    }
    $passed = $unityCommand.exit_code -eq 0 -and $null -ne $render -and $render.status -eq 'passed_single_frame' -and $pngValid
    $status = if ($passed) { 'passed_single_frame' } else { 'blocked_render' }
    $report = [ordered]@{
        schema_version = 1
        created_utc = [DateTime]::UtcNow.ToString('o')
        status = $status
        profile = 'arknights'
        scope = 'authentic_gameobject_prefab_single_frame_offline_render'
        input_bundle = $inputFile
        input_sha256 = (Get-FileHash -LiteralPath $inputFile -Algorithm SHA256).Hash
        requested_asset = $EffectAsset
        sample_time_seconds = $SampleTimeSeconds
        stage_status = $stageReport.status
        resolved_cab_count = $stageReport.resolved_cab_count
        unresolved_cab_count = $stageReport.unresolved_count
        unity_exit_code = $unityCommand.exit_code
        render_status = if ($null -eq $render) { 'missing_report' } else { $render.status }
        png_signature_valid = $pngValid
        output_image = $imagePath
        render_report = $renderReportPath
        stage_report = $stageReportPath
        authenticity_gates = [ordered]@{
            original_gameobject_prefab = $true
            original_materials_textures_particles = $true
            replacement_shader_used = $false
            generated_texture_used = $false
            synthetic_particle_used = $false
            graphics_api = 'Vulkan'
        }
        limitations = @(
            'This route renders one deterministic sample frame, not a complete effect timeline.',
            'A passed frame proves offline Unity/Vulkan rendering of the exact retained prefab; it does not prove the in-game runtime call site.'
        )
    }
    Write-Utf8NoBom -Path (Join-Path $outputFull 'effects_adapter_report.json') -Text (($report | ConvertTo-Json -Depth 10) + "`n")

    $keyFiles = @(
        'effect_stage.json', 'effect_render.json', 'effect_frame.png', 'effect_frame_full.png', 'effects_adapter_report.json',
        'logs\stage.command.json', 'logs\stage.stdout.txt', 'logs\stage.stderr.txt',
        'logs\unity.command.json', 'logs\unity.stdout.txt', 'logs\unity.stderr.txt', 'logs\unity.editor.log'
    )
    $hashLines = foreach ($relative in $keyFiles) {
        $path = Join-Path $outputFull $relative
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            '{0} *{1}' -f (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash, $relative.Replace('\','/')
        }
    }
    Write-Utf8NoBom -Path (Join-Path $outputFull 'KEY_SHA256SUMS.txt') -Text (($hashLines -join "`n") + "`n")
    Write-Output (Get-Content -LiteralPath (Join-Path $outputFull 'effects_adapter_report.json') -Raw)
    if (-not $passed) { exit 6 }
    exit 0
}
finally {
    if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Recurse -Force }
}
