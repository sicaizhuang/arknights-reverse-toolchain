param([Parameter(ValueFromRemainingArguments=$true)][string[]]$ForwardArgs)
$legacy = Join-Path $PSScriptRoot '..\analyze_native.ps1'
& $legacy @ForwardArgs
exit $LASTEXITCODE
