#!/usr/bin/env pwsh
<#
.SYNOPSIS
Proves the published version scheme against Scoop's own version comparator.

.DESCRIPTION
Scoop decides whether a newer version exists with Compare-Version from
lib/versions.ps1, so the scheme in the published manifests has to order correctly in
Scoop's hands, not just in ours. The builder publishes upstream's `X.Y.Z` for the
first build of a release and `X.Y.Z-r<N>` for a hand-dispatched rebuild of that same
release, which is exactly what this script asserts.

Scoop's comparator is loaded from a pinned commit so the assertions stay
reproducible. The rejection cases at the end record what the scheme deliberately
avoids, so nobody has to re-derive it.

.EXAMPLE
pwsh tests/version-order.ps1
pwsh tests/version-order.ps1 -ScoopVersionsPath ./versions.ps1
#>
[CmdletBinding()]
param(
    [string]$ScoopVersionsUrl = 'https://raw.githubusercontent.com/ScoopInstaller/Scoop/145381fc0db8134376f98f4234376023224305db/lib/versions.ps1',
    [string]$ScoopVersionsPath
)

$ErrorActionPreference = 'Stop'
$env:DOTNET_SYSTEM_GLOBALIZATION_INVARIANT = '1'

$failures = [System.Collections.Generic.List[string]]::new()

function Assert-Order {
    param([string]$Reference, [string]$Difference, [int]$Expected, [string]$Why)
    $actual = Compare-Version -ReferenceVersion $Reference -DifferenceVersion $Difference
    $label = '{0,-22} -> {1,-14} expected {2,2} got {3,2}' -f $Reference, $Difference, $Expected, $actual
    if ($actual -eq $Expected) {
        Write-Host "PASS  $label  ($Why)"
    }
    else {
        Write-Host "FAIL  $label  ($Why)"
        $failures.Add($label)
    }
}

function Assert-NoOrder {
    param([string]$Reference, [string]$Difference, [string]$Why)
    $actual = Compare-Version -ReferenceVersion $Reference -DifferenceVersion $Difference
    $label = '{0,-22} -> {1,-14} expected no order     got {2}' -f $Reference, $Difference, $actual
    if ($null -eq $actual -or $actual -eq '') {
        Write-Host "PASS  $label  ($Why)"
    }
    else {
        Write-Host "FAIL  $label  ($Why)"
        $failures.Add($label)
    }
}

$temporary = $null
try {
    if (-not $ScoopVersionsPath) {
        $temporary = Join-Path ([System.IO.Path]::GetTempPath()) "scoop-versions-$([guid]::NewGuid()).ps1"
        Write-Host 'fetching Scoop version comparator from the pinned commit'
        Invoke-WebRequest -Uri $ScoopVersionsUrl -OutFile $temporary -UseBasicParsing
        $ScoopVersionsPath = $temporary
    }
    if (-not (Test-Path -LiteralPath $ScoopVersionsPath)) {
        throw "Scoop version comparator not found: $ScoopVersionsPath"
    }

    # Compare-Version only reaches Scoop's config on its nightly-version branch.
    if (-not (Get-Command -Name get_config -ErrorAction SilentlyContinue)) {
        function global:get_config { param($name) return $false }
    }
    . $ScoopVersionsPath
    if (-not (Get-Command -Name Compare-Version -ErrorAction SilentlyContinue)) {
        throw 'the supplied file does not define Compare-Version'
    }

    Write-Host ''
    Write-Host 'Published scheme: 0.23.3 for the first build, 0.23.3-r<N> for a rebuild of it'
    $chain = @(
        @{ From = '0.23.3';     To = '0.23.3-r2';  Why = 'a rebuild of the same release must be offered' }
        @{ From = '0.23.3-r2';  To = '0.23.3-r11'; Why = 'revisions stay numeric at double digits' }
        @{ From = '0.23.3-r11'; To = '0.23.4';     Why = 'the next release outranks any earlier rebuild' }
        @{ From = '0.23.4';     To = '0.23.4-r1';  Why = 'a rebuild of the next release is offered too' }
        @{ From = '0.23.4-r1';  To = '0.23.10';    Why = 'a later release outranks it in turn' }
    )
    foreach ($step in $chain) {
        Assert-Order -Reference $step.From -Difference $step.To -Expected 1 -Why $step.Why
    }
    Assert-Order -Reference '0.23.3' -Difference '0.23.3' -Expected 0 `
        -Why 'a build never outranks itself'

    Write-Host ''
    Write-Host 'Adoption path: an existing bucket manifest of this app upgrades forward'
    Assert-Order -Reference '2026.6864.2132.0-r1' -Difference '0.23.3' -Expected -1 `
        -Why 'a legacy 4-part product version is older than the plain release version'
    Assert-Order -Reference '2026.6864.2132.0-r1' -Difference '0.23.3-r2' -Expected -1 `
        -Why 'and older than a rebuild of it'

    Write-Host ''
    Write-Host 'Rejected: zero-padded revisions, because they have no defined order'
    Assert-NoOrder -Reference '0.23.3-r01' -Difference '0.23.3-r1' `
        -Why 'never pad a revision, even to keep digits aligned'

    Write-Host ''
    if ($failures.Count -gt 0) {
        Write-Host "$($failures.Count) version ordering invariant(s) violated:"
        $failures | ForEach-Object { Write-Host "  $_" }
        exit 1
    }
    Write-Host 'all version ordering invariants hold'
}
finally {
    if ($temporary -and (Test-Path -LiteralPath $temporary)) {
        Remove-Item -LiteralPath $temporary -Force
    }
}
