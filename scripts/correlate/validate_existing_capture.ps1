param([Parameter(ValueFromRemainingArguments=$true)][string[]]$ForwardArgs)
$legacy = Join-Path $PSScriptRoot '..\validate_existing_capture.ps1'
& $legacy @ForwardArgs
exit $LASTEXITCODE
