Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:ToolchainRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

function Get-ToolchainRoot { return $script:ToolchainRoot }

function Get-Sha256([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "File not found: $Path" }
    return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToUpperInvariant()
}

function Get-RelativeFileRecords([string]$Root) {
    $resolved = (Resolve-Path -LiteralPath $Root).Path
    $records = @()
    foreach ($file in (Get-ChildItem -LiteralPath $resolved -File -Recurse)) {
        $relative = $file.FullName.Substring($resolved.Length).TrimStart('\','/')
        $records += [ordered]@{
            path = $relative.Replace('\','/')
            size = [int64]$file.Length
            last_write_utc = $file.LastWriteTimeUtc.ToString('o')
            sha256 = Get-Sha256 $file.FullName
        }
    }
    return $records
}

function Write-JsonReport([string]$Path, [hashtable]$Data) {
    $parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    $Data['schema_version'] = 1
    $Data['created_utc'] = [DateTime]::UtcNow.ToString('o')
    $Data | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $Path -Encoding UTF8
}

function New-VersionedDirectory([string]$Parent, [string]$Prefix) {
    New-Item -ItemType Directory -Path $Parent -Force | Out-Null
    $stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
    $path = Join-Path $Parent ("{0}_{1}" -f $Prefix,$stamp)
    if (Test-Path -LiteralPath $path) { throw "Refusing existing output: $path" }
    New-Item -ItemType Directory -Path $path | Out-Null
    return $path
}

function Resolve-Adb([string]$ExplicitPath) {
    if ($ExplicitPath) { if (-not (Test-Path -LiteralPath $ExplicitPath -PathType Leaf)) { throw "ADB not found: $ExplicitPath" }; return (Resolve-Path -LiteralPath $ExplicitPath).Path }
    $configured = Join-Path $script:ToolchainRoot 'tools\adb.exe'
    if (Test-Path -LiteralPath $configured -PathType Leaf) { return $configured }
    $known = 'D:\Program Files\Netease\MuMu\nx_main\adb.exe'
    if (Test-Path -LiteralPath $known -PathType Leaf) { return $known }
    $command = Get-Command adb -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    throw 'No ADB executable is configured.'
}

function Get-CommandVersion([string]$Path, [string[]]$Arguments=@('--version')) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return @{status='missing'; path=$Path} }
    try {
        $lines = @(& $Path @Arguments 2>&1 | Select-Object -First 5 | ForEach-Object { $_.ToString() })
        return @{status='ok'; path=$Path; output=($lines -join "`n")}
    } catch {
        return @{status='error'; path=$Path; error=$_.Exception.Message}
    }
}

function ConvertTo-NativeArgument([string]$Value) {
    if ($null -eq $Value -or $Value.Length -eq 0) { return '""' }
    if ($Value -notmatch '[\s"]') { return $Value }
    return '"' + $Value.Replace('"','\"') + '"'
}

function Invoke-CapturedCommand {
    param(
        [Parameter(Mandatory=$true)][string]$FilePath,
        [string[]]$Arguments=@(),
        [string]$WorkingDirectory='',
        [int]$TimeoutSeconds=0,
        [hashtable]$Environment=@{}
    )
    $started=[DateTime]::UtcNow
    $result=[ordered]@{
        executable=$FilePath
        arguments=@($Arguments)
        command_line=($FilePath + ' ' + (($Arguments | ForEach-Object { ConvertTo-NativeArgument $_ }) -join ' ')).Trim()
        started_utc=$started.ToString('o')
        duration_ms=0
        exit_code=$null
        stdout=''
        stderr=''
        launch_error=$null
    }
    try {
        $info=[System.Diagnostics.ProcessStartInfo]::new()
        $info.FileName=$FilePath
        $info.Arguments=($Arguments | ForEach-Object { ConvertTo-NativeArgument $_ }) -join ' '
        $info.UseShellExecute=$false
        $info.RedirectStandardOutput=$true
        $info.RedirectStandardError=$true
        $info.CreateNoWindow=$true
        if($WorkingDirectory){$info.WorkingDirectory=$WorkingDirectory}
        foreach($key in $Environment.Keys){$info.EnvironmentVariables[[string]$key]=[string]$Environment[$key]}
        $process=[System.Diagnostics.Process]::new()
        $process.StartInfo=$info
        if(-not $process.Start()){throw 'ProcessStart returned false'}
        $stdoutTask=$process.StandardOutput.ReadToEndAsync()
        $stderrTask=$process.StandardError.ReadToEndAsync()
        if($TimeoutSeconds -gt 0 -and -not $process.WaitForExit($TimeoutSeconds*1000)){
            $process.Kill()
            $process.WaitForExit()
            $result.launch_error=("timed out after {0} seconds" -f $TimeoutSeconds)
        }else{$process.WaitForExit()}
        $result.exit_code=$process.ExitCode
        $result.stdout=$stdoutTask.Result
        $result.stderr=$stderrTask.Result
        $process.Dispose()
    } catch {
        $result.launch_error=$_.Exception.Message
        $result.exit_code=-1
    }
    $result.duration_ms=[int64]([DateTime]::UtcNow-$started).TotalMilliseconds
    return [pscustomobject]$result
}

function Save-CommandResult {
    param(
        [Parameter(Mandatory=$true)]$Result,
        [Parameter(Mandatory=$true)][string]$LogDirectory,
        [Parameter(Mandatory=$true)][string]$Name
    )
    New-Item -ItemType Directory -Path $LogDirectory -Force | Out-Null
    $safe=($Name -replace '[^0-9A-Za-z._-]','_')
    $stdoutPath=Join-Path $LogDirectory ($safe + '.stdout.txt')
    $stderrPath=Join-Path $LogDirectory ($safe + '.stderr.txt')
    $commandPath=Join-Path $LogDirectory ($safe + '.command.json')
    [System.IO.File]::WriteAllText($stdoutPath,[string]$Result.stdout,[System.Text.UTF8Encoding]::new($false))
    [System.IO.File]::WriteAllText($stderrPath,[string]$Result.stderr,[System.Text.UTF8Encoding]::new($false))
    $record=[ordered]@{
        executable=$Result.executable
        arguments=@($Result.arguments)
        command_line=$Result.command_line
        started_utc=$Result.started_utc
        duration_ms=$Result.duration_ms
        exit_code=$Result.exit_code
        launch_error=$Result.launch_error
        stdout_path=$stdoutPath
        stderr_path=$stderrPath
    }
    [System.IO.File]::WriteAllText($commandPath,($record|ConvertTo-Json -Depth 6),[System.Text.UTF8Encoding]::new($false))
    $record.command_record=$commandPath
    return [pscustomobject]$record
}
