param([Parameter(ValueFromRemainingArguments=$true)][string[]]$ForwardArgs)
$legacy = Join-Path $PSScriptRoot '..\collect_client.ps1'
& $legacy @ForwardArgs
exit $LASTEXITCODE
