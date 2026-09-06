param(
    [Parameter(Mandatory=$true)][string]$ProfilePath,
    [Parameter(Mandatory=$true)][string]$InputBundle,
    [Parameter(Mandatory=$true)][string]$AssetPattern,
    [string]$ExcludePattern = '#',
    [double]$DurationSeconds = 0,
    [int]$Fps = 30,
    [int]$MaxFrames = 300,
    [int]$MinimumFrames = 30,
    [int]$EmptyTailFrames = 15,
    [double]$PrewarmRatio = 0.6,
    [int]$MaxAssets = 24,
    [int]$PerAssetTimeoutSeconds = 120,
    [string]$Output = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
function Write-Utf8NoBom([string]$Path,[string]$Text){[IO.File]::WriteAllText($Path,$Text,[Text.UTF8Encoding]::new($false))}
function Get-RelativePath([string]$Base,[string]$Path){$baseUri=[Uri]($Base.TrimEnd('\')+'\');[Uri]::UnescapeDataString($baseUri.MakeRelativeUri([Uri]$Path).ToString()).Replace('/','\')}
function Invoke-Process([string]$Executable,[string[]]$Arguments,[string]$Stdout,[string]$Stderr,[int]$TimeoutSeconds){
    $started=[DateTime]::UtcNow
    $process=Start-Process -FilePath $Executable -ArgumentList $Arguments -PassThru -RedirectStandardOutput $Stdout -RedirectStandardError $Stderr
    # Windows PowerShell 5 does not preserve ExitCode after timed WaitForExit
    # unless the native process handle/event plumbing is initialized first.
    $process.EnableRaisingEvents=$true
    $finished=$process.WaitForExit($TimeoutSeconds*1000)
    if($finished){
        # Windows PowerShell 5 can expose a null ExitCode until redirected streams
        # have finished draining and the process object has been refreshed.
        $process.WaitForExit()
        $process.Refresh()
        $exitCode=[int]$process.ExitCode
    }else{
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        $process.WaitForExit()
        $exitCode=-1001
    }
    [ordered]@{executable=$Executable;arguments=@($Arguments);started_utc=$started.ToString('o');completed_utc=[DateTime]::UtcNow.ToString('o');exit_code=$exitCode;timed_out=(-not $finished);stdout=$Stdout;stderr=$Stderr}
}

$profileFile=(Resolve-Path -LiteralPath $ProfilePath).Path
$profile=Get-Content -LiteralPath $profileFile -Raw|ConvertFrom-Json
if($profile.profile_id -ne 'arknights'){throw 'The batch timeline adapter accepts only the isolated Arknights profile.'}
if($MaxAssets -lt 1 -or $MaxAssets -gt 100){throw 'MaxAssets must be between 1 and 100.'}
if($PerAssetTimeoutSeconds -lt 30 -or $PerAssetTimeoutSeconds -gt 600){throw 'PerAssetTimeoutSeconds must be between 30 and 600.'}
$inputFile=(Resolve-Path -LiteralPath $InputBundle).Path
if(-not $Output){$root=(Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path;$Output=Join-Path $root ('reports\arknights_effect_timeline_batch_'+(Get-Date -Format 'yyyyMMdd_HHmmss'))}
$outputFull=[IO.Path]::GetFullPath($Output)
if(Test-Path -LiteralPath $outputFull){throw "Output already exists: $outputFull"}
$temporary=Join-Path ([IO.Path]::GetTempPath()) ('arknights_effect_batch_'+[Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $temporary|Out-Null

try{
    $stageOut=Join-Path $temporary 'stage.stdout.txt';$stageErr=Join-Path $temporary 'stage.stderr.txt'
    $stageArgs=@([string]$profile.effects_adapter.preparer,'--profile',$profileFile,'--input-bundle',$inputFile,'--asset-pattern',$AssetPattern,'--exclude-pattern',$ExcludePattern,'--sample-time','0','--mode','timeline','--duration',$DurationSeconds.ToString([Globalization.CultureInfo]::InvariantCulture),'--fps',$Fps.ToString(),'--max-frames',$MaxFrames.ToString(),'--minimum-frames',$MinimumFrames.ToString(),'--empty-tail-frames',$EmptyTailFrames.ToString(),'--prewarm-ratio',$PrewarmRatio.ToString([Globalization.CultureInfo]::InvariantCulture),'--output',$outputFull)
    $stageCommand=Invoke-Process ([string]$profile.effects_adapter.python) $stageArgs $stageOut $stageErr 600
    if(-not(Test-Path $outputFull)){New-Item -ItemType Directory -Path $outputFull|Out-Null}
    $logs=Join-Path $outputFull 'logs';New-Item -ItemType Directory -Path $logs -Force|Out-Null
    Copy-Item $stageOut (Join-Path $logs 'stage.stdout.txt') -Force;Copy-Item $stageErr (Join-Path $logs 'stage.stderr.txt') -Force
    $stageCommand.stdout=Join-Path $logs 'stage.stdout.txt';$stageCommand.stderr=Join-Path $logs 'stage.stderr.txt'
    Write-Utf8NoBom (Join-Path $logs 'stage.command.json') (($stageCommand|ConvertTo-Json -Depth 8)+"`n")
    if($stageCommand.exit_code -ne 0){exit $stageCommand.exit_code}
    $stagePath=Join-Path $outputFull 'effect_stage.json';$stage=Get-Content $stagePath -Raw|ConvertFrom-Json
    if($stage.status -ne 'passed' -or [int]$stage.unresolved_count -ne 0){exit 4}
    $assets=@($stage.selected_assets)
    if($assets.Count -gt $MaxAssets){throw "Asset pattern selected $($assets.Count), exceeding MaxAssets=$MaxAssets."}
    $baseInput=Get-Content -LiteralPath $stage.unity_input -Raw|ConvertFrom-Json
    $results=[Collections.Generic.List[object]]::new()
    $assetRoot=Join-Path $outputFull 'assets';New-Item -ItemType Directory -Path $assetRoot|Out-Null
    foreach($asset in $assets){
        $safe=[IO.Path]::GetFileNameWithoutExtension([string]$asset) -replace '[^A-Za-z0-9._-]','_'
        $assetDir=Join-Path $assetRoot $safe;New-Item -ItemType Directory -Path $assetDir|Out-Null
        $assetLogs=Join-Path $assetDir 'logs';New-Item -ItemType Directory -Path $assetLogs|Out-Null
        $renderPath=Join-Path $assetDir 'effect_render.json';$framesDir=Join-Path $assetDir 'frames'
        $unityInput=[ordered]@{main_bundle=$baseInput.main_bundle;dependency_bundles=@($baseInput.dependency_bundles);asset_path=[string]$asset;asset_paths=@([string]$asset);sample_time=0;output_image=(Join-Path $assetDir 'unused.png');output_report=$renderPath;mode='timeline';duration_seconds=$DurationSeconds;fps=$Fps;max_frames=$MaxFrames;minimum_frames=$MinimumFrames;empty_tail_frames=$EmptyTailFrames;prewarm_ratio=$PrewarmRatio;frames_directory=$framesDir;drive_shader_time=$true}
        $unityInputPath=Join-Path $assetDir 'unity_input.json';Write-Utf8NoBom $unityInputPath (($unityInput|ConvertTo-Json -Depth 8)+"`n")
        $unityOut=Join-Path $assetLogs 'unity.stdout.txt';$unityErr=Join-Path $assetLogs 'unity.stderr.txt';$unityLog=Join-Path $assetLogs 'unity.editor.log'
        $unityArgs=@('-batchmode','-force-vulkan','-projectPath',[string]$profile.effects_adapter.unity_project,'-executeMethod','ArknightsEffectTimelineRenderer.Run','-stageInput',$unityInputPath,'-driveShaderTime','true','-logFile',$unityLog)
        $unity=Invoke-Process ([string]$profile.effects_adapter.unity_editor) $unityArgs $unityOut $unityErr $PerAssetTimeoutSeconds
        Write-Utf8NoBom (Join-Path $assetLogs 'unity.command.json') (($unity|ConvertTo-Json -Depth 8)+"`n")
        $render=if(Test-Path $renderPath){Get-Content $renderPath -Raw|ConvertFrom-Json}else{$null}
        $postStatus='not_run';$post=$null
        if($unity.exit_code -eq 0 -and $null -ne $render -and $render.status -eq 'passed_timeline_frames'){
            $postDir=Join-Path $assetDir 'preview';$postOut=Join-Path $assetLogs 'postprocess.stdout.txt';$postErr=Join-Path $assetLogs 'postprocess.stderr.txt'
            $postArgs=@([string]$profile.effects_adapter.timeline.postprocessor,'--render-report',$renderPath,'--output',$postDir,'--minimum-visible-pixels','300','--trim-pad','2')
            $postCommand=Invoke-Process ([string]$profile.effects_adapter.python) $postArgs $postOut $postErr 120
            Write-Utf8NoBom (Join-Path $assetLogs 'postprocess.command.json') (($postCommand|ConvertTo-Json -Depth 8)+"`n")
            $postPath=Join-Path $postDir 'timeline_postprocess.json'
            if(Test-Path $postPath){$post=Get-Content $postPath -Raw|ConvertFrom-Json;$postStatus=$post.status}else{$postStatus='missing_report'}
        }
        $passed=$null -ne $render -and $render.status -eq 'passed_timeline_frames' -and $null -ne $post -and $post.status -eq 'passed_timeline'
        $results.Add([ordered]@{asset=[string]$asset;directory=$assetDir;status=if($passed){'passed_timeline'}else{'failed'};unity_exit_code=$unity.exit_code;unity_timed_out=$unity.timed_out;render_status=if($null -eq $render){'missing_report'}else{$render.status};postprocess_status=$postStatus;frame_count=if($null -eq $render){0}else{$render.frame_count};retained_frame_count=if($null -eq $post){0}else{$post.retained_frame_count};gif=if($null -eq $post){$null}else{$post.gif.path};contact_sheet=if($null -eq $post){$null}else{$post.contact_sheet.path}})
        Write-Output ("[{0}/{1}] {2}: {3}" -f $results.Count,$assets.Count,$safe,$results[$results.Count-1].status)
    }
    $passedCount=@($results|Where-Object status -eq 'passed_timeline').Count
    $report=[ordered]@{schema_version=1;created_utc=[DateTime]::UtcNow.ToString('o');status=if($passedCount -eq $assets.Count){'passed_timeline_batch'}elseif($passedCount -gt 0){'partial'}else{'failed'};profile='arknights';scope='authentic_gameobject_prefab_deterministic_offline_timeline_batch';input_bundle=$inputFile;input_sha256=(Get-FileHash $inputFile -Algorithm SHA256).Hash;asset_pattern=$AssetPattern;exclude_pattern=$ExcludePattern;selected_asset_count=$assets.Count;passed_asset_count=$passedCount;failed_asset_count=$assets.Count-$passedCount;resolved_cab_count=$stage.resolved_cab_count;unresolved_cab_count=$stage.unresolved_count;shared_stage=$stagePath;results=$results;limitations=@('Each asset is rendered in a fresh Unity process while sharing one converted dependency stage.','Batch success proves offline rendering only; it does not prove runtime skill ownership or invocation order.')}
    $reportPath=Join-Path $outputFull 'effects_timeline_batch_report.json';Write-Utf8NoBom $reportPath (($report|ConvertTo-Json -Depth 12)+"`n")
    $key=@($stagePath,$reportPath,(Join-Path $logs 'stage.command.json'))+@($results|ForEach-Object{if($_.gif){$_.gif};if($_.contact_sheet){$_.contact_sheet};Join-Path $_.directory 'effect_render.json';Join-Path $_.directory 'preview\timeline_postprocess.json'}|Where-Object{Test-Path -LiteralPath $_ -PathType Leaf})
    $hashLines=foreach($path in $key|Select-Object -Unique){'{0} *{1}' -f (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash,((Get-RelativePath $outputFull $path).Replace('\','/'))}
    Write-Utf8NoBom (Join-Path $outputFull 'KEY_SHA256SUMS.txt') (($hashLines -join "`n")+"`n")
    if($report.status -ne 'passed_timeline_batch'){exit 8}
    exit 0
}finally{if(Test-Path $temporary){Remove-Item -LiteralPath $temporary -Recurse -Force}}
