param([ValidateSet('core','unity','native','all')][string]$Layer='core',[string]$Kind='',[switch]$WhatIf)
. (Join-Path $PSScriptRoot 'common.ps1')
$root=Get-ToolchainRoot; $toolRoot=Join-Path $root 'tools'; $manifest=Get-Content -LiteralPath (Join-Path $toolRoot 'tool_requirements.json') -Raw | ConvertFrom-Json
$layers=@{core=@('jdk','jadxcli','apktool','bundletool','aapt2','il2cppdumper');unity=@();native=@('jdk21','ghidra')}
$selected=@(); if($Kind){$selected=@($Kind)}elseif($Layer -eq 'all'){$selected=$layers.core+$layers.unity+$layers.native}else{$selected=$layers[$Layer]}; $results=@()
foreach($kind in $selected){
    $item=$manifest.planned_downloads | Where-Object {$_.kind -eq $kind} | Select-Object -First 1
    if(-not $item){$results += [ordered]@{kind=$kind;status='missing_requirement'}; continue}
    $archive=Join-Path $toolRoot (Split-Path $item.url -Leaf)
    if(-not (Test-Path -LiteralPath $archive)){
        if($WhatIf){$results += [ordered]@{kind=$kind;status='planned';url=$item.url;expected_sha256=$item.sha256}; continue}
        Invoke-WebRequest -Uri $item.url -OutFile $archive -UseBasicParsing
    }
    $sha=Get-Sha256 $archive
    if($item.sha256 -notlike 'UNVERIFIED*' -and $sha -ne $item.sha256){throw "Hash mismatch for archive: $archive"}
    if($item.PSObject.Properties.Name -contains 'sha1'){$sha1=(Get-FileHash -Algorithm SHA1 -LiteralPath $archive).Hash.ToUpperInvariant(); if($sha1 -ne $item.sha1){throw "SHA-1 mismatch for archive: $archive"}}
    if($WhatIf){$results += [ordered]@{kind=$kind;status='planned';archive=$archive;sha256=$sha;expected_sha256=$item.sha256}; continue}
    $installRoot=Join-Path $toolRoot $kind
    if(-not (Test-Path -LiteralPath $installRoot)){
        New-Item -ItemType Directory -Path $installRoot | Out-Null
        if($item.url -match '\.jar$') { Copy-Item -LiteralPath $archive -Destination (Join-Path $installRoot (Split-Path $archive -Leaf)) }
        else { Expand-Archive -LiteralPath $archive -DestinationPath $installRoot -Force }
    }
    $results += [ordered]@{kind=$kind;status='installed';archive=$archive;sha256=$sha;install_root=$installRoot;expected_sha256=$item.sha256}
}
Write-JsonReport (Join-Path $root 'reports\install_tools.json') ([ordered]@{layer=$Layer;kind=$Kind;what_if=[bool]$WhatIf;results=$results;note='Archives are downloaded only after the release URL and digest are recorded. Existing archives and install roots are never overwritten.'})
Write-Output (@{layer=$Layer;kind=$Kind;results=$results.Count;what_if=[bool]$WhatIf} | ConvertTo-Json -Compress)
