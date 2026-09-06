param(
    [string]$Output = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
if (-not $Output) {
    $stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
    $Output = Join-Path $root ("tests\toolchain_health_{0}" -f $stamp)
}
if (Test-Path -LiteralPath $Output) { throw "Refusing to overwrite output: $Output" }

$python = Join-Path $root 'tools\unitypy\Scripts\python.exe'
$aapt2 = (Get-ChildItem -LiteralPath (Join-Path $root 'tools\aapt2') -Filter aapt2.exe -Recurse -File | Select-Object -First 1).FullName
$apktool = (Get-ChildItem -LiteralPath (Join-Path $root 'tools\apktool') -Filter 'apktool*.jar' -Recurse -File | Select-Object -First 1).FullName
$jadx = (Get-ChildItem -LiteralPath (Join-Path $root 'tools\jadxcli') -Filter jadx.bat -Recurse -File | Select-Object -First 1).FullName
$bundletool = (Get-ChildItem -LiteralPath (Join-Path $root 'tools\bundletool') -Filter 'bundletool*.jar' -Recurse -File | Select-Object -First 1).FullName
$java = (Get-ChildItem -LiteralPath (Join-Path $root 'tools') -Filter java.exe -Recurse -File | Where-Object { $_.FullName -match 'jdk' } | Select-Object -First 1).FullName
$required = @($python, $aapt2, $apktool, $jadx, $bundletool, $java)
if ($required | Where-Object { -not $_ -or -not (Test-Path -LiteralPath $_ -PathType Leaf) }) {
    throw 'A required health-check tool is missing.'
}
$env:JAVA_HOME = Split-Path (Split-Path $java -Parent) -Parent

& $python (Join-Path $root 'scripts\fixtures\generate_fixtures.py') --force
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $python (Join-Path $root 'scripts\verify\probe_fixture_formats.py') `
    --fixtures (Join-Path $root 'fixtures') `
    --output $Output `
    --aapt2 $aapt2 `
    --java $java `
    --apktool $apktool `
    --jadx $jadx `
    --bundletool $bundletool `
    --python $python `
    --unitypy-python $python
exit $LASTEXITCODE
