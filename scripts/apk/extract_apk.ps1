param([Parameter(ValueFromRemainingArguments=$true)][string[]]$ForwardArgs)
$legacy = Join-Path $PSScriptRoot '..\extract_apk.ps1'
& $legacy @ForwardArgs
exit $LASTEXITCODE
