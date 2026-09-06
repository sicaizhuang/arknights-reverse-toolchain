Set-StrictMode -Version Latest

function Get-AnimeStudioLogSummary {
    param(
        [string]$Stdout = '',
        [string]$Stderr = ''
    )

    $combined = @($Stdout, $Stderr) -join "`n"
    $exportMatches = [regex]::Matches($combined, 'Finished exporting\s+(\d+)\s+assets', 'IgnoreCase')
    $skippedMatches = [regex]::Matches($combined, '(\d+)\s+assets skipped', 'IgnoreCase')
    $errorLines = @($combined -split "`r?`n" | Where-Object {
        $_ -match '^\s*\[Error\]' -or
        $_ -match '(?i)\b(unhandled exception|notimplementedexception|failed to (?:load|read|parse|export)|has invalid format)\b'
    })
    $reportedExported = 0
    foreach ($match in $exportMatches) { $reportedExported += [int]$match.Groups[1].Value }
    $reportedSkipped = 0
    foreach ($match in $skippedMatches) { $reportedSkipped += [int]$match.Groups[1].Value }

    return [pscustomobject][ordered]@{
        scan_started = $combined -match 'Scanning for files'
        input_file_count_reported = if ($combined -match 'Found\s+(\d+)\s+files') { [int]$Matches[1] } else { $null }
        finished_export_log_present = $exportMatches.Count -gt 0
        nothing_exported_log_present = $combined -match 'Nothing exported\.'
        reported_exported_assets = $reportedExported
        reported_skipped_assets = $reportedSkipped
        invalid_type_log_present = $combined -match 'has invalid format, skipping'
        error_line_count = $errorLines.Count
        error_lines = $errorLines
    }
}

function Test-AnimeStudioOutputFile {
    param([Parameter(Mandatory=$true)][string]$Path)

    $file = Get-Item -LiteralPath $Path
    $extension = $file.Extension.ToLowerInvariant()
    $validation = 'nonempty'
    $valid = $file.Length -gt 0
    $error = $null
    try {
        switch ($extension) {
            '.png' {
                Add-Type -AssemblyName System.Drawing
                $image = [Drawing.Image]::FromFile($file.FullName)
                try {
                    $valid = $image.Width -gt 0 -and $image.Height -gt 0
                    $validation = 'image_decode'
                } finally { $image.Dispose() }
            }
            '.json' {
                [void](Get-Content -LiteralPath $file.FullName -Raw -Encoding UTF8 | ConvertFrom-Json)
                $validation = 'json_parse'
            }
            '.wav' {
                $stream = [IO.File]::OpenRead($file.FullName)
                try {
                    $head = New-Object byte[] 12
                    $read = $stream.Read($head,0,$head.Length)
                    $valid = $read -eq 12 -and [Text.Encoding]::ASCII.GetString($head,0,4) -eq 'RIFF' -and [Text.Encoding]::ASCII.GetString($head,8,4) -eq 'WAVE'
                    $validation = 'wav_header'
                } finally { $stream.Dispose() }
            }
            '.obj' {
                $text = Get-Content -LiteralPath $file.FullName -TotalCount 64 -Encoding UTF8
                $valid = @($text | Where-Object { $_ -match '^(?:v|g|f)\s' }).Count -gt 0
                $validation = 'obj_structure'
            }
        }
    } catch {
        $valid = $false
        $error = $_.Exception.Message
    }
    return [pscustomobject][ordered]@{ valid=$valid; validation=$validation; error=$error }
}

function Get-AnimeStudioExportFileRecords {
    param(
        [Parameter(Mandatory=$true)][string]$ExportRoot,
        [Parameter(Mandatory=$true)][string]$SourceBundle,
        [string[]]$RequestedTypes = @(),
        [string]$GroupAssets = 'ByType',
        [string]$BundleRoot = ''
    )

    if (-not (Test-Path -LiteralPath $ExportRoot -PathType Container)) { return @() }
    $records = @()
    foreach ($file in Get-ChildItem -LiteralPath $ExportRoot -Recurse -File | Sort-Object FullName) {
        $relative = $file.FullName.Substring($ExportRoot.TrimEnd('\').Length + 1).Replace('\','/')
        $segments = $relative -split '/'
        $type = 'unclassified'
        if ($GroupAssets -eq 'ByType' -and $segments.Count -gt 1) {
            $type = $segments[0]
        } elseif ($RequestedTypes.Count -eq 1) {
            $type = [string]$RequestedTypes[0]
        } else {
            $type = switch ($file.Extension.ToLowerInvariant()) {
                '.anim' { 'AnimationClip' }
                '.obj' { 'Mesh' }
                '.fbx' { 'Animator_or_GameObject' }
                '.wav' { 'AudioClip' }
                '.shader' { 'Shader' }
                '.ttf' { 'Font' }
                '.otf' { 'Font' }
                default { 'ambiguous_requested_type' }
            }
        }
        $attributedSource = $SourceBundle
        $sourceAttribution = 'requested_input'
        if ($GroupAssets -eq 'BySource' -and $segments.Count -gt 1) {
            $sourceLeaf = $segments[0] -replace '_export$',''
            if ($BundleRoot -and (Test-Path -LiteralPath $BundleRoot -PathType Container)) {
                $matches = @(Get-ChildItem -LiteralPath $BundleRoot -Recurse -File -Filter $sourceLeaf -ErrorAction SilentlyContinue)
                if ($matches.Count -eq 1) {
                    $attributedSource = $matches[0].FullName
                    $sourceAttribution = 'by_source_directory_unique_bundle_leaf'
                } elseif ($matches.Count -gt 1) {
                    $sourceAttribution = 'ambiguous_bundle_leaf_fallback_requested_input'
                }
            }
        }
        $check = Test-AnimeStudioOutputFile -Path $file.FullName
        $records += [pscustomobject][ordered]@{
            source_bundle = $attributedSource
            source_attribution = $sourceAttribution
            unity_type = $type
            relative_path = $relative
            output_path = $file.FullName
            size = $file.Length
            sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $file.FullName).Hash
            validation = $check.validation
            valid = $check.valid
            validation_error = $check.error
        }
    }
    return $records
}
