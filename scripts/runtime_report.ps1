param([string]$AdbPath='',[string]$PackageName='',[string]$Output='')
. (Join-Path $PSScriptRoot 'common.ps1')
$adb=Resolve-Adb $AdbPath; if(-not $Output){$Output=Join-Path (Get-ToolchainRoot) 'reports\runtime_report.json'}
$devices=@(& $adb devices 2>&1 | Select-Object -Skip 1 | Where-Object {$_ -match '\S+\s+device\b'})
if($devices.Count -eq 0){Write-JsonReport $Output ([ordered]@{status='blocked';reason='no_authorized_mumu_device';adb=$adb;adb_sha256=(Get-Sha256 $adb);observations=@()}); Write-Output (@{status='blocked';report=$Output} | ConvertTo-Json -Compress); exit 2}
$serial=(($devices[0] -split '\s+')[0]); $ps=@(& $adb -s $serial shell ps -A 2>&1 | Select-Object -First 500 | ForEach-Object {$_.ToString()}); $top=@(& $adb -s $serial shell dumpsys activity activities 2>&1 | Select-Object -First 120 | ForEach-Object {$_.ToString()});
Write-JsonReport $Output ([ordered]@{status='ok';device=$serial;package=$PackageName;observations=@([ordered]@{kind='process_list';lines=$ps},[ordered]@{kind='activity_state';lines=$top});safety=@('no code injection','no server/account/anti-cheat bypass','stop by ending this script')})
Write-Output (@{status='ok';report=$Output;device=$serial} | ConvertTo-Json -Compress)
