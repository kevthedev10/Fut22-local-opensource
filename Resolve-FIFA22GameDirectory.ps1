param([string]$GameDirectory, [string[]]$AdditionalSearchRoots = @())
$ErrorActionPreference = 'Stop'

function Resolve-GameFolder([string]$Folder) {
    if ([string]::IsNullOrWhiteSpace($Folder)) { return $null }
    $Folder = [Environment]::ExpandEnvironmentVariables($Folder.Trim().Trim('"'))
    if (-not (Test-Path -LiteralPath $Folder -PathType Container)) { return $null }
    $resolved = (Resolve-Path -LiteralPath $Folder).Path
    if (-not (Test-Path -LiteralPath (Join-Path $resolved 'FIFA22.exe') -PathType Leaf)) { return $null }
    return $resolved
}

if ($GameDirectory) {
    $found = Resolve-GameFolder $GameDirectory
    if (-not $found) { throw "FIFA22.exe was not found in the selected game directory: $GameDirectory" }
    return $found
}

if ($env:FIFA22_GAME_DIRECTORY) {
    $found = Resolve-GameFolder $env:FIFA22_GAME_DIRECTORY
    if ($found) { return $found }
}

foreach ($root in $AdditionalSearchRoots) {
    foreach ($relative in @('EA Games\FIFA 22', 'Origin Games\FIFA 22')) {
        $found = Resolve-GameFolder (Join-Path $root $relative)
        if ($found) { return $found }
    }
}

foreach ($process in @(Get-Process FIFA22 -ErrorAction SilentlyContinue)) {
    try {
        if ($process.Path) {
            $found = Resolve-GameFolder (Split-Path -Parent $process.Path)
            if ($found) { return $found }
        }
    } catch { }
}

# EA app and Origin installs expose their game folder through these registry
# locations. InstallLocation also handles custom EA library folders on any drive.
$eaCandidates = @()
foreach ($key in @('HKLM:\SOFTWARE\EA Games\FIFA 22',
                   'HKLM:\SOFTWARE\WOW6432Node\EA Games\FIFA 22',
                   'HKLM:\SOFTWARE\Electronic Arts\FIFA 22',
                   'HKLM:\SOFTWARE\WOW6432Node\Electronic Arts\FIFA 22',
                   'HKCU:\SOFTWARE\Electronic Arts\FIFA 22')) {
    $settings = Get-ItemProperty -LiteralPath $key -ErrorAction SilentlyContinue
    if (-not $settings) { continue }
    foreach ($name in @('Install Dir','InstallDir','InstallLocation','Path','GameInstallDir')) {
        $value = $settings.PSObject.Properties[$name].Value
        if ($value) { $eaCandidates += [string]$value }
    }
}
foreach ($root in @('HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall',
                    'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall',
                    'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall')) {
    foreach ($entry in @(Get-ChildItem -LiteralPath $root -ErrorAction SilentlyContinue)) {
        $settings = Get-ItemProperty -LiteralPath $entry.PSPath -ErrorAction SilentlyContinue
        if ($settings.DisplayName -notmatch '^(EA SPORTS\s+)?FIFA\s*22\b') { continue }
        if ($settings.InstallLocation -and
            [string]$settings.InstallLocation -notmatch '[\\/]steamapps[\\/]common[\\/]') {
            $eaCandidates += [string]$settings.InstallLocation
        }
        if ($settings.DisplayIcon) {
            $icon = [string]$settings.DisplayIcon
            if ($icon -match '^\s*"([^"]*FIFA22\.exe)"' -or
                $icon -match '^\s*(.+?FIFA22\.exe)(?:,\d+)?\s*$') {
                if ($Matches[1] -notmatch '[\\/]steamapps[\\/]common[\\/]') {
                    $eaCandidates += Split-Path -Parent $Matches[1]
                }
            }
        }
    }
}
foreach ($drive in (Get-PSDrive -PSProvider FileSystem)) {
    foreach ($relative in @('EA Games\FIFA 22',
                           'Program Files\EA Games\FIFA 22',
                           'Program Files (x86)\EA Games\FIFA 22',
                           'Origin Games\FIFA 22',
                           'Program Files\Origin Games\FIFA 22',
                           'Program Files (x86)\Origin Games\FIFA 22')) {
        $eaCandidates += Join-Path $drive.Root $relative
    }
}
foreach ($candidate in ($eaCandidates | Select-Object -Unique)) {
    $found = Resolve-GameFolder $candidate
    if ($found) { return $found }
}

$steamRoots = @()
foreach ($key in @('HKCU:\Software\Valve\Steam', 'HKLM:\SOFTWARE\WOW6432Node\Valve\Steam')) {
    $settings = Get-ItemProperty -LiteralPath $key -ErrorAction SilentlyContinue
    if ($settings.SteamPath) { $steamRoots += $settings.SteamPath }
    if ($settings.InstallPath) { $steamRoots += $settings.InstallPath }
}
foreach ($drive in (Get-PSDrive -PSProvider FileSystem)) {
    $steamRoots += Join-Path $drive.Root 'Program Files (x86)\Steam'
    $steamRoots += Join-Path $drive.Root 'Program Files\Steam'
    $steamRoots += Join-Path $drive.Root 'Steam'
    $steamRoots += Join-Path $drive.Root 'SteamLibrary'
}

foreach ($steamRoot in ($steamRoots | Select-Object -Unique)) {
    if (-not (Test-Path -LiteralPath $steamRoot -PathType Container)) { continue }
    $libraries = @($steamRoot)
    $libraryConfig = Join-Path $steamRoot 'steamapps\libraryfolders.vdf'
    if (Test-Path -LiteralPath $libraryConfig -PathType Leaf) {
        foreach ($line in (Get-Content -LiteralPath $libraryConfig)) {
            if ($line -match '^\s*"path"\s+"([^"]+)"') {
                $libraries += $Matches[1].Replace('\\', '\')
            }
        }
    }
    foreach ($library in ($libraries | Select-Object -Unique)) {
        if (-not (Test-Path -LiteralPath $library)) {
    continue
 }

 $manifest = Join-Path $library 'steamapps\appmanifest_1506830.acf'
        if (-not (Test-Path -LiteralPath $manifest -PathType Leaf)) { continue }
        $installDir = 'FIFA 22'
        foreach ($line in (Get-Content -LiteralPath $manifest)) {
            if ($line -match '^\s*"installdir"\s+"([^"]+)"') {
                $installDir = $Matches[1]
                break
            }
        }
        $found = Resolve-GameFolder (Join-Path (Join-Path $library 'steamapps\common') $installDir)
        if ($found) { return $found }
    }
}

throw 'FIFA 22 was not found in Steam or EA app locations. Run Play-Local-FUT.ps1 -GameDirectory "C:\path\to\FIFA 22" for a custom library folder.'
