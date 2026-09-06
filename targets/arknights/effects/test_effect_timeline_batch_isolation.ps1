param([Parameter(Mandatory=$true)][string]$Output)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
function Write-Utf8NoBom([string]$Path,[string]$Text){[IO.File]::WriteAllText($Path,$Text,[Text.UTF8Encoding]::new($false))}

$outputFull=[IO.Path]::GetFullPath($Output)
if(Test-Path -LiteralPath $outputFull){throw "Output already exists: $outputFull"}
New-Item -ItemType Directory -Path $outputFull|Out-Null
$forbiddenOutput=Join-Path $outputFull 'pvz2_forbidden_output'
$stdout=Join-Path $outputFull 'stdout.txt'
$stderr=Join-Path $outputFull 'stderr.txt'
$root=(Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
$cli=Join-Path $root 'scripts\toolchain.ps1'
$arguments=@('-NoProfile','-ExecutionPolicy','Bypass','-File',$cli,'-Command','effects-timeline-batch','-Profile','pvz2','-InputBundle','D:\nonexistent.ab','-AssetPattern','^x$','-Output',$forbiddenOutput)
$process=Start-Process -FilePath 'powershell.exe' -ArgumentList $arguments -PassThru -Wait -RedirectStandardOutput $stdout -RedirectStandardError $stderr
$exitCode=[int]$process.ExitCode
$passed=$exitCode -ne 0 -and -not(Test-Path -LiteralPath $forbiddenOutput)
$command=[ordered]@{executable='powershell.exe';arguments=$arguments;exit_code=$exitCode;stdout=$stdout;stderr=$stderr}
$report=[ordered]@{schema_version=1;created_utc=[DateTime]::UtcNow.ToString('o');status=if($passed){'passed'}else{'failed'};route='effects-timeline-batch';requested_profile='pvz2';exit_code=$exitCode;output_created=(Test-Path -LiteralPath $forbiddenOutput);expected='nonzero_exit_and_no_output'}
Write-Utf8NoBom (Join-Path $outputFull 'command.json') (($command|ConvertTo-Json -Depth 6)+"`n")
Write-Utf8NoBom (Join-Path $outputFull 'profile_isolation.json') (($report|ConvertTo-Json -Depth 6)+"`n")
if(-not $passed){exit 1}
exit 0
