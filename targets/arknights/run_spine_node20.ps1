[CmdletBinding()]
param(
    [string]$ScriptPath,
    [string]$PvzOperatorRoot,
    [string]$RuntimeRoot,
    [string[]]$ScriptArguments = @(),
    [string]$ReportPath,
    [switch]$Probe
)

$ErrorActionPreference = 'Stop'
if (-not $PvzOperatorRoot) {
    $documents = [Environment]::GetFolderPath('MyDocuments')
    $PvzOperatorRoot = Get-ChildItem -LiteralPath $documents -Directory |
        Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'pvz_operator_mod') -PathType Container } |
        Select-Object -First 1 -ExpandProperty FullName
    if ($PvzOperatorRoot) { $PvzOperatorRoot = Join-Path $PvzOperatorRoot 'pvz_operator_mod' }
}
if (-not $RuntimeRoot -and $PvzOperatorRoot) {
    $RuntimeRoot = Join-Path $PvzOperatorRoot 'tools\spine-exporter-node20-clean'
}
$nodePath = if ($PvzOperatorRoot) {
    Join-Path $PvzOperatorRoot 'tools\node-v20.20.2-win-x64\node.exe'
} else {
    ''
}
$canvasPath = if ($RuntimeRoot) {
    Join-Path $RuntimeRoot 'node_modules\canvas\build\Release\canvas.node'
} else {
    ''
}

$result = [ordered]@{
    schema = 'spine_node_runtime_probe.v1'
    node = $nodePath
    runtimeRoot = $RuntimeRoot
    canvasNativeModule = $canvasPath
    expectedNode = 'v20.20.2'
    expectedAbi = '115'
    nodeExists = Test-Path -LiteralPath $nodePath -PathType Leaf
    runtimeExists = Test-Path -LiteralPath $RuntimeRoot -PathType Container
    canvasExists = Test-Path -LiteralPath $canvasPath -PathType Leaf
    version = $null
    abi = $null
    status = 'node20_runtime_missing'
}

if ($result.nodeExists) {
    $probeJson = & $nodePath -e "process.stdout.write(JSON.stringify({version:process.version,abi:process.versions.modules,execPath:process.execPath}))"
    if ($LASTEXITCODE -eq 0) {
        $nodeProbe = $probeJson | ConvertFrom-Json
        $result.version = [string]$nodeProbe.version
        $result.abi = [string]$nodeProbe.abi
        if (-not $result.runtimeExists -or -not $result.canvasExists) {
            $result.status = 'runtime_dependency_missing'
        } elseif ($result.version -ne $result.expectedNode -or $result.abi -ne $result.expectedAbi) {
            $result.status = 'abi_mismatch'
        } else {
            $result.status = 'passed'
        }
    } else {
        $result.status = 'node_probe_failed'
    }
}

$json = $result | ConvertTo-Json -Depth 5
if ($ReportPath) {
    $reportParent = Split-Path -Parent $ReportPath
    if ($reportParent) { New-Item -ItemType Directory -Force -Path $reportParent | Out-Null }
    Set-Content -LiteralPath $ReportPath -Value $json -Encoding UTF8
}
Write-Output $json
if ($result.status -ne 'passed') { exit 2 }
if ($Probe) { exit 0 }
if (-not $ScriptPath) { throw 'ScriptPath is required unless -Probe is specified' }
if (-not (Test-Path -LiteralPath $ScriptPath -PathType Leaf)) { throw "Spine script not found: $ScriptPath" }

Push-Location $RuntimeRoot
try {
    & $nodePath $ScriptPath @ScriptArguments
    $scriptExit = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $scriptExit
