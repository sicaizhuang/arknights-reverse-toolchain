Set-StrictMode -Version Latest

function Test-ArknightsAnimeStudioBaseline {
    param(
        [Parameter(Mandatory=$true)][string]$ResolvedInput,
        [Parameter(Mandatory=$true)][string]$InputSha256,
        [Parameter(Mandatory=$true)][object[]]$ActualFiles,
        [Parameter(Mandatory=$true)]$VerifiedSample
    )

    $expectedFiles = @($VerifiedSample.expected_outputs)
    $actualByPath = @{}
    foreach ($file in $ActualFiles) {
        $relative = ([string]$file.path).Replace('\','/')
        $actualByPath[$relative] = $file
    }
    $expectedByPath = @{}
    foreach ($file in $expectedFiles) {
        $relative = ([string]$file.path).Replace('\','/')
        $expectedByPath[$relative] = $file
    }

    $missing = @($expectedByPath.Keys | Where-Object { -not $actualByPath.ContainsKey($_) } | Sort-Object)
    $unexpected = @($actualByPath.Keys | Where-Object { -not $expectedByPath.ContainsKey($_) } | Sort-Object)
    $hashMismatches = @()
    foreach ($relative in @($expectedByPath.Keys | Where-Object { $actualByPath.ContainsKey($_) } | Sort-Object)) {
        $expectedHash = ([string]$expectedByPath[$relative].sha256).ToUpperInvariant()
        $actualHash = ([string]$actualByPath[$relative].sha256).ToUpperInvariant()
        if ($expectedHash -ne $actualHash) {
            $hashMismatches += [ordered]@{ path=$relative; expected=$expectedHash; actual=$actualHash }
        }
    }

    $samplePathMatch = $ResolvedInput.Equals([string]$VerifiedSample.bundle_path,[StringComparison]::OrdinalIgnoreCase)
    $sampleHashMatch = $InputSha256.ToUpperInvariant() -eq ([string]$VerifiedSample.sha256).ToUpperInvariant()
    $countMatch = $ActualFiles.Count -eq [int]$VerifiedSample.expected_export_files -and $ActualFiles.Count -eq $expectedFiles.Count
    $pathsMatch = $missing.Count -eq 0 -and $unexpected.Count -eq 0
    $hashesMatch = $hashMismatches.Count -eq 0 -and $pathsMatch
    $passed = $samplePathMatch -and $sampleHashMatch -and $countMatch -and $pathsMatch -and $hashesMatch

    return [pscustomobject][ordered]@{
        passed = $passed
        sample_path_match = $samplePathMatch
        sample_hash_match = $sampleHashMatch
        expected_output_count_match = $countMatch
        expected_relative_paths_match = $pathsMatch
        expected_output_hashes_match = $hashesMatch
        expected_count = [int]$VerifiedSample.expected_export_files
        actual_count = $ActualFiles.Count
        missing_paths = $missing
        unexpected_paths = $unexpected
        hash_mismatches = $hashMismatches
    }
}
