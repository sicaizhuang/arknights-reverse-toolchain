param(
    [Parameter(Mandatory=$true)][string]$Elf,
    [string]$MethodMap='',
    [string]$Output='',
    [int]$AnalysisTimeoutSeconds=3600,
    [int]$FocusFunctionLimit=64
)
. (Join-Path $PSScriptRoot 'common.ps1')

$elf=(Resolve-Path -LiteralPath $Elf).Path
if(-not $Output){$Output=New-VersionedDirectory (Join-Path (Get-ToolchainRoot) 'native') ([IO.Path]::GetFileNameWithoutExtension($elf))}
elseif(Test-Path -LiteralPath $Output){throw "Refusing existing output: $Output"}
else{New-Item -ItemType Directory -Path $Output|Out-Null}
$bytes=[IO.File]::ReadAllBytes($elf);$isElf=$bytes.Length-ge 4 -and $bytes[0]-eq 0x7F -and $bytes[1]-eq 0x45 -and $bytes[2]-eq 0x4C -and $bytes[3]-eq 0x46
$ghidra=(Get-ChildItem -LiteralPath (Join-Path (Get-ToolchainRoot) 'tools') -Filter analyzeHeadless.bat -Recurse -File|Select-Object -First 1).FullName
$java=(Get-ChildItem -LiteralPath (Join-Path (Get-ToolchainRoot) 'tools\jdk21') -Filter java.exe -Recurse -File|Select-Object -First 1).FullName
$focusScript=Join-Path $PSScriptRoot 'ghidra\FocusKeywordFunctions.java'
$status='failed_not_elf';$ghidraCommand=$null;$focusPath=Join-Path $Output 'focus_pseudocode.txt';$logPath=Join-Path $Output 'ghidra_headless.log';$analysisStatus='not_attempted';$focusStatus='not_attempted'
if($isElf -and $ghidra -and $java){
    $projectRoot=Join-Path $Output 'ghidra_project';New-Item -ItemType Directory -Path $projectRoot|Out-Null;$projectName='current_il2cpp_analysis'
    $scriptRoot=Join-Path $PSScriptRoot 'ghidra'
    $args=@('/d','/c',$ghidra,$projectRoot,$projectName,'-import',$elf,'-analysisTimeoutPerFile',[string]$AnalysisTimeoutSeconds,'-scriptPath',$scriptRoot,'-postScript','FocusKeywordFunctions.java',$focusPath,[string]$FocusFunctionLimit)
    if($MethodMap){
        $resolvedMap=(Resolve-Path -LiteralPath $MethodMap).Path
        $args+=@('-postScript','ImportIl2CppSymbols.java',$resolvedMap)
    }
    $javaHome=Split-Path (Split-Path $java -Parent) -Parent
    $result=Invoke-CapturedCommand -FilePath "$env:SystemRoot\System32\cmd.exe" -Arguments $args -TimeoutSeconds ($AnalysisTimeoutSeconds+300) -Environment @{JAVA_HOME=$javaHome}
    $ghidraCommand=Save-CommandResult $result $Output 'ghidra_headless'
    [IO.File]::WriteAllText($logPath,(($result.stdout+"`r`n"+$result.stderr)),[Text.UTF8Encoding]::new($false))
    $combinedOutput=("{0}`n{1}" -f [string]$result.stdout,[string]$result.stderr)
    $timedOut=($result.launch_error -match 'timed out') -or ($combinedOutput -match 'Analysis timed out')
    $analysisStatus=if($timedOut){'timed_out'}elseif($result.exit_code -eq 0){'completed'}else{'failed'}
    $focusStatus=if(Test-Path -LiteralPath $focusPath -PathType Leaf){'generated'}elseif($combinedOutput -match 'FocusKeywordFunctions|postScript.*failed|class could not be found|skipping'){ 'failed' }else{'missing'}
    if($analysisStatus -eq 'completed' -and $focusStatus -eq 'generated'){$status='analyzed'}
    elseif($analysisStatus -eq 'timed_out'){$status='partial_timeout'}
    else{$status='partial_focus_failed'}
}elseif($isElf){$status='blocked_missing_ghidra_or_java'}
$records=Get-RelativeFileRecords $Output
Write-JsonReport (Join-Path $Output 'native_analysis.json') ([ordered]@{status=$status;input=$elf;input_sha256=Get-Sha256 $elf;elf=$isElf;architecture='AARCH64:LE:64:v8A expected; Ghidra performs the authoritative import';ghidra=$ghidra;java=$java;auto_analysis=$true;analysis_status=$analysisStatus;focus_status=$focusStatus;method_map=if($MethodMap){(Resolve-Path -LiteralPath $MethodMap).Path}else{$null};focus_keywords=@('operator','animation','attack','summon','projectile','damage','wisadel','voice','audio','soul');focus_limit=$FocusFunctionLimit;focus_output=$focusPath;command=$ghidraCommand;outputs=$records;warning='Ghidra pseudocode is reconstructed evidence, not original C/C++ or C# source'})
Write-Output ([ordered]@{status=$status;output=$Output;elf=$isElf;auto_analysis=$true;ghidra=($null -ne $ghidra);focus_output=$focusPath}|ConvertTo-Json -Compress)
if($status -ne 'analyzed'){exit 7}
