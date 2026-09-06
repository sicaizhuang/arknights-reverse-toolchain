param(
    [Parameter(Mandatory=$true)][string]$InputRoot,
    [string]$Output='',
    [int]$MaxUnityFiles=-1,
    [int]$MaxExportsPerType=3
)
. (Join-Path $PSScriptRoot 'common.ps1')

$input=(Resolve-Path -LiteralPath $InputRoot).Path
if(-not $Output){$Output=New-VersionedDirectory (Join-Path (Get-ToolchainRoot) 'assets') ([IO.Path]::GetFileName($input.TrimEnd('\')))}
elseif(Test-Path -LiteralPath $Output){throw "Refusing existing output: $Output"}
else{New-Item -ItemType Directory -Path $Output|Out-Null}
$logs=Join-Path $Output 'logs';New-Item -ItemType Directory -Path $logs|Out-Null
$python=Join-Path (Get-ToolchainRoot) 'tools\unitypy\Scripts\python.exe'
$exporter=Join-Path $PSScriptRoot 'unity_asset_inventory.py'
$unityOutput=Join-Path $Output 'unitypy'
$unityResult=$null;$unityCommand=$null;$unityReport=$null
if(Test-Path -LiteralPath $python -PathType Leaf){
    $unityResult=Invoke-CapturedCommand -FilePath $python -Arguments @($exporter,$input,$unityOutput,'--max-files',[string]$MaxUnityFiles,'--max-exports-per-type',[string]$MaxExportsPerType)
    $unityCommand=Save-CommandResult $unityResult $logs 'unitypy_export'
    $candidate=Join-Path $unityOutput 'unity_asset_export.json'
    if(Test-Path -LiteralPath $candidate -PathType Leaf){$unityReport=Get-Content -LiteralPath $candidate -Raw|ConvertFrom-Json}
}
$assetRipper=Get-ChildItem -LiteralPath (Join-Path (Get-ToolchainRoot) 'tools\assetripper') -Filter 'AssetRipper.GUI.Free.exe' -File -ErrorAction SilentlyContinue|Select-Object -First 1
$assetRipperRecord=[ordered]@{status='missing';path=$null;version_command=$null;export_status='not_attempted';failure_reason='AssetRipper executable not installed'}
if($assetRipper){
    $versionResult=Invoke-CapturedCommand -FilePath $assetRipper.FullName -Arguments @('--version') -TimeoutSeconds 15
    $versionCommand=Save-CommandResult $versionResult $logs 'assetripper_version'
    $assetRipperRecord=[ordered]@{status=if($versionResult.exit_code-eq 0){'runnable'}else{'failed'};path=$assetRipper.FullName;version_command=$versionCommand;analysis_status='not_attempted';export_status='not_completed';failure_reason='AssetRipper.GUI.Free exposes a local web UI but no documented noninteractive input/output export arguments; no current resource was supplied and no GUI automation was used'}
}
$summary=if($unityReport){$unityReport.summary}else{$null}
$status='failed'
if($unityResult -and $unityResult.exit_code-eq 0 -and $unityReport){$status='partial';if($summary.unity_parse_success-gt 0 -and $summary.object_exports_success-gt 0){$status='exported'}}
$records=Get-RelativeFileRecords $Output
Write-JsonReport (Join-Path $Output 'asset_analysis.json') ([ordered]@{status=$status;input=$input;input_sha256_manifest=$null;unitypy=[ordered]@{path=$python;exit_code=if($unityResult){$unityResult.exit_code}else{$null};command=$unityCommand;report=if($unityReport){Join-Path $unityOutput 'unity_asset_export.json'}else{$null};summary=$summary};assetripper=$assetRipperRecord;outputs=$records;limitations=@('bounded sample exports do not imply every object decoded','Spine, CRI and custom encryption remain unsupported unless explicitly marked successful')})
Write-Output ([ordered]@{status=$status;output=$Output;unitypy_exit=if($unityResult){$unityResult.exit_code}else{$null};summary=$summary}|ConvertTo-Json -Depth 5 -Compress)
if($status -eq 'failed'){exit 4}
