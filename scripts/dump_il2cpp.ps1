param([Parameter(Mandatory=$true)][string]$LibIl2Cpp,[Parameter(Mandatory=$true)][string]$Metadata,[ValidateSet('static','runtime')][string]$Method='static',[string]$GamePath='',[string]$UnityVersion='',[string]$Output='')
. (Join-Path $PSScriptRoot 'common.ps1')
$lib=(Resolve-Path -LiteralPath $LibIl2Cpp).Path; $meta=(Resolve-Path -LiteralPath $Metadata).Path
if(-not $Output){$Output=New-VersionedDirectory (Join-Path (Get-ToolchainRoot) 'il2cpp') ("{0}_{1}" -f $Method,(Split-Path $lib -Leaf))}elseif(Test-Path -LiteralPath $Output){throw "Refusing existing output: $Output"}else{New-Item -ItemType Directory -Path $Output | Out-Null}
Copy-Item -LiteralPath $lib -Destination (Join-Path $Output 'libil2cpp.so')
Copy-Item -LiteralPath $meta -Destination (Join-Path $Output 'global-metadata.dat')
$dumper=Get-ChildItem -LiteralPath (Join-Path (Get-ToolchainRoot) 'tools') -Filter 'Il2CppDumper.exe' -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
$cpp=Get-ChildItem -LiteralPath (Join-Path (Get-ToolchainRoot) 'tools') -Filter 'Cpp2IL.exe' -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
if(-not $cpp){
    $requirements=Get-Content -LiteralPath (Join-Path (Get-ToolchainRoot) 'tools\tool_requirements.json') -Raw | ConvertFrom-Json
    foreach($candidate in ($requirements.existing | Where-Object {$_.name -like 'Cpp2IL*'} | Sort-Object name -Descending)){
        $candidatePath=$candidate.path.Replace('/','\')
        if(Test-Path -LiteralPath $candidatePath -PathType Leaf){$cpp=Get-Item -LiteralPath $candidatePath; break}
    }
}
$attempts=@();
if($dumper){
    $out=Join-Path $Output 'il2cppdumper'; New-Item -ItemType Directory -Path $out | Out-Null
    $oldPreference=$ErrorActionPreference; $ErrorActionPreference='Continue'
    $dumperCommand='echo 0|"{0}" "{1}" "{2}" "{3}"' -f $dumper.FullName,$lib,$meta,$out
    $dumperLog=Join-Path $out 'run.log'
    & cmd.exe /d /c $dumperCommand 2>&1 | Set-Content -LiteralPath $dumperLog -Encoding UTF8
    $dumperExit=$LASTEXITCODE; $ErrorActionPreference=$oldPreference
    $dumperStatus='failed'; $dumperFailure='Il2CppDumper exited without producing a verified dump'
    if($dumperExit -eq 0){
        $dumperStatus='completed';$dumperFailure=$null
    }else{
        $dumperText=Get-Content -LiteralPath $dumperLog -Raw -ErrorAction SilentlyContinue
        if($dumperText -match 'System\.OverflowException'){
            $dumperFailure='metadata parsed, but cropped/protected ELF registration search produced an invalid range and overflowed; redirected ReadKey failure is secondary'
        }elseif($dumperText -match 'Metadata file (supplied is not valid metadata file|not found or encrypted)'){
            $dumperFailure='metadata is encrypted, incomplete, or incompatible with this Il2CppDumper version'
        }elseif($dumperText -match 'This file may be protected'){
            $dumperFailure='cropped/protected ELF prevented reliable registration discovery'
        }
    }
    $attempts += [ordered]@{tool='Il2CppDumper';status=$dumperStatus;exit_code=$dumperExit;output=$out;failure_reason=$dumperFailure}
}else{$attempts += [ordered]@{tool='Il2CppDumper';status='missing'}}
if($cpp -and $GamePath){
    $out=Join-Path $Output 'cpp2il'; New-Item -ItemType Directory -Path $out | Out-Null
    $oldPreference=$ErrorActionPreference; $ErrorActionPreference='Continue'
    $game=(Resolve-Path -LiteralPath $GamePath).Path
    $cppArgs=@('--game-path',$game,'--force-binary-path',$lib,'--force-metadata-path',$meta,'--output-to',$out)
    if($UnityVersion){$cppArgs+=@('--force-unity-version',$UnityVersion)}
    & $cpp.FullName @cppArgs 2>&1 | Set-Content -LiteralPath (Join-Path $out 'run.log') -Encoding UTF8
    $cppExit=$LASTEXITCODE; $ErrorActionPreference=$oldPreference
    $cppStatus='failed'; $cppFailure='Cpp2IL failed; inspect the preserved run.log'; if($cppExit -eq 0){$cppStatus='completed';$cppFailure=$null}else{$cppText=Get-Content -LiteralPath (Join-Path $out 'run.log') -Raw -ErrorAction SilentlyContinue;if($cppText -match 'ProcessSymbols|Sequence contains no elements|no elements'){$cppFailure='cropped ELF symbol table has no usable entries; LibCpp2IL.Elf.ElfFile.ProcessSymbols failed'}elseif($cppText -match 'Invalid or corrupt metadata|magic number check failed'){$cppFailure='metadata magic check failed; current client metadata is encrypted or transformed'}elseif($cppText -match 'metadata'){$cppFailure='Cpp2IL could not initialize the supplied metadata/game layout'}}
    $attempts += [ordered]@{tool='Cpp2IL';status=$cppStatus;exit_code=$cppExit;output=$out;failure_reason=$cppFailure}
}elseif(-not $cpp){$attempts += [ordered]@{tool='Cpp2IL';status='missing'}}else{$attempts += [ordered]@{tool='Cpp2IL';status='blocked';failure_reason='Cpp2IL requires a Unity game directory; raw libil2cpp input was not passed through a guessed CLI contract'}}
$files=Get-RelativeFileRecords $Output
Write-JsonReport (Join-Path $Output 'il2cpp_analysis.json') ([ordered]@{status='partial';method=$Method;inputs=@([ordered]@{path=$lib;sha256=(Get-Sha256 $lib)},[ordered]@{path=$meta;sha256=(Get-Sha256 $meta)});attempts=$attempts;existing_runtime_dump='preserved separately';outputs=$files;limitations=@('type/method counts are derived only when a tool succeeds','unverified RVA/VA must not be treated as runtime truth')})
Write-Output (@{status='partial';output=$Output;attempts=$attempts.Count} | ConvertTo-Json -Compress)
