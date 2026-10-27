param(
    [string]$GameDirectory,
    [switch]$CheckOnly
)
$ErrorActionPreference = 'Stop'
$expectedGameHash = '48BB9AAAB79950D80A1C440A06C7969214078DEC0E0CFE4F70A284FB6709CC51'
$expectedProxyHash = '2C6B1F588B12B898E36F23BEAAB1239B4CD7A1EAE94808FD0694F5349F2DBBB1'
$expectedProfileHash = '0156C993C9C6D5FCA13EB1E7F96DDC6C3BA48D92224352C1539A3C322B82131B'
$previousProfileHash = '62413A6CDAD4C02725C47ABD04377C2992C52DA8C8BC14B95928C4D0AA77C4E5'
$previousProxyHash = '4C25BAAC258CB6525A1CB567F8A50212C68E468F9B751C6DDE87F8EA52C55AAF'
$gameDirectoryPath = & "$PSScriptRoot\Resolve-FIFA22GameDirectory.ps1" -GameDirectory $GameDirectory
$gameExe = Join-Path $gameDirectoryPath 'FIFA22.exe'
$source = Join-Path $PSScriptRoot 'network-proxy\revival-boot\dinput8.dll'
$configSource = Join-Path $PSScriptRoot 'network-proxy\revival-boot\FUT22-MITM.ini'
$target = Join-Path $gameDirectoryPath 'dinput8.dll'
$configTarget = Join-Path $gameDirectoryPath 'FUT22-MITM.ini'
if (-not (Test-Path -LiteralPath $gameExe -PathType Leaf)) { throw 'FIFA22.exe was not found in the selected game directory.' }
if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw 'The revival startup proxy is missing.' }
if (-not (Test-Path -LiteralPath $configSource -PathType Leaf)) { throw 'The revival startup configuration is missing.' }
if ((Get-FileHash -LiteralPath $gameExe -Algorithm SHA256).Hash -cne $expectedGameHash) {
    throw 'This FIFA 22 executable is a different build. The network hook was not installed.'
}
$sourceHash = (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash
if ($sourceHash -cne $expectedProxyHash) { throw 'The staged revival DLL does not match the audited binary.' }
$configHash = (Get-FileHash -LiteralPath $configSource -Algorithm SHA256).Hash
if ($configHash -cne $expectedProfileHash) { throw 'The staged startup profile has changed from the audited boot-only configuration.' }
$proxyCurrent = $false
if (Test-Path -LiteralPath $target -PathType Leaf) {
    $targetHash = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash
    if ($targetHash -ceq $sourceHash) { $proxyCurrent = $true }
    elseif ($targetHash -cne $previousProxyHash) {
        throw "A different dinput8.dll already exists at $target. No file was replaced."
    }
}
$configCurrent = $false
if (Test-Path -LiteralPath $configTarget -PathType Leaf) {
    $installedConfigHash = (Get-FileHash -LiteralPath $configTarget -Algorithm SHA256).Hash
    $configCurrent = $installedConfigHash -ceq $configHash
    if (-not $configCurrent -and $installedConfigHash -cne $previousProfileHash) {
        throw "An unrecognized FUT22-MITM.ini already exists at $configTarget. No file was replaced."
    }
}
if ($proxyCurrent -and $configCurrent) { Write-Host 'Revival startup connector and profile are installed for this FIFA 22 build.'; return }
if ($CheckOnly) { throw 'The revival startup connector/profile is not installed.' }
if (Get-Process FIFA22 -ErrorAction SilentlyContinue) { throw 'Close FIFA 22 before installing the revival startup connector.' }
if (-not $configCurrent) { Copy-Item -LiteralPath $configSource -Destination $configTarget -ErrorAction Stop }
if (-not $proxyCurrent) { Copy-Item -LiteralPath $source -Destination $target -ErrorAction Stop }
if ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash -cne $sourceHash -or
    (Get-FileHash -LiteralPath $configTarget -Algorithm SHA256).Hash -cne $configHash) {
    throw 'Revival startup connector installation did not verify.'
}
Write-Host "Revival startup connector and profile installed at $gameDirectoryPath."
