<#
.SYNOPSIS
    Small, portable router for the public read-only adapters.

.DESCRIPTION
    Private chat integrations and machine-specific launchers are not
    part of this repository.  Commands forward their remaining arguments to
    the corresponding adapter under this repository.
#>
param(
    [ValidateSet('health','collect','apk','assets','assets-lz4ak','assets-inventory','assets-batch','operator-pack','operator-closure-validate','effects','effects-timeline','effects-timeline-batch','static','pc-assets-probe','pc-external-cab-resolve','pc-monoscript-closure','pc-skill-effect-join','pc-code-index','pc-code-query','pc-code-map','pc-code-trace','pc-code-verify','pc-code-surface','query','help')]
    [string]$Command = 'help',
    [ValidateSet('arknights','arknights_pc','pvz2')]
    [string]$Profile = 'arknights',
    [string]$Output = '',
    [Parameter(ValueFromRemainingArguments=$true)]
    [string[]]$ForwardArgs
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $root
$python = if ($env:ARKNIGHTS_TOOLCHAIN_PYTHON) { $env:ARKNIGHTS_TOOLCHAIN_PYTHON }
          elseif (Test-Path (Join-Path $root '.venv\Scripts\python.exe')) { Join-Path $root '.venv\Scripts\python.exe' }
          else { 'python' }

function Invoke-PythonScript([string]$script, [string[]]$args) {
    if (-not (Test-Path -LiteralPath $script -PathType Leaf)) { throw "Script not found: $script" }
    & $python $script @args
    exit $LASTEXITCODE
}

if ($Command -eq 'help') {
    @'
Public read-only routes:
  health, collect, apk, assets, assets-lz4ak, assets-inventory, assets-batch,
  operator-pack, operator-closure-validate, effects, effects-timeline,
  effects-timeline-batch, static, pc-assets-probe, pc-external-cab-resolve,
  pc-monoscript-closure, pc-skill-effect-join, pc-code-index/query/map/trace/
  verify/surface, query.

Private chat-record integration, private evidence, runtime injection and bundled
game/client files are intentionally excluded.  Use docs/USAGE_PUBLIC.md.
'@
    exit 0
}

$profilePath = Join-Path $root ("targets\{0}\profile.json" -f $Profile)
switch ($Command) {
    'health' { & (Join-Path $root 'scripts\verify\verify_toolchain.ps1') @ForwardArgs; exit $LASTEXITCODE }
    'collect' { & (Join-Path $root 'scripts\collect\collect_client.ps1') @ForwardArgs; exit $LASTEXITCODE }
    'apk' { & (Join-Path $root 'scripts\apk\extract_apk.ps1') @ForwardArgs; exit $LASTEXITCODE }
    'assets' { & (Join-Path $root 'targets\arknights\run_animestudio_assets.ps1') -ProfilePath $profilePath @ForwardArgs; exit $LASTEXITCODE }
    'assets-lz4ak' { & (Join-Path $root 'targets\arknights\run_ark_unpacker_probe.ps1') -ProfilePath $profilePath @ForwardArgs; exit $LASTEXITCODE }
    'assets-inventory' { & (Join-Path $root 'targets\arknights\run_animestudio_inventory.ps1') -ProfilePath $profilePath @ForwardArgs; exit $LASTEXITCODE }
    'assets-batch' { & (Join-Path $root 'targets\arknights\run_animestudio_batch.ps1') -ProfilePath $profilePath @ForwardArgs; exit $LASTEXITCODE }
    'operator-pack' { Invoke-PythonScript (Join-Path $root 'targets\arknights\build_operator_pack.py') @ForwardArgs }
    'operator-closure-validate' { Invoke-PythonScript (Join-Path $root 'scripts\validate_operator_closure_matrix.py') @ForwardArgs }
    'effects' { & (Join-Path $root 'targets\arknights\effects\run_effects.ps1') -ProfilePath $profilePath @ForwardArgs; exit $LASTEXITCODE }
    'effects-timeline' { & (Join-Path $root 'targets\arknights\effects\run_effect_timeline.ps1') -ProfilePath $profilePath @ForwardArgs; exit $LASTEXITCODE }
    'effects-timeline-batch' { & (Join-Path $root 'targets\arknights\effects\run_effect_timeline_batch.ps1') -ProfilePath $profilePath @ForwardArgs; exit $LASTEXITCODE }
    'static' { & (Join-Path $root 'targets\arknights\run_static_module.ps1') -ProfilePath $profilePath @ForwardArgs; exit $LASTEXITCODE }
    'pc-assets-probe' { Invoke-PythonScript (Join-Path $root 'targets\arknights_pc\pc_asset_probe.py') @ForwardArgs }
    'pc-external-cab-resolve' { Invoke-PythonScript (Join-Path $root 'targets\arknights_pc\resolve_external_cab.py') @ForwardArgs }
    'pc-monoscript-closure' { Invoke-PythonScript (Join-Path $root 'targets\arknights_pc\close_monoscript_objects.py') @ForwardArgs }
    'pc-skill-effect-join' { Invoke-PythonScript (Join-Path $root 'targets\arknights_pc\join_skill_effect_evidence.py') @ForwardArgs }
    'pc-code-index' { Invoke-PythonScript (Join-Path $root 'targets\arknights_pc\code_reference_index.py') @ForwardArgs }
    'pc-code-query' { Invoke-PythonScript (Join-Path $root 'targets\arknights_pc\code_reference_index.py') @ForwardArgs }
    'pc-code-map' { Invoke-PythonScript (Join-Path $root 'targets\arknights_pc\code_reference_index.py') @ForwardArgs }
    'pc-code-trace' { Invoke-PythonScript (Join-Path $root 'targets\arknights_pc\code_reference_index.py') @ForwardArgs }
    'pc-code-verify' { Invoke-PythonScript (Join-Path $root 'targets\arknights_pc\verify_current_pc_edges.py') @ForwardArgs }
    'pc-code-surface' { Invoke-PythonScript (Join-Path $root 'targets\arknights_pc\verify_operator_code_surface.py') @ForwardArgs }
    'query' { Invoke-PythonScript (Join-Path $root 'targets\arknights\query_workbench.py') @ForwardArgs }
    default { throw "Unsupported public command: $Command" }
}
