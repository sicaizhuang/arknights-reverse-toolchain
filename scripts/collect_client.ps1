param(
    [string]$PackageName = '',
    [string]$AdbPath = '',
    [string]$OutputRoot = ''
)
. (Join-Path $PSScriptRoot 'common.ps1')

$root=Get-ToolchainRoot
if(-not $OutputRoot){$OutputRoot=Join-Path $root 'captures'}
$adb=Resolve-Adb $AdbPath
$stamp=Get-Date -Format 'yyyyMMdd_HHmmss'
$discovery=@()

function Invoke-Adb([string]$Name,[string[]]$Arguments){
    $result=Invoke-CapturedCommand -FilePath $adb -Arguments $Arguments
    $script:discovery += [pscustomobject]@{name=$Name;result=$result}
    return $result
}
function Stop-Collection([string]$Reason,[int]$ExitCode,[hashtable]$Details=@{}){
    $report=Join-Path $root ("reports\collect_client_blocked_{0}.json" -f $stamp)
    $payload=[ordered]@{status='blocked';reason=$Reason;adb=$adb;adb_sha256=(Get-Sha256 $adb);commands=@($discovery|ForEach-Object{[ordered]@{name=$_.name;exit_code=$_.result.exit_code;stdout=$_.result.stdout;stderr=$_.result.stderr;launch_error=$_.result.launch_error}})}
    foreach($key in $Details.Keys){$payload[$key]=$Details[$key]}
    Write-JsonReport $report $payload
    Write-Output ([ordered]@{status='blocked';reason=$Reason;report=$report}|ConvertTo-Json -Compress)
    exit $ExitCode
}

$deviceList=Invoke-Adb 'adb_devices' @('devices','-l')
if($deviceList.exit_code -ne 0){Stop-Collection 'adb_devices_failed' 2 @{stderr=$deviceList.stderr}}
$deviceRows=@($deviceList.stdout -split "`r?`n"|Select-Object -Skip 1|Where-Object{$_ -match '^\S+\s+device\b'})
if($deviceRows.Count -eq 0){Stop-Collection 'no_online_adb_device' 3}

$vmm=Get-CimInstance Win32_Process -ErrorAction SilentlyContinue|Where-Object{$_.Name -eq 'MuMuVMMHeadless.exe' -and $_.CommandLine -match 'MuMuPlayer'}
if(-not $vmm){Stop-Collection 'no_running_mumu_vmm' 4}
$vmmPids=@($vmm|Select-Object -ExpandProperty ProcessId)
$listeners=@(foreach($pidValue in $vmmPids){Get-NetTCPConnection -OwningProcess $pidValue -State Listen -ErrorAction SilentlyContinue})
$listenerPorts=@($listeners|Select-Object -ExpandProperty LocalPort -Unique)
$mumuTransports=@()
foreach($row in $deviceRows){
    $serial=($row -split '\s+')[0]
    $ownedPort=$null
    $kind=$null
    if($serial -match '^127\.0\.0\.1:(\d+)$' -and $listenerPorts -contains [int]$Matches[1]){$ownedPort=[int]$Matches[1];$kind='mumu_tcp'}
    elseif($serial -match '^emulator-(\d+)$' -and $listenerPorts -contains ([int]$Matches[1]+1)){$ownedPort=[int]$Matches[1]+1;$kind='mumu_emulator_alias'}
    if($kind){
        $androidId=Invoke-Adb ("identity_{0}_android_id" -f ($serial-replace '[^0-9A-Za-z]','_')) @('-s',$serial,'shell','settings','get','secure','android_id')
        $bootId=Invoke-Adb ("identity_{0}_boot_id" -f ($serial-replace '[^0-9A-Za-z]','_')) @('-s',$serial,'shell','cat','/proc/sys/kernel/random/boot_id')
        $fingerprint=Invoke-Adb ("identity_{0}_fingerprint" -f ($serial-replace '[^0-9A-Za-z]','_')) @('-s',$serial,'shell','getprop','ro.build.fingerprint')
        if(@(@($androidId,$bootId,$fingerprint)|Where-Object{$_.exit_code -ne 0}).Count -gt 0){Stop-Collection 'mumu_identity_query_failed' 5 @{serial=$serial}}
        $identity=(($androidId.stdout.Trim())+'|'+($bootId.stdout.Trim())+'|'+($fingerprint.stdout.Trim()))
        $mumuTransports += [pscustomobject]@{serial=$serial;kind=$kind;owned_port=$ownedPort;identity=$identity;row=$row}
    }
}
if($mumuTransports.Count -eq 0){Stop-Collection 'adb_devices_are_not_owned_by_mumu' 6 @{devices=$deviceRows;vmm_listener_ports=$listenerPorts}}
$identityGroups=@($mumuTransports|Group-Object identity)
if($identityGroups.Count -ne 1){Stop-Collection 'multiple_mumu_instances_detected' 7 @{transports=$mumuTransports}}
$preferred=@($mumuTransports|Where-Object{$_.kind -eq 'mumu_tcp'}|Sort-Object owned_port|Select-Object -First 1)
if($preferred.Count -eq 0){$preferred=@($mumuTransports|Sort-Object owned_port|Select-Object -First 1)}
$device=$preferred[0].serial

$packageList=Invoke-Adb 'pm_list_packages' @('-s',$device,'shell','pm','list','packages')
if($packageList.exit_code -ne 0){Stop-Collection 'pm_list_packages_failed' 8 @{device=$device}}
if(-not $PackageName){
    $candidates=@($packageList.stdout -split "`r?`n"|ForEach-Object{($_-replace '^package:','').Trim()}|Where-Object{$_ -match '(?i)(^com\.hypergryph\.arknights|arknights|gryphline|longcheng)'}|Sort-Object -Unique)
    if($candidates.Count -ne 1){Stop-Collection 'package_selection_required' 9 @{device=$device;candidates=$candidates}}
    $PackageName=$candidates[0]
}

$packageDump=Invoke-Adb 'dumpsys_package' @('-s',$device,'shell','dumpsys','package',$PackageName)
if($packageDump.exit_code -ne 0){Stop-Collection 'dumpsys_package_failed' 10 @{device=$device;package=$PackageName}}
$versionNameMatch=[regex]::Match($packageDump.stdout,'(?m)^\s*versionName=([^\r\n]+)')
$versionCodeMatch=[regex]::Match($packageDump.stdout,'(?m)^\s*versionCode=(\d+)')
$abiMatch=[regex]::Match($packageDump.stdout,'(?m)^\s*primaryCpuAbi=([^\r\n]+)')
if(-not $versionNameMatch.Success -or -not $versionCodeMatch.Success){Stop-Collection 'package_version_parse_failed' 11 @{device=$device;package=$PackageName}}
$versionName=$versionNameMatch.Groups[1].Value.Trim();$versionCode=$versionCodeMatch.Groups[1].Value.Trim();$primaryAbi=if($abiMatch.Success){$abiMatch.Groups[1].Value.Trim()}else{$null}
$safeVersion=$versionName-replace '[^0-9A-Za-z._-]','_'
$capture=New-VersionedDirectory $OutputRoot ("{0}_{1}" -f $PackageName,$safeVersion)
$logDir=Join-Path $capture 'logs';New-Item -ItemType Directory -Path $logDir|Out-Null
$commandRecords=@();foreach($entry in $discovery){$commandRecords+=Save-CommandResult $entry.result $logDir $entry.name}
function Invoke-LoggedAdb([string]$Name,[string[]]$Arguments){
    $result=Invoke-CapturedCommand -FilePath $adb -Arguments $Arguments
    $script:commandRecords+=Save-CommandResult $result $logDir $Name
    return $result
}

$apkDir=Join-Path $capture 'apk';New-Item -ItemType Directory -Path $apkDir|Out-Null
$pmPath=Invoke-LoggedAdb 'pm_path' @('-s',$device,'shell','pm','path',$PackageName)
$remoteApks=@();if($pmPath.exit_code-eq 0){$remoteApks=@($pmPath.stdout-split"`r?`n"|ForEach-Object{($_-replace'^package:','').Trim()}|Where-Object{$_-match'^/.+\.apk$'})}
$apkPulls=@();$requiredFailures=@()
if($pmPath.exit_code -ne 0 -or $remoteApks.Count -eq 0){$requiredFailures+='pm_path_or_apk_list_failed'}
foreach($remote in $remoteApks){
    $name=[IO.Path]::GetFileName($remote);$local=Join-Path $apkDir $name
    if(Test-Path -LiteralPath $local){$requiredFailures+=("duplicate_apk_name:{0}"-f$name);continue}
    $stat=Invoke-LoggedAdb ("stat_apk_{0}"-f$name) @('-s',$device,'shell','toybox','stat','-c','%s|%Y|%n',$remote)
    $pull=Invoke-LoggedAdb ("pull_apk_{0}"-f$name) @('-s',$device,'pull','-a','-q',$remote,$local)
    $ok=$pull.exit_code-eq 0 -and (Test-Path -LiteralPath $local -PathType Leaf)
    if(-not$ok){$requiredFailures+=("apk_pull_failed:{0}"-f$name)}
    $apkPulls += [ordered]@{remote_path=$remote;original_name=$name;is_base=($name -eq 'base.apk');local_path=$local;remote_stat=$stat.stdout.Trim();stat_exit_code=$stat.exit_code;pull_exit_code=$pull.exit_code;status=if($ok){'success'}else{'failed'};failure_reason=if($ok){$null}else{($pull.stderr+$pull.stdout).Trim()};size=if($ok){(Get-Item -LiteralPath $local).Length}else{$null};sha256=if($ok){Get-Sha256 $local}else{$null}}
}
if(@($apkPulls|Where-Object{$_.is_base -and $_.status -eq 'success'}).Count -ne 1){$requiredFailures+='base_apk_missing'}

$directoryPulls=@()
foreach($kind in @('obb','data')){
    $remote="/sdcard/Android/$kind/$PackageName";$parent=Join-Path $capture ("android\{0}"-f$kind);New-Item -ItemType Directory -Path $parent -Force|Out-Null;$local=Join-Path $parent $PackageName
    $probe=Invoke-LoggedAdb ("probe_android_{0}"-f$kind) @('-s',$device,'shell','ls','-ld',$remote)
    if($probe.exit_code -ne 0){$directoryPulls+=[ordered]@{category=$kind;remote_path=$remote;local_path=$local;status='not_present';probe_exit_code=$probe.exit_code;pull_exit_code=$null;failure_reason=$probe.stderr.Trim()};continue}
    $du=Invoke-LoggedAdb ("du_android_{0}"-f$kind) @('-s',$device,'shell','du','-sk',$remote)
    if($kind -eq 'obb'){
        $pull=Invoke-LoggedAdb 'pull_android_obb' @('-s',$device,'pull','-a','-q',$remote,$local)
        $ok=$pull.exit_code -eq 0 -and (Test-Path -LiteralPath $local -PathType Container)
        if(-not $ok){$requiredFailures+='android_obb_pull_failed'}
        $directoryPulls+=[ordered]@{category=$kind;remote_path=$remote;local_path=$local;status=if($ok){'success'}else{'failed'};probe_exit_code=$probe.exit_code;du_exit_code=$du.exit_code;remote_kib=if($du.stdout -match '^(\d+)'){[int64]$Matches[1]}else{$null};items=@([ordered]@{remote_path=$remote;local_path=$local;pull_exit_code=$pull.exit_code;status=if($ok){'success'}else{'failed'};failure_reason=if($ok){$null}else{($pull.stderr+$pull.stdout).Trim()}})}
        continue
    }
    New-Item -ItemType Directory -Path $local -Force|Out-Null
    $staging=Join-Path $capture '_pull_staging\android_data';New-Item -ItemType Directory -Path $staging -Force|Out-Null
    $topFind=Invoke-LoggedAdb 'find_android_data_top' @('-s',$device,'shell','find',$remote,'-mindepth','1','-maxdepth','1','-print')
    $items=@();$nonPermissionFailures=0
    if($topFind.exit_code -ne 0){$requiredFailures+='android_data_inventory_failed';$nonPermissionFailures++}
    $pullTargets=@()
    foreach($topRemote in @($topFind.stdout -split "`r?`n"|Where-Object{$_ -match '^/'})){
        $topName=[IO.Path]::GetFileName($topRemote);$children=Invoke-LoggedAdb ("find_android_data_{0}"-f($topName-replace'[^0-9A-Za-z._-]','_')) @('-s',$device,'shell','find',$topRemote,'-mindepth','1','-maxdepth','1','-print')
        $childRows=@($children.stdout -split "`r?`n"|Where-Object{$_ -match '^/'})
        if($children.exit_code -eq 0 -and $childRows.Count -gt 0){
            $topLocal=Join-Path $local $topName;New-Item -ItemType Directory -Path $topLocal -Force|Out-Null
            foreach($child in $childRows){$pullTargets+=[pscustomobject]@{remote=$child;local=Join-Path $topLocal ([IO.Path]::GetFileName($child))}}
        }elseif($children.exit_code -eq 0){
            New-Item -ItemType Directory -Path (Join-Path $local $topName) -Force|Out-Null
            $items+=[ordered]@{remote_path=$topRemote;local_path=(Join-Path $local $topName);pull_exit_code=$null;status='success_empty_directory';failure_reason=$null}
        }else{$pullTargets+=[pscustomobject]@{remote=$topRemote;local=Join-Path $local $topName}}
    }
    $pullIndex=0
    foreach($target in $pullTargets){
        $leaf=[IO.Path]::GetFileName($target.remote);$safe=$leaf-replace'[^0-9A-Za-z._-]','_';$stage=Join-Path $staging ("{0:D3}_{1}"-f$pullIndex,$safe)
        $pull=Invoke-LoggedAdb ("pull_android_data_{0:D3}_{1}"-f$pullIndex,$safe) @('-s',$device,'pull','-a','-q',$target.remote,$stage)
        $reason=($pull.stderr+$pull.stdout).Trim();$ok=$pull.exit_code -eq 0 -and (Test-Path -LiteralPath $stage)
        $itemStatus='failed'
        if($ok){
            if(Test-Path -LiteralPath $target.local){$reason='local destination collision';$requiredFailures+=("android_data_local_collision:{0}"-f$target.local);$nonPermissionFailures++}
            else{Move-Item -LiteralPath $stage -Destination $target.local;$itemStatus='success'}
        }elseif($reason -match '(?i)Permission denied'){$itemStatus='inaccessible'}
        else{$requiredFailures+=("android_data_pull_failed:{0}"-f$target.remote);$nonPermissionFailures++}
        $items+=[ordered]@{remote_path=$target.remote;local_path=$target.local;staging_path=if($itemStatus -eq 'success'){$null}else{$stage};pull_exit_code=$pull.exit_code;status=$itemStatus;failure_reason=if($itemStatus -eq 'success'){$null}else{$reason}}
        $pullIndex++
    }
    $dataOk=$topFind.exit_code -eq 0 -and $nonPermissionFailures -eq 0
    $directoryPulls+=[ordered]@{category=$kind;remote_path=$remote;local_path=$local;status=if($dataOk -and @($items|Where-Object{$_.status -eq 'inaccessible'}).Count){'success_with_inaccessible_files'}elseif($dataOk){'success'}else{'failed'};probe_exit_code=$probe.exit_code;du_exit_code=$du.exit_code;remote_kib=if($du.stdout -match '^(\d+)'){[int64]$Matches[1]}else{$null};items=$items;inaccessible_count=@($items|Where-Object{$_.status -eq 'inaccessible'}).Count;failed_count=@($items|Where-Object{$_.status -eq 'failed'}).Count}
}

Add-Type -AssemblyName System.IO.Compression.FileSystem
$il2cppDir=Join-Path $capture 'il2cpp';New-Item -ItemType Directory -Path $il2cppDir|Out-Null
$archiveArtifacts=@();$unityClues=@()
foreach($apkRecord in $apkPulls|Where-Object{$_.status -eq 'success'}){
    $zip=[IO.Compression.ZipFile]::OpenRead($apkRecord.local_path)
    try{
        foreach($entry in $zip.Entries){
            $targetKind=$null
            if($entry.FullName-match'^lib/([^/]+)/libil2cpp\.so$'){$targetKind='libil2cpp';$abi=$Matches[1]}
            elseif($entry.FullName-match'(^|/)global-metadata\.dat$'){$targetKind='global_metadata';$abi=$null}
            elseif($entry.FullName-match'(^|/)globalgamemanagers$'){$targetKind='globalgamemanagers';$abi=$null}
            if(-not$targetKind){continue}
            $safeEntry=$entry.FullName-replace'[/\\]','_';$dest=Join-Path $il2cppDir (([IO.Path]::GetFileNameWithoutExtension($apkRecord.original_name))+'_'+$safeEntry)
            if(Test-Path -LiteralPath $dest){throw "Refusing existing archive extraction: $dest"}
            $inputStream=$entry.Open();$outputStream=[IO.File]::Create($dest);try{$inputStream.CopyTo($outputStream)}finally{$outputStream.Dispose();$inputStream.Dispose()}
            $record=[ordered]@{source_apk=$apkRecord.local_path;archive_entry=$entry.FullName;kind=$targetKind;abi=$abi;local_path=$dest;size=(Get-Item -LiteralPath $dest).Length;sha256=Get-Sha256 $dest;status='success'}
            $archiveArtifacts+=$record
            if($targetKind -eq 'globalgamemanagers'){$bytes=[IO.File]::ReadAllBytes($dest);$ascii=[Text.Encoding]::ASCII.GetString($bytes);$matches=[regex]::Matches($ascii,'20\d\d\.\d+\.\d+[abfp]\d+');$unityClues+=@($matches|ForEach-Object{$_.Value}|Select-Object -Unique)}
        }
    }finally{$zip.Dispose()}
}
if(@($archiveArtifacts|Where-Object{$_.kind -eq 'libil2cpp'}).Count -eq 0){$requiredFailures+='libil2cpp_not_found'}
if(@($archiveArtifacts|Where-Object{$_.kind -eq 'global_metadata'}).Count -eq 0){$requiredFailures+='global_metadata_not_found'}

$dataRoot=Join-Path $capture ("android\data\{0}"-f$PackageName)
$hotUpdate=@();if(Test-Path -LiteralPath $dataRoot){$hotUpdate=@(Get-ChildItem -LiteralPath $dataRoot -Recurse -File -ErrorAction SilentlyContinue|Where-Object{$_.Name -match '(?i)(\.bundle$|\.ab$|\.assets$|\.acb$|\.awb$|\.usm$|\.hca$)' }|Select-Object FullName,Length,LastWriteTimeUtc)}
$files=Get-RelativeFileRecords $capture
$allDirectoryItems=@()
foreach($directoryRecord in @($directoryPulls)){
    foreach($directoryItem in @($directoryRecord.items)){
        if($null -ne $directoryItem){$allDirectoryItems += $directoryItem}
    }
}
$inaccessibleCount=@($allDirectoryItems|Where-Object{ $_.status -eq 'inaccessible' }).Count
$failedDirectoryItemCount=@($allDirectoryItems|Where-Object{ $_.status -eq 'failed' }).Count
$status=if($requiredFailures.Count -gt 0){'partial'}elseif($inaccessibleCount -gt 0){'collected_with_warnings'}else{'collected'}
$captureReport=Join-Path $capture 'capture.json'
Write-JsonReport $captureReport ([ordered]@{status=$status;inaccessible_count=$inaccessibleCount;failed_directory_item_count=$failedDirectoryItemCount;required_failures=@($requiredFailures|Select-Object -Unique);mumu=[ordered]@{selected_transport=$device;aliases=$mumuTransports;vmm_instances=@($vmm|Select-Object ProcessId,CommandLine);listener_ports=$listenerPorts;identity=$identityGroups[0].Name};package=$PackageName;version_name=$versionName;version_code=$versionCode;primary_abi=$primaryAbi;adb=$adb;adb_sha256=Get-Sha256 $adb;commands=$commandRecords;apk=[ordered]@{status=if(@($apkPulls|Where-Object status -eq 'failed').Count){'failed'}else{'success'};items=$apkPulls};android_directories=$directoryPulls;archive_artifacts=$archiveArtifacts;unity_version_clues=@($unityClues|Select-Object -Unique);hot_update_file_count=$hotUpdate.Count;hot_update_files=$hotUpdate;files=$files})
$summary=Join-Path $root ("reports\collect_client_{0}.json"-f$stamp)
Write-JsonReport $summary ([ordered]@{status=$status;capture=$capture;capture_report=$captureReport;package=$PackageName;version_name=$versionName;version_code=$versionCode;selected_transport=$device;required_failures=@($requiredFailures|Select-Object -Unique);file_count=$files.Count})
Write-Output ([ordered]@{status=$status;capture=$capture;report=$captureReport;required_failures=@($requiredFailures|Select-Object -Unique);files=$files.Count}|ConvertTo-Json -Compress)
if($status -ne 'collected'){exit 12}
