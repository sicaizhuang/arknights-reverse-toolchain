param(
    [string]$Capture='',
    [string]$AdbPath='',
    [string]$DeviceSerial='',
    [string]$Output=''
)
. (Join-Path $PSScriptRoot 'common.ps1')

$root=Get-ToolchainRoot
if(-not $Output){$Output=New-VersionedDirectory (Join-Path $root 'tests') 'verify'}
elseif(Test-Path -LiteralPath $Output){throw "Refusing existing output: $Output"}
else{New-Item -ItemType Directory -Path $Output|Out-Null}
$logs=Join-Path $Output 'logs';New-Item -ItemType Directory -Path $logs|Out-Null
if(-not $Capture){
    $captureReport=Get-ChildItem -LiteralPath (Join-Path $root 'captures') -Filter capture.json -Recurse -File -ErrorAction SilentlyContinue|Sort-Object LastWriteTime -Descending|Select-Object -First 1
    if($captureReport){$Capture=$captureReport.DirectoryName}
}
if($Capture){$Capture=(Resolve-Path -LiteralPath $Capture).Path}

$java17=(Get-ChildItem -LiteralPath (Join-Path $root 'tools\jdk') -Filter java.exe -Recurse -File|Select-Object -First 1).FullName
$java21=(Get-ChildItem -LiteralPath (Join-Path $root 'tools\jdk21') -Filter java.exe -Recurse -File|Select-Object -First 1).FullName
$jadx=(Get-ChildItem -LiteralPath (Join-Path $root 'tools\jadxcli') -Filter jadx.bat -Recurse -File|Select-Object -First 1).FullName
$apktool=(Get-ChildItem -LiteralPath (Join-Path $root 'tools\apktool') -Filter 'apktool*.jar' -Recurse -File|Select-Object -First 1).FullName
$aapt2=(Get-ChildItem -LiteralPath (Join-Path $root 'tools\aapt2') -Filter aapt2.exe -Recurse -File|Select-Object -First 1).FullName
$bundletool=(Get-ChildItem -LiteralPath (Join-Path $root 'tools\bundletool') -Filter 'bundletool*.jar' -Recurse -File|Select-Object -First 1).FullName
$dumper=(Get-ChildItem -LiteralPath (Join-Path $root 'tools\il2cppdumper') -Filter Il2CppDumper.exe -Recurse -File|Select-Object -First 1).FullName
$unityPython=Join-Path $root 'tools\unitypy\Scripts\python.exe'
$ghidraHeadless=(Get-ChildItem -LiteralPath (Join-Path $root 'tools\ghidra') -Filter analyzeHeadless.bat -Recurse -File|Select-Object -First 1).FullName
$cpp2il='D:\Cpp2IL-2022.1.0-pre-release.21\Cpp2IL.exe'
$adb=Resolve-Adb $AdbPath

$checks=@();$failures=@()
function Add-VersionCheck([string]$Name,[string]$File,[string[]]$Arguments,[bool]$Required=$true,[hashtable]$Environment=@{},[string]$SubjectPath=''){
    if(-not $SubjectPath){$SubjectPath=$File}
    if(-not $File -or -not(Test-Path -LiteralPath $File -PathType Leaf) -or -not $SubjectPath -or -not(Test-Path -LiteralPath $SubjectPath -PathType Leaf)){
        $script:checks += [ordered]@{name=$Name;status='missing';required=$Required;path=$SubjectPath;launcher=$File;command=$null;sha256=$null}
        if($Required){$script:failures+=("missing_tool:{0}"-f$Name)}
        return
    }
    $invokeFile=$File;$invokeArguments=$Arguments
    if([IO.Path]::GetExtension($File)-in @('.bat','.cmd')){$invokeFile="$env:SystemRoot\System32\cmd.exe";$invokeArguments=@('/d','/c',$File)+$Arguments}
    $result=Invoke-CapturedCommand -FilePath $invokeFile -Arguments $invokeArguments -TimeoutSeconds 20 -Environment $Environment
    $command=Save-CommandResult $result $logs ("version_{0}"-f($Name-replace'[^0-9A-Za-z]','_'))
    $ok=$result.exit_code -eq 0
    $script:checks += [ordered]@{name=$Name;status=if($ok){'runnable'}else{'failed'};required=$Required;path=$SubjectPath;launcher=$File;sha256=if(Test-Path -LiteralPath $SubjectPath -PathType Leaf){Get-Sha256 $SubjectPath}else{$null};command=$command;version_output=(($result.stdout+"`n"+$result.stderr).Trim())}
    if($Required -and -not$ok){$script:failures+=("version_check_failed:{0}"-f$Name)}
}

Add-VersionCheck 'MuMu ADB' $adb @('version') $true
Add-VersionCheck 'Temurin JDK 17' $java17 @('-version') $true
Add-VersionCheck 'Temurin JDK 21' $java21 @('-version') $true
$javaHome=if($java17){Split-Path (Split-Path $java17 -Parent) -Parent}else{''}
Add-VersionCheck 'JADX' $jadx @('--version') $true @{JAVA_HOME=$javaHome}
Add-VersionCheck 'Apktool' $java17 @('-jar',$apktool,'--version') $true @{} $apktool
Add-VersionCheck 'AAPT2' $aapt2 @('version') $true
Add-VersionCheck 'Bundletool' $java17 @('-jar',$bundletool,'version') $true @{} $bundletool
Add-VersionCheck 'UnityPy' $unityPython @('-c','import UnityPy; print(UnityPy.__version__)') $true
Add-VersionCheck 'Cpp2IL' $cpp2il @('--version') $false

# A real bounded Ghidra smoke test: import a tiny raw binary without auto-analysis.
if($ghidraHeadless -and (Test-Path -LiteralPath $ghidraHeadless)){
    $ghidraSmokeRoot=Join-Path $Output 'ghidra_smoke'
    New-Item -ItemType Directory -Path $ghidraSmokeRoot|Out-Null
    $ghidraSmokeInput=Join-Path $ghidraSmokeRoot 'ret.bin'
    [IO.File]::WriteAllBytes($ghidraSmokeInput,[byte[]](0xC3))
    $ghidraJavaHome=if($java21){Split-Path (Split-Path $java21 -Parent) -Parent}else{''}
    $ghidraResult=Invoke-CapturedCommand -FilePath "$env:SystemRoot\System32\cmd.exe" -Arguments @('/d','/c',$ghidraHeadless,$ghidraSmokeRoot,'bounded_smoke','-import',$ghidraSmokeInput,'-loader','BinaryLoader','-processor','x86:LE:32:default','-noanalysis','-deleteProject') -TimeoutSeconds 90 -Environment @{JAVA_HOME=$ghidraJavaHome}
    $ghidraCommand=Save-CommandResult $ghidraResult $logs 'smoke_ghidra_bounded_import'
    $ghidraOk=$ghidraResult.exit_code -eq 0 -and $ghidraResult.stdout -match 'Import succeeded'
    $checks += [ordered]@{name='Ghidra headless';status=if($ghidraOk){'runnable'}else{'failed'};required=$true;path=$ghidraHeadless;sha256=Get-Sha256 $ghidraHeadless;command=$ghidraCommand;smoke='tiny BinaryLoader import with -noanalysis'}
    if(-not$ghidraOk){$failures+='ghidra_bounded_smoke_failed'}
}else{
    $checks += [ordered]@{name='Ghidra headless';status='missing';required=$true;path=$ghidraHeadless}
    $failures+='missing_tool:Ghidra headless'
}

$smoke=[ordered]@{capture=$Capture;capture_status='missing';apk=$null;asset=$null;bundletool_device_spec=$null;il2cppdumper=$null}
if(-not $Capture){$failures+='no_current_capture'}
else{
    $captureJson=Join-Path $Capture 'capture.json'
    if(-not(Test-Path -LiteralPath $captureJson -PathType Leaf)){$failures+='capture_report_missing'}
    else{
        $captureData=Get-Content -LiteralPath $captureJson -Raw|ConvertFrom-Json
        $smoke.capture_status=$captureData.status
        if($captureData.status -notin @('collected','collected_with_warnings')){$failures+='capture_not_collected'}
        if(-not$DeviceSerial){$DeviceSerial=$captureData.mumu.selected_transport}
        $base=@($captureData.apk.items|Where-Object{$_.is_base -and $_.status -eq 'success'}|Select-Object -First 1)
        if($base.Count -ne 1){$failures+='base_apk_missing_for_smoke'}
        else{
            $apkResult=Invoke-CapturedCommand -FilePath $aapt2 -Arguments @('dump','badging',$base[0].local_path)
            $apkCommand=Save-CommandResult $apkResult $logs 'smoke_aapt2_current_base'
            $smoke.apk=[ordered]@{path=$base[0].local_path;sha256=Get-Sha256 $base[0].local_path;exit_code=$apkResult.exit_code;command=$apkCommand;status=if($apkResult.exit_code -eq 0 -and $apkResult.stdout -match "package: name="){'passed'}else{'failed'}}
            if($smoke.apk.status -ne 'passed'){$failures+='current_apk_smoke_failed'}
        }
        if($DeviceSerial){
            $deviceSpec=Join-Path $Output 'bundletool_device_spec.json'
            $specResult=Invoke-CapturedCommand -FilePath $java17 -Arguments @('-jar',$bundletool,'get-device-spec',("--output={0}"-f$deviceSpec),("--adb={0}"-f$adb),("--device-id={0}"-f$DeviceSerial))
            $specCommand=Save-CommandResult $specResult $logs 'smoke_bundletool_device_spec'
            $smoke.bundletool_device_spec=[ordered]@{exit_code=$specResult.exit_code;output=$deviceSpec;command=$specCommand;status=if($specResult.exit_code -eq 0 -and (Test-Path -LiteralPath $deviceSpec)){'passed'}else{'failed'}}
            if($smoke.bundletool_device_spec.status -ne 'passed'){$failures+='bundletool_device_spec_smoke_failed'}
        }
        $currentElf=Get-ChildItem -LiteralPath (Join-Path $Capture 'il2cpp') -Filter '*arm64-v8a_libil2cpp.so' -File -ErrorAction SilentlyContinue|Select-Object -First 1
        $currentMetadata=Get-ChildItem -LiteralPath (Join-Path $Capture 'il2cpp') -Filter '*global-metadata.dat' -File -ErrorAction SilentlyContinue|Select-Object -First 1
        if($dumper -and $currentElf -and $currentMetadata){
            $dumperOutput=Join-Path $Output 'il2cppdumper_smoke';New-Item -ItemType Directory -Path $dumperOutput|Out-Null
            $dumperResult=Invoke-CapturedCommand -FilePath $dumper -Arguments @($currentElf.FullName,$currentMetadata.FullName,$dumperOutput) -TimeoutSeconds 45
            $dumperCommand=Save-CommandResult $dumperResult $logs 'smoke_il2cppdumper_current_files'
            $dumpCs=Join-Path $dumperOutput 'dump.cs'
            $parsed=$dumperResult.exit_code -eq 0 -and (Test-Path -LiteralPath $dumpCs)
            $ranParser=$parsed -or (($dumperResult.stdout+"`n"+$dumperResult.stderr) -match '(?i)metadata|magic|version|registration|invalid')
            $checks += [ordered]@{name='Il2CppDumper';status=if($ranParser){'runnable'}else{'failed'};required=$true;path=$dumper;sha256=Get-Sha256 $dumper;command=$dumperCommand;smoke='current arm64 ELF plus current metadata'}
            $smoke.il2cppdumper=[ordered]@{elf=$currentElf.FullName;metadata=$currentMetadata.FullName;exit_code=$dumperResult.exit_code;command=$dumperCommand;parsed=$parsed;status=if($parsed){'passed'}elseif($ranParser){'partial'}else{'failed'};reason=if($parsed){$null}else{'tool ran, but current protected/encrypted metadata and ELF did not produce dump.cs'}}
            if(-not$parsed){$failures+='current_il2cpp_static_parse_failed'}
        }else{
            $checks += [ordered]@{name='Il2CppDumper';status='missing_input_or_tool';required=$true;path=$dumper}
            $smoke.il2cppdumper=[ordered]@{status='failed';reason='tool, current ELF, or current metadata missing'}
            $failures+='il2cppdumper_smoke_unavailable'
        }
        $assetAnalysis=Get-ChildItem -LiteralPath (Join-Path $root 'assets') -Filter asset_analysis.json -Recurse -File -ErrorAction SilentlyContinue|Sort-Object LastWriteTime -Descending|Where-Object{try{(Get-Content -LiteralPath $_.FullName -Raw|ConvertFrom-Json).input -eq $Capture}catch{$false}}|Select-Object -First 1
        if($assetAnalysis){
            $assetData=Get-Content -LiteralPath $assetAnalysis.FullName -Raw|ConvertFrom-Json
            $unityReportPath=$assetData.unitypy.report
            if($unityReportPath -and (Test-Path -LiteralPath $unityReportPath)){
                $unityData=Get-Content -LiteralPath $unityReportPath -Raw|ConvertFrom-Json
                $sample=@($unityData.items|Where-Object{$_.parse_status -eq 'success'}|Select-Object -First 1)
                if($sample.Count){
                    $sampleInput=Join-Path $Output 'asset_input';New-Item -ItemType Directory -Path $sampleInput|Out-Null
                    $sampleCopy=Join-Path $sampleInput ([IO.Path]::GetFileName($sample[0].source_path));Copy-Item -LiteralPath $sample[0].source_path -Destination $sampleCopy
                    $sampleOutput=Join-Path $Output 'asset_smoke'
                    $assetResult=Invoke-CapturedCommand -FilePath $unityPython -Arguments @((Join-Path $PSScriptRoot 'unity_asset_inventory.py'),$sampleInput,$sampleOutput,'--max-files','1','--max-exports-per-type','1')
                    $assetCommand=Save-CommandResult $assetResult $logs 'smoke_unitypy_current_asset'
                    $sampleReport=Join-Path $sampleOutput 'unity_asset_export.json';$sampleData=if(Test-Path -LiteralPath $sampleReport){Get-Content -LiteralPath $sampleReport -Raw|ConvertFrom-Json}else{$null}
                    $sampleOk=$assetResult.exit_code -eq 0 -and $sampleData -and $sampleData.summary.unity_parse_success -gt 0
                    $smoke.asset=[ordered]@{source=$sample[0].source_path;source_sha256=Get-Sha256 $sample[0].source_path;exit_code=$assetResult.exit_code;command=$assetCommand;report=$sampleReport;status=if($sampleOk){'passed'}else{'failed'}}
                }
            }
        }
        if(-not$smoke.asset){$smoke.asset=[ordered]@{status='failed';reason='no successfully parsed current Unity asset was available for smoke test'};$failures+='current_asset_smoke_unavailable'}
        elseif($smoke.asset.status -ne 'passed'){$failures+='current_asset_smoke_failed'}
    }
}

$passed=$failures.Count -eq 0
$report=Join-Path $Output 'verify_toolchain.json'
Write-JsonReport $report ([ordered]@{passed=$passed;status=if($passed){'passed'}else{'failed'};failures=@($failures|Select-Object -Unique);tool_checks=$checks;smoke_tests=$smoke;outputs=Get-RelativeFileRecords $Output})
Write-Output ([ordered]@{passed=$passed;report=$report;failures=@($failures|Select-Object -Unique)}|ConvertTo-Json -Depth 4 -Compress)
if(-not$passed){exit 6}
