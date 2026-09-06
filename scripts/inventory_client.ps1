param([Parameter(Mandatory=$true)][string]$Capture,[string]$Output='')
. (Join-Path $PSScriptRoot 'common.ps1')
$capture=(Resolve-Path -LiteralPath $Capture).Path
if (-not $Output) { $Output=Join-Path (Get-ToolchainRoot) 'reports\client_inventory.json' }
$files=Get-RelativeFileRecords $capture
$classes=@{apk=0;obb=0;data=0;il2cpp=0;metadata=0;unityfs=0;assets=0;bundle=0;audio=0;other=0}
foreach($f in $files){$n=$f.path.ToLowerInvariant(); if($n -match '\.apk$'){$classes.apk++}elseif($n -match '\.obb$'){$classes.obb++}elseif($n -match 'libil2cpp\.so$'){$classes.il2cpp++}elseif($n -match 'global-metadata.*\.dat$'){$classes.metadata++}elseif($n -match 'unityfs|\.bundle$'){$classes.unityfs++}elseif($n -match '\.assets$'){$classes.assets++}elseif($n -match '\.ab$'){$classes.bundle++}elseif($n -match '\.(wav|mp3|ogg|wem|acb|awb|usm)$'){$classes.audio++}else{$classes.other++}}
Write-JsonReport $Output ([ordered]@{status='ok';capture=$capture;file_count=$files.Count;classes=$classes;files=$files})
Write-Output (@{status='ok';report=$Output;files=$files.Count} | ConvertTo-Json -Compress)
