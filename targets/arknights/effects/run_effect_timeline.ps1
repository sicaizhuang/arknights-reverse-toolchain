param(
    [Parameter(Mandatory=$true)][string]$ProfilePath,
    [Parameter(Mandatory=$true)][string]$InputBundle,
    [Parameter(Mandatory=$true)][string]$EffectAsset,
    [double]$DurationSeconds = 0,
    [int]$Fps = 30,
    [int]$MaxFrames = 300,
    [int]$MinimumFrames = 30,
    [int]$EmptyTailFrames = 15,
    [double]$PrewarmRatio = 0.6,
    [string]$MotionStart = '',
    [string]$MotionEnd = '',
    [string]$MotionSource = '',
    [string]$Output = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-Utf8NoBom([string]$Path, [string]$Text) {
    [IO.File]::WriteAllText($Path, $Text, [Text.UTF8Encoding]::new($false))
}

function Get-CommandText([string]$Executable, [string[]]$Arguments) {
    $quoted = foreach ($argument in $Arguments) {
        if ($argument -match '[\s"]') { '"' + $argument.Replace('"', '\"') + '"' } else { $argument }
    }
    return (($Executable) + ' ' + ($quoted -join ' ')).Trim()
}

function Invoke-RecordedProcess([string]$Executable, [string[]]$Arguments, [string]$StdoutPath, [string]$StderrPath) {
    $started = [DateTime]::UtcNow
    $process = Start-Process -FilePath $Executable -ArgumentList $Arguments -Wait -PassThru -RedirectStandardOutput $StdoutPath -RedirectStandardError $StderrPath
    return [ordered]@{
        executable=$Executable
        arguments=@($Arguments)
        command_text=Get-CommandText $Executable $Arguments
        started_utc=$started.ToString('o')
        completed_utc=[DateTime]::UtcNow.ToString('o')
        exit_code=$process.ExitCode
        stdout=$StdoutPath
        stderr=$StderrPath
    }
}

$profileFile = (Resolve-Path -LiteralPath $ProfilePath).Path
$profile = Get-Content -LiteralPath $profileFile -Raw | ConvertFrom-Json
if ($profile.profile_id -ne 'arknights') { throw 'The timeline adapter accepts only the isolated Arknights Android profile.' }
if (-not $profile.effects_adapter.enabled) { throw 'The Arknights effects adapter is disabled.' }
if ($DurationSeconds -lt 0 -or $DurationSeconds -gt 120) { throw 'DurationSeconds must be between 0 and 120.' }
if ($Fps -lt 1 -or $Fps -gt 120) { throw 'Fps must be between 1 and 120.' }
if ($MaxFrames -lt 1 -or $MaxFrames -gt 3600) { throw 'MaxFrames must be between 1 and 3600.' }
if ($MinimumFrames -lt 1 -or $MinimumFrames -gt $MaxFrames) { throw 'MinimumFrames must be between 1 and MaxFrames.' }
if ($EmptyTailFrames -lt 1 -or $EmptyTailFrames -gt $MaxFrames) { throw 'EmptyTailFrames must be between 1 and MaxFrames.' }
if ($PrewarmRatio -lt 0 -or $PrewarmRatio -gt 1) { throw 'PrewarmRatio must be between 0 and 1.' }
if (($MotionStart -and -not $MotionEnd) -or ($MotionEnd -and -not $MotionStart)) { throw 'MotionStart and MotionEnd must be provided together.' }
if (($MotionStart -or $MotionEnd) -and -not $MotionSource) { throw 'MotionSource is required for an external projectile motion fixture.' }
$inputFile = (Resolve-Path -LiteralPath $InputBundle).Path

if (-not $Output) {
    $root = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
    $Output = Join-Path $root ('reports\arknights_effect_timeline_' + (Get-Date -Format 'yyyyMMdd_HHmmss'))
}
$outputFull = [IO.Path]::GetFullPath($Output)
if (Test-Path -LiteralPath $outputFull) { throw "Output already exists: $outputFull" }
$temporary = Join-Path ([IO.Path]::GetTempPath()) ('arknights_effect_timeline_' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $temporary | Out-Null

try {
    $stageOut = Join-Path $temporary 'stage.stdout.txt'
    $stageErr = Join-Path $temporary 'stage.stderr.txt'
    $stageArgs = @(
        [string]$profile.effects_adapter.preparer,
        '--profile',$profileFile,
        '--input-bundle',$inputFile,
        '--asset',$EffectAsset,
        '--sample-time','0',
        '--mode','timeline',
        '--duration',$DurationSeconds.ToString([Globalization.CultureInfo]::InvariantCulture),
        '--fps',$Fps.ToString(),
        '--max-frames',$MaxFrames.ToString(),
        '--minimum-frames',$MinimumFrames.ToString(),
        '--empty-tail-frames',$EmptyTailFrames.ToString(),
        '--prewarm-ratio',$PrewarmRatio.ToString([Globalization.CultureInfo]::InvariantCulture),
        '--output',$outputFull
    )
    if ($MotionStart) { $stageArgs += @('--motion-start',$MotionStart,'--motion-end',$MotionEnd,'--motion-source',$MotionSource) }
    $stageCommand = Invoke-RecordedProcess ([string]$profile.effects_adapter.python) $stageArgs $stageOut $stageErr
    if (-not (Test-Path -LiteralPath $outputFull)) { New-Item -ItemType Directory -Path $outputFull | Out-Null }
    $logs = Join-Path $outputFull 'logs'
    New-Item -ItemType Directory -Path $logs -Force | Out-Null
    Copy-Item -LiteralPath $stageOut -Destination (Join-Path $logs 'stage.stdout.txt') -Force
    Copy-Item -LiteralPath $stageErr -Destination (Join-Path $logs 'stage.stderr.txt') -Force
    $stageCommand.stdout = Join-Path $logs 'stage.stdout.txt'
    $stageCommand.stderr = Join-Path $logs 'stage.stderr.txt'
    Write-Utf8NoBom (Join-Path $logs 'stage.command.json') (($stageCommand|ConvertTo-Json -Depth 8)+"`n")
    if ($stageCommand.exit_code -ne 0) { exit $stageCommand.exit_code }

    $stagePath = Join-Path $outputFull 'effect_stage.json'
    $stage = Get-Content -LiteralPath $stagePath -Raw | ConvertFrom-Json
    if ($stage.status -ne 'passed' -or $stage.render_mode -ne 'timeline') { throw "Timeline staging did not pass: $($stage.status)/$($stage.render_mode)" }

    $unityOut = Join-Path $logs 'unity.stdout.txt'
    $unityErr = Join-Path $logs 'unity.stderr.txt'
    $unityLog = Join-Path $logs 'unity.editor.log'
    $unityArgs = @(
        '-batchmode','-force-vulkan',
        '-projectPath',[string]$profile.effects_adapter.unity_project,
        '-executeMethod','ArknightsEffectTimelineRenderer.Run',
        '-stageInput',[string]$stage.unity_input,
        '-driveShaderTime','true',
        '-logFile',$unityLog
    )
    $unityCommand = Invoke-RecordedProcess ([string]$profile.effects_adapter.unity_editor) $unityArgs $unityOut $unityErr
    Write-Utf8NoBom (Join-Path $logs 'unity.command.json') (($unityCommand|ConvertTo-Json -Depth 8)+"`n")
    $renderPath = Join-Path $outputFull 'effect_render.json'
    if ($unityCommand.exit_code -ne 0 -or -not(Test-Path -LiteralPath $renderPath)) { exit 6 }
    $render = Get-Content -LiteralPath $renderPath -Raw | ConvertFrom-Json
    if ($render.status -ne 'passed_timeline_frames') { exit 6 }

    $postOut = Join-Path $logs 'postprocess.stdout.txt'
    $postErr = Join-Path $logs 'postprocess.stderr.txt'
    $postDirectory = Join-Path $outputFull 'preview'
    $postArgs = @(
        [string]$profile.effects_adapter.timeline.postprocessor,
        '--render-report',$renderPath,
        '--output',$postDirectory,
        '--minimum-visible-pixels','300',
        '--trim-pad','2'
    )
    $postCommand = Invoke-RecordedProcess ([string]$profile.effects_adapter.python) $postArgs $postOut $postErr
    Write-Utf8NoBom (Join-Path $logs 'postprocess.command.json') (($postCommand|ConvertTo-Json -Depth 8)+"`n")
    $postPath = Join-Path $postDirectory 'timeline_postprocess.json'
    if ($postCommand.exit_code -ne 0 -or -not(Test-Path -LiteralPath $postPath)) { exit 7 }
    $post = Get-Content -LiteralPath $postPath -Raw | ConvertFrom-Json
    $passed = $post.status -eq 'passed_timeline'

    $report = [ordered]@{
        schema_version=1
        created_utc=[DateTime]::UtcNow.ToString('o')
        status=if($passed){'passed_timeline'}else{'blocked_postprocess'}
        profile='arknights'
        scope='authentic_gameobject_prefab_deterministic_offline_timeline'
        input_bundle=$inputFile
        input_sha256=(Get-FileHash -LiteralPath $inputFile -Algorithm SHA256).Hash
        requested_asset=$EffectAsset
        stage_status=$stage.status
        resolved_cab_count=$stage.resolved_cab_count
        unresolved_cab_count=$stage.unresolved_count
        graphics_device=$render.graphics_device
        fps=$render.fps
        detected_duration_seconds=$render.detected_duration_seconds
        render_duration_seconds=$render.render_duration_seconds
        external_projectile_motion=$render.external_projectile_motion
        motion_source=$render.motion_source
        applied_motion_distance=$render.applied_motion_distance
        max_trail_position_count=$render.max_trail_position_count
        max_trail_path_length=$render.max_trail_path_length
        source_frame_count=$post.source_frame_count
        retained_frame_count=$post.retained_frame_count
        contact_sheet=$post.contact_sheet.path
        gif=$post.gif.path
        authenticity_gates=[ordered]@{
            original_gameobject_prefab=$true
            original_materials_textures_particles=$true
            replacement_shader_used=$false
            generated_texture_used=$false
            synthetic_particle_used=$false
            fixed_particle_seed=$true
            dependency_first_main_last=$true
            prewarmed_bounds=$true
            fixed_camera_z=-50
            alpha_recovery='max_rgb'
        }
        limitations=@(
            'This is a deterministic offline Unity/Vulkan simulation, not an observed in-game runtime call.',
            'GIF palette quantization is a preview derivative; PNG frames remain the lossless evidence.'
        )
    }
    $reportPath = Join-Path $outputFull 'effects_timeline_report.json'
    Write-Utf8NoBom $reportPath (($report|ConvertTo-Json -Depth 10)+"`n")
    $keyFiles=@(
        'effect_stage.json','effect_render.json','effects_timeline_report.json',
        'preview\timeline_postprocess.json','preview\contact_sheet.png','preview\effect_timeline.gif',
        'logs\stage.command.json','logs\stage.stdout.txt','logs\stage.stderr.txt',
        'logs\unity.command.json','logs\unity.stdout.txt','logs\unity.stderr.txt','logs\unity.editor.log',
        'logs\postprocess.command.json','logs\postprocess.stdout.txt','logs\postprocess.stderr.txt'
    )
    $hashLines=foreach($relative in $keyFiles){$path=Join-Path $outputFull $relative;if(Test-Path -LiteralPath $path -PathType Leaf){'{0} *{1}' -f (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash,$relative.Replace('\','/')}}
    Write-Utf8NoBom (Join-Path $outputFull 'KEY_SHA256SUMS.txt') (($hashLines -join "`n")+"`n")
    Write-Output ($report|ConvertTo-Json -Depth 10)
    if(-not $passed){exit 7}
    exit 0
}
finally {
    if(Test-Path -LiteralPath $temporary){Remove-Item -LiteralPath $temporary -Recurse -Force}
}
