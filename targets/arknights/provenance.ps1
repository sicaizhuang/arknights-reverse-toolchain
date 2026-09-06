Set-StrictMode -Version Latest

function Get-RecordText {
    param([string]$Path)
    if (-not $Path -or -not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    try { return Get-Content -LiteralPath $Path -Raw -ErrorAction Stop } catch { return $null }
}

function Get-JsonInputEvidence {
    param([string]$Path)
    $raw = Get-RecordText $Path
    if (-not $raw) { return @() }
    try {
        $doc = $raw | ConvertFrom-Json
        $items = @()
        if ($doc.PSObject.Properties.Name -contains 'inputs') { $items += @($doc.inputs) }
        if ($doc.PSObject.Properties.Name -contains 'input') { $items += @($doc.input) }
        foreach ($item in $items) {
            if ($item.apk -and $item.sha256) {
                [ordered]@{ source_record=$Path; source_apk=[string]$item.apk; source_sha256=([string]$item.sha256).ToUpperInvariant(); record_type='json_input' }
            }
        }
    } catch { @() }
}

function Get-ArknightsApkProvenance {
    param(
        [Parameter(Mandatory=$true)][string]$CurrentApk,
        [Parameter(Mandatory=$true)][string]$ExistingDecodedRoot,
        [Parameter(Mandatory=$true)][string]$ExistingJadxOutput,
        [string[]]$SourceManifestPaths = @()
    )

    if (-not (Test-Path -LiteralPath $CurrentApk -PathType Leaf)) { throw "Current APK is missing: $CurrentApk" }
    $currentHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $CurrentApk).Hash.ToUpperInvariant()
    if (-not $SourceManifestPaths -or $SourceManifestPaths.Count -eq 0) {
        $apkRoot = Split-Path -Parent $ExistingDecodedRoot
        $SourceManifestPaths = @(
            (Join-Path $apkRoot 'apk_analysis.json'),
            (Join-Path $apkRoot 'logs\apktool_base.command.json'),
            (Join-Path $apkRoot 'logs\jadx_all_apks.command.json')
        )
    }

    $evidence = @()
    foreach ($path in $SourceManifestPaths) { $evidence += @(Get-JsonInputEvidence $path) }
    $rawEvidence = @{}
    foreach ($path in $SourceManifestPaths) {
        $raw = Get-RecordText $path
        $rawEvidence[$path] = if ($raw) { $raw.Replace('\\','\') } else { $null }
    }

    function Assess-Output {
        param([string]$Kind,[string]$OutputRoot,[string]$ToolToken)
        $outputExists = Test-Path -LiteralPath $OutputRoot -PathType Container
        $linkedRecords = @($rawEvidence.GetEnumerator() | Where-Object {
            $_.Value -and $_.Value -like "*$CurrentApk*" -and $_.Value -like "*$OutputRoot*"
        } | ForEach-Object { $_.Key })
        $matchingHashEvidence = @($evidence | Where-Object { $_.source_apk -eq $CurrentApk -and $_.source_sha256 -eq $currentHash })
        $mismatchingHashEvidence = @($evidence | Where-Object { $_.source_apk -eq $CurrentApk -and $_.source_sha256 -ne $currentHash })
        $matches = @($evidence | Where-Object {
            $_.source_sha256 -eq $currentHash -and
            $_.source_apk -eq $CurrentApk -and
            (($rawEvidence[$_.source_record] -like "*$OutputRoot*") -or ($_.source_record -like '*apk_analysis.json' -and $Kind -eq 'decoded'))
        })
        if ($matches.Count -eq 0 -and $linkedRecords.Count -gt 0 -and $matchingHashEvidence.Count -gt 0) {
            $matches = @($matchingHashEvidence | ForEach-Object {
                [ordered]@{ source_record=$_.source_record; source_apk=$_.source_apk; source_sha256=$_.source_sha256; record_type='linked_command_plus_hash' }
            })
        }
        $mismatch = @($evidence | Where-Object {
            $_.source_apk -eq $CurrentApk -and $_.source_sha256 -ne $currentHash -and
            (($rawEvidence[$_.source_record] -like "*$OutputRoot*") -or ($_.source_record -like '*apk_analysis.json' -and $Kind -eq 'decoded'))
        })
        $status = if (-not $outputExists) { 'blocked_provenance' } elseif ($matches.Count -gt 0) { 'reused_verified' } else { 'blocked_provenance' }
        $reason = if (-not $outputExists) { 'Existing output directory is missing.' } elseif ($matches.Count -gt 0) { 'Source record links this output to the current APK hash.' } elseif ($mismatch.Count -gt 0 -or $mismatchingHashEvidence.Count -gt 0) { 'Source record exists but APK SHA-256 does not match current input.' } else { 'No source record links this output to the current APK.' }
        [ordered]@{
            kind=$Kind
            output_root=$OutputRoot
            output_exists=$outputExists
            status=$status
            reason=$reason
            source_records=@($linkedRecords + @($matches + $mismatch | ForEach-Object { $_.source_record }) | Select-Object -Unique)
            source_apk_paths=@($matches + $mismatch | ForEach-Object { $_.source_apk } | Select-Object -Unique)
            source_hashes=@($matches + $mismatch | ForEach-Object { $_.source_sha256 } | Select-Object -Unique)
            current_apk=$CurrentApk
            current_sha256=$currentHash
            hash_comparisons=@($matches + $mismatch | ForEach-Object { [ordered]@{source_record=$_.source_record;source_apk=$_.source_apk;source_sha256=$_.source_sha256;current_sha256=$currentHash;match=($_.source_sha256 -eq $currentHash)} })
            tool_token=$ToolToken
        }
    }

    $decoded = Assess-Output 'decoded' $ExistingDecodedRoot 'apktool'
    $jadx = Assess-Output 'jadx' $ExistingJadxOutput 'jadx'
    $overall = if ($decoded.status -eq 'reused_verified' -and $jadx.status -eq 'reused_verified') { 'reused_verified' } else { 'blocked_provenance' }
    [ordered]@{
        schema_version=1
        status=$overall
        current_apk=$CurrentApk
        current_sha256=$currentHash
        source_manifest_paths=@($SourceManifestPaths)
        outputs=@($decoded,$jadx)
        source_evidence_count=$evidence.Count
        source_evidence=@($evidence)
        rule='A reused decoded/JADX output is current only when an existing source record links it to the exact current APK SHA-256.'
    }
}
