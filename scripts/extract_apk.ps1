param(
    [Parameter(Mandatory=$true)][string]$Apk,
    [string]$SplitApkDirectory='',
    [string]$ApksArchive='',
    [string]$AdbPath='',
    [string]$DeviceSerial='',
    [switch]$SkipApktool,
    [string]$ExistingDecodedRoot='',
    [switch]$SkipJadx,
    [string]$ExistingJadxOutput='',
    [string]$Output=''
)
. (Join-Path $PSScriptRoot 'common.ps1')

$baseApk=(Resolve-Path -LiteralPath $Apk).Path
if(-not $Output){$Output=New-VersionedDirectory (Join-Path (Get-ToolchainRoot) 'apk') ([IO.Path]::GetFileNameWithoutExtension($baseApk))}
elseif(Test-Path -LiteralPath $Output){throw "Refusing existing output: $Output"}
else{New-Item -ItemType Directory -Path $Output|Out-Null}
$logs=Join-Path $Output 'logs';New-Item -ItemType Directory -Path $logs|Out-Null

$apkFiles=@($baseApk)
if($SplitApkDirectory){
    $splitRoot=(Resolve-Path -LiteralPath $SplitApkDirectory).Path
    $apkFiles=@(Get-ChildItem -LiteralPath $splitRoot -Filter '*.apk' -File|Select-Object -ExpandProperty FullName)
    if($apkFiles -notcontains $baseApk){$apkFiles=@($baseApk)+$apkFiles}
}
$apkFiles=@($apkFiles|Sort-Object -Unique)
$aapt2=(Get-ChildItem -LiteralPath (Join-Path (Get-ToolchainRoot) 'tools\aapt2') -Filter aapt2.exe -Recurse -File|Select-Object -First 1).FullName
$apktool=(Get-ChildItem -LiteralPath (Join-Path (Get-ToolchainRoot) 'tools\apktool') -Filter 'apktool*.jar' -Recurse -File|Select-Object -First 1).FullName
$java=(Get-ChildItem -LiteralPath (Join-Path (Get-ToolchainRoot) 'tools\jdk') -Filter java.exe -Recurse -File|Select-Object -First 1).FullName
$jadx=(Get-ChildItem -LiteralPath (Join-Path (Get-ToolchainRoot) 'tools\jadxcli') -Filter jadx.bat -Recurse -File|Select-Object -First 1).FullName
$bundletool=(Get-ChildItem -LiteralPath (Join-Path (Get-ToolchainRoot) 'tools\bundletool') -Filter 'bundletool*.jar' -Recurse -File|Select-Object -First 1).FullName
$toolPaths=[ordered]@{aapt2=$aapt2;apktool=$apktool;java=$java;jadx=$jadx;bundletool=$bundletool}
$missing=@($toolPaths.GetEnumerator()|Where-Object{-not $_.Value -or -not(Test-Path -LiteralPath $_.Value -PathType Leaf)}|ForEach-Object{$_.Key})

Add-Type -AssemblyName System.IO.Compression.FileSystem
$aaptRecords=@();$apktoolRecords=@();$archiveRecords=@()
foreach($apkPath in $apkFiles){
    $name=[IO.Path]::GetFileName($apkPath);$stem=[IO.Path]::GetFileNameWithoutExtension($apkPath)
    $badging=$null;$permissions=$null;$xmltree=$null
    if($aapt2){
        $badgingResult=Invoke-CapturedCommand -FilePath $aapt2 -Arguments @('dump','badging',$apkPath)
        $badging=Save-CommandResult $badgingResult $logs ("aapt2_badging_{0}"-f$stem)
        $permissionResult=Invoke-CapturedCommand -FilePath $aapt2 -Arguments @('dump','permissions',$apkPath)
        $permissions=Save-CommandResult $permissionResult $logs ("aapt2_permissions_{0}"-f$stem)
        $xmlResult=Invoke-CapturedCommand -FilePath $aapt2 -Arguments @('dump','xmltree','--file','AndroidManifest.xml',$apkPath)
        $xmltree=Save-CommandResult $xmlResult $logs ("aapt2_xmltree_{0}"-f$stem)
    }
    $aaptRecords += [ordered]@{apk=$apkPath;badging=$badging;permissions=$permissions;xmltree=$xmltree;status=if($badging.exit_code-eq 0 -and $permissions.exit_code-eq 0 -and $xmltree.exit_code-eq 0){'success'}else{'failed'}}
    $decoded=Join-Path $Output ("decoded\{0}"-f$stem)
    $apktoolResult=$null;$apktoolCommand=$null
    if($SkipApktool -and $ExistingDecodedRoot){
        $decoded=Join-Path (Resolve-Path -LiteralPath $ExistingDecodedRoot).Path $stem
        $apktoolRecords += [ordered]@{apk=$apkPath;decoded=$decoded;command=$null;status=if(Test-Path -LiteralPath (Join-Path $decoded 'AndroidManifest.xml')){'reused_readonly'}else{'failed'}}
    }else{
        if($java -and $apktool){
            $apktoolResult=Invoke-CapturedCommand -FilePath $java -Arguments @('-jar',$apktool,'d','-f','-o',$decoded,$apkPath)
            $apktoolCommand=Save-CommandResult $apktoolResult $logs ("apktool_{0}"-f$stem)
        }
        $apktoolRecords += [ordered]@{apk=$apkPath;decoded=$decoded;command=$apktoolCommand;status=if($apktoolResult -and $apktoolResult.exit_code -eq 0 -and (Test-Path -LiteralPath (Join-Path $decoded 'AndroidManifest.xml'))){'success'}else{'failed'}}
    }
    $zip=[IO.Compression.ZipFile]::OpenRead($apkPath)
    try{
        $native=@($zip.Entries|Where-Object{$_.FullName-match'^lib/([^/]+)/([^/]+\.so)$'}|ForEach-Object{[ordered]@{entry=$_.FullName;abi=([regex]::Match($_.FullName,'^lib/([^/]+)/').Groups[1].Value);library=[IO.Path]::GetFileName($_.FullName);size=$_.Length}})
        $archiveRecords += [ordered]@{apk=$apkPath;sha256=Get-Sha256 $apkPath;size=(Get-Item -LiteralPath $apkPath).Length;native_libraries=$native;abis=@($native.abi|Sort-Object -Unique);entry_count=$zip.Entries.Count}
    }finally{$zip.Dispose()}
}

$jadxOutput=Join-Path $Output 'jadx';$jadxResult=$null;$jadxCommand=$null;$jadxReused=$false
if($SkipJadx -and $ExistingJadxOutput){$jadxOutput=(Resolve-Path -LiteralPath $ExistingJadxOutput).Path;$jadxReused=$true}
elseif($jadx){
    $jadxArgs=@('/d','/c',$jadx,'--output-dir',$jadxOutput,'--show-bad-code','--deobf')+$apkFiles
    $javaHome=Split-Path (Split-Path $java -Parent) -Parent
    $jadxResult=Invoke-CapturedCommand -FilePath "$env:SystemRoot\System32\cmd.exe" -Arguments $jadxArgs -Environment @{JAVA_HOME=$javaHome}
    $jadxCommand=Save-CommandResult $jadxResult $logs 'jadx_all_apks'
}

$hasSplitApks=@($apkFiles|Where-Object{[IO.Path]::GetFileName($_) -ne 'base.apk'}).Count -gt 0
$bundletoolRecord=[ordered]@{version=$null;device_spec=$null;apks_extract=$null;status=if(-not $hasSplitApks -and -not $ApksArchive){'not_applicable_no_split_apks'}else{'not_attempted'};relationship_mode=if(-not $hasSplitApks -and -not $ApksArchive){'not_applicable_no_split_apks'}else{'manifest_only'}}
if($java -and $bundletool){
    $versionResult=Invoke-CapturedCommand -FilePath $java -Arguments @('-jar',$bundletool,'version')
    $bundletoolRecord.version=Save-CommandResult $versionResult $logs 'bundletool_version'
    if($DeviceSerial){
        $adb=Resolve-Adb $AdbPath;$deviceSpec=Join-Path $Output 'bundletool_device_spec.json'
        $specResult=Invoke-CapturedCommand -FilePath $java -Arguments @('-jar',$bundletool,'get-device-spec',("--output={0}"-f$deviceSpec),("--adb={0}"-f$adb),("--device-id={0}"-f$DeviceSerial))
        $bundletoolRecord.device_spec=Save-CommandResult $specResult $logs 'bundletool_device_spec'
        $bundletoolRecord.relationship_mode='manifest_plus_bundletool_device_targeting'
    }
    if($ApksArchive){
        $apks=(Resolve-Path -LiteralPath $ApksArchive).Path;$selected=Join-Path $Output 'bundletool_selected_splits';$spec=Join-Path $Output 'bundletool_device_spec.json'
        $arguments=@('-jar',$bundletool,'extract-apks',("--apks={0}"-f$apks),("--output-dir={0}"-f$selected));if(Test-Path -LiteralPath $spec){$arguments+=("--device-spec={0}"-f$spec)}
        $extractResult=Invoke-CapturedCommand -FilePath $java -Arguments $arguments
        $bundletoolRecord.apks_extract=Save-CommandResult $extractResult $logs 'bundletool_extract_apks'
        $bundletoolRecord.relationship_mode='bundletool_apks_selection'
    }
    if($hasSplitApks -or $ApksArchive){$bundletoolRecord.status=if($bundletoolRecord.version.exit_code-ne 0){'failed'}elseif($DeviceSerial -and $bundletoolRecord.device_spec.exit_code-ne 0){'failed'}elseif($ApksArchive -and $bundletoolRecord.apks_extract.exit_code-ne 0){'failed'}else{'success'}}
}

$androidNs='http://schemas.android.com/apk/res/android'
$manifests=@();$permissionsList=@();$activities=@();$services=@();$providers=@();$receivers=@();$splitRelations=@()
foreach($apktoolRecord in $apktoolRecords|Where-Object{$_.status -in @('success','reused_readonly')}){
    $manifestPath=Join-Path $apktoolRecord.decoded 'AndroidManifest.xml'
    try{
        [xml]$xml=Get-Content -LiteralPath $manifestPath -Raw
        $manifest=$xml.DocumentElement;$pkg=$manifest.GetAttribute('package');$split=$manifest.GetAttribute('split');$configForSplit=$manifest.GetAttribute('configForSplit');$isFeature=$manifest.GetAttribute('isFeatureSplit',$androidNs)
        $manifestVersionName=$manifest.GetAttribute('versionName',$androidNs);$manifestVersionCode=$manifest.GetAttribute('versionCode',$androidNs)
        if(-not $manifestVersionName){$badgingRecord=@($aaptRecords|Where-Object{$_.apk -eq $apktoolRecord.apk}|Select-Object -First 1);if($badgingRecord){$badgingText=Get-Content -LiteralPath $badgingRecord.badging.stdout_path -Raw;$badgingMatch=[regex]::Match($badgingText,"package: name='([^']+)' versionCode='([^']+)' versionName='([^']*)'");if($badgingMatch.Success){if(-not$pkg){$pkg=$badgingMatch.Groups[1].Value};$manifestVersionCode=$badgingMatch.Groups[2].Value;$manifestVersionName=$badgingMatch.Groups[3].Value}}}
        $manifests += [ordered]@{apk=$apktoolRecord.apk;path=$manifestPath;package=$pkg;version_name=$manifestVersionName;version_code=$manifestVersionCode;split=$split;config_for_split=$configForSplit;is_feature_split=$isFeature}
        foreach($node in $xml.SelectNodes('/manifest/uses-permission|/manifest/uses-permission-sdk-23')){$permissionsList+=[ordered]@{apk=$apktoolRecord.apk;name=$node.GetAttribute('name',$androidNs);kind=$node.Name}}
        foreach($node in $xml.SelectNodes('/manifest/application/activity|/manifest/application/activity-alias')){$activities+=[ordered]@{apk=$apktoolRecord.apk;name=$node.GetAttribute('name',$androidNs);exported=$node.GetAttribute('exported',$androidNs);permission=$node.GetAttribute('permission',$androidNs);kind=$node.Name}}
        foreach($node in $xml.SelectNodes('/manifest/application/service')){$services+=[ordered]@{apk=$apktoolRecord.apk;name=$node.GetAttribute('name',$androidNs);exported=$node.GetAttribute('exported',$androidNs);permission=$node.GetAttribute('permission',$androidNs)}}
        foreach($node in $xml.SelectNodes('/manifest/application/provider')){$providers+=[ordered]@{apk=$apktoolRecord.apk;name=$node.GetAttribute('name',$androidNs);authorities=$node.GetAttribute('authorities',$androidNs);exported=$node.GetAttribute('exported',$androidNs)}}
        foreach($node in $xml.SelectNodes('/manifest/application/receiver')){$receivers+=[ordered]@{apk=$apktoolRecord.apk;name=$node.GetAttribute('name',$androidNs);exported=$node.GetAttribute('exported',$androidNs);permission=$node.GetAttribute('permission',$androidNs)}}
        $usesSplits=@($xml.SelectNodes('/manifest/uses-split')|ForEach-Object{$_.GetAttribute('name',$androidNs)})
        $splitRelations += [ordered]@{apk=$apktoolRecord.apk;package=$pkg;split=if($split){$split}else{'base'};config_for_split=$configForSplit;is_feature_split=$isFeature;uses_splits=$usesSplits;analysis_source='decoded_manifest';bundletool_targeting=if($bundletoolRecord.device_spec -and $bundletoolRecord.device_spec.exit_code-eq 0){'device_spec_verified'}else{'not_available'}}
    }catch{
        $manifests += [ordered]@{apk=$apktoolRecord.apk;path=$manifestPath;parse_error=$_.Exception.Message}
    }
}

$baseManifest=@($manifests|Where-Object{-not $_.split}|Select-Object -First 1)
$necessaryFailures=@()
if($missing.Count){$necessaryFailures+='missing_tools:' + ($missing-join',')}
if(@($aaptRecords|Where-Object{$_.status -ne 'success'}).Count){$necessaryFailures+='aapt2_failed'}
if(@($apktoolRecords|Where-Object{$_.status -notin @('success','reused_readonly')}).Count){$necessaryFailures+='apktool_failed'}
$jadxStatus=if($jadxReused){'reused_partial'}elseif($jadxResult -and $jadxResult.exit_code -eq 0){'success'}elseif($jadxOutput -and (Get-ChildItem -LiteralPath $jadxOutput -Recurse -File -ErrorAction SilentlyContinue|Measure-Object).Count -gt 0){'partial'}else{'failed'}
if($jadxStatus -eq 'failed'){$necessaryFailures+='jadx_failed'}elseif($jadxStatus -in @('partial','reused_partial')){$necessaryFailures+='jadx_partial_with_errors'}
if($bundletoolRecord.status -notin @('success','not_applicable_no_split_apks')){$necessaryFailures+='bundletool_failed'}
if($baseManifest.Count -ne 1){$necessaryFailures+='base_manifest_not_parsed'}
$status=if($necessaryFailures.Count){'partial'}else{'parsed'}
$records=Get-RelativeFileRecords $Output
$reportPath=Join-Path $Output 'apk_analysis.json'
Write-JsonReport $reportPath ([ordered]@{status=$status;necessary_failures=$necessaryFailures;inputs=$archiveRecords;tools=$toolPaths;aapt2=$aaptRecords;apktool=$apktoolRecords;jadx=[ordered]@{status=$jadxStatus;command=$jadxCommand;output=$jadxOutput};bundletool=$bundletoolRecord;package=if($baseManifest){$baseManifest[0].package}else{$null};version_name=if($baseManifest){$baseManifest[0].version_name}else{$null};version_code=if($baseManifest){$baseManifest[0].version_code}else{$null};manifests=$manifests;permissions=$permissionsList;activities=$activities;services=$services;providers=$providers;receivers=$receivers;abis=@($archiveRecords.abis|ForEach-Object{$_}|Sort-Object -Unique);native_libraries=@($archiveRecords.native_libraries|ForEach-Object{$_});split_relationships=$splitRelations;outputs=$records})
Write-Output ([ordered]@{status=$status;output=$Output;report=$reportPath;package=if($baseManifest){$baseManifest[0].package}else{$null};apk_count=$apkFiles.Count;failures=$necessaryFailures}|ConvertTo-Json -Depth 4 -Compress)
if($status -ne 'parsed'){exit 5}
