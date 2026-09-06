param([Parameter(ValueFromRemainingArguments=$true)][string[]]$ForwardArgs)
$legacy = Join-Path $PSScriptRoot '..\dump_il2cpp.ps1'
& $legacy @ForwardArgs
exit $LASTEXITCODE
