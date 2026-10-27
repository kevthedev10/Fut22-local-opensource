param([switch]$WaitForExternalGame, [switch]$CheckOnly,
      [string]$GameDirectory)
$ErrorActionPreference = 'Stop'
$pythonPath = & "$PSScriptRoot\Get-LocalFUTPython.ps1"
$expectedServerHash = (Get-FileHash -LiteralPath (Join-Path $PSScriptRoot 'localfut22\server.py') -Algorithm SHA256).Hash
$ownedServer = $null
$serverErrorLog = Join-Path $PSScriptRoot 'runtime\launcher\server-error.log'

function Test-LocalFUTService {
    try {
        $status = Invoke-RestMethod -Uri 'http://127.0.0.1:18222/localfut22/status' -TimeoutSec 2
    } catch { return $false }
    if ($status.stage -eq 'club-bring-up' -and $status.futMenuVerified -eq $true `
            -and $status.redirectorTls -eq $false) {
        if ($status.serverSourceSha256 -ne $expectedServerHash) {
            throw 'An older Local FUT service is still running. Close its command window, then launch again. A PC restart is not needed.'
        }
        return $true
    }
    return $false
}

function Get-ReadyGame {
    $games = @(Get-Process FIFA22 -ErrorAction SilentlyContinue)
    if ($games.Count -ne 1) { return $null }
    try {
        if (-not $games[0].Path) { return $null }
    } catch { return $null }
    return $games[0]
}

function Test-SteamGameDirectory([string]$Directory) {
    $common = Split-Path -Parent $Directory
    if ((Split-Path -Leaf $common) -ne 'common') { return $false }
    $steamApps = Split-Path -Parent $common
    if ((Split-Path -Leaf $steamApps) -ne 'steamapps') { return $false }
    return Test-Path -LiteralPath (Join-Path $steamApps 'appmanifest_1506830.acf') -PathType Leaf
}

function Get-EAAppExecutable {
    $candidates = @()
    foreach ($key in @('HKLM:\SOFTWARE\Electronic Arts\EA Desktop',
                       'HKLM:\SOFTWARE\WOW6432Node\Electronic Arts\EA Desktop')) {
        $settings = Get-ItemProperty -LiteralPath $key -ErrorAction SilentlyContinue
        if ($settings.DesktopAppPath) { $candidates += $settings.DesktopAppPath }
        if ($settings.ClientPath) { $candidates += $settings.ClientPath }
    }
    foreach ($drive in (Get-PSDrive -PSProvider FileSystem)) {
        $candidates += Join-Path $drive.Root 'Program Files\Electronic Arts\EA Desktop\EA Desktop\EADesktop.exe'
    }
    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    }
    return $null
}

Push-Location $PSScriptRoot
try {
    $env:PYTHONPATH = $PSScriptRoot
    $gameDirectoryPath = & "$PSScriptRoot\Resolve-FIFA22GameDirectory.ps1" -GameDirectory $GameDirectory
    $env:FIFA22_GAME_DIRECTORY = $gameDirectoryPath
    & "$PSScriptRoot\Install-Network-Proxy.ps1" -GameDirectory $gameDirectoryPath -CheckOnly:$CheckOnly
    if (Test-LocalFUTService) {
        Write-Host 'Local FUT service is already running.'
    } else {
        $logDir = Join-Path $PSScriptRoot 'runtime\launcher'
        New-Item -ItemType Directory -Path $logDir -Force | Out-Null
        $serverOutputLog = Join-Path $logDir 'server-output.log'
        $serverArgs = @('-u','-m','localfut22.server','--runtime','runtime/live-fut-massinfo',
                        '--http-port','18222','--blaze-port','18221',
                        '--redirector-port','42230','--qos-port','17502','--preauth-qos')
        Write-Host 'Starting local FUT service...'
        $ownedServer = Start-Process -FilePath $pythonPath -ArgumentList $serverArgs `
            -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput $serverOutputLog -RedirectStandardError $serverErrorLog
        $serviceReady = $false
        for ($attempt = 0; $attempt -lt 60; $attempt++) {
            if ($ownedServer.HasExited) { break }
            if (Test-LocalFUTService) { $serviceReady = $true; break }
            Start-Sleep -Seconds 1
        }
        if (-not $serviceReady) {
            if (Test-Path -LiteralPath $serverErrorLog) {
                Get-Content -LiteralPath $serverErrorLog -Tail 12 | Write-Host
            }
            throw 'Local FUT service did not start. Check runtime\launcher\server-error.log.'
        }
        Write-Host 'Local FUT service is ready.'
    }

    if ($CheckOnly) {
        Write-Host 'Launcher service check passed.'
        return
    }

    $clubStatus = Invoke-RestMethod -Uri 'http://127.0.0.1:18222/localfut22/status' -TimeoutSec 5
    if (-not $clubStatus.club.clubNamed) {
        Write-Host 'FIFA 22 should ask you to name your FUT club when Ultimate Team opens.'
    }

    $game = Get-ReadyGame
    if (-not $game) {
        if ($WaitForExternalGame) {
            Write-Host 'Launch FIFA 22 through Steam or the EA app now.'
        } elseif (Test-SteamGameDirectory $gameDirectoryPath) {
            Write-Host 'Opening FIFA 22 through Steam...'
            Start-Process -FilePath 'steam://rungameid/1506830'
        } else {
            $eaApp = Get-EAAppExecutable
            if ($eaApp) {
                Write-Host 'Opening the EA app. Start FIFA 22 from your Library.'
                Start-Process -FilePath $eaApp
            } else {
                Write-Host 'Start FIFA 22 through the EA app now.'
            }
        }
        $deadline = (Get-Date).AddMinutes(10)
        while ((Get-Date) -lt $deadline) {
            $game = Get-ReadyGame
            if ($game) { break }
            Start-Sleep -Seconds 1
        }
        if (-not $game) { throw 'FIFA 22 did not start within 10 minutes.' }
    } else {
        Write-Host 'FIFA 22 is already running.'
    }

    Write-Host 'FIFA 22 opened. The revival startup connector directs it to local FUT.'
    Write-Host 'Open Ultimate Team when the game menu appears.'
    while (Get-Process -Id $game.Id -ErrorAction SilentlyContinue) {
        Start-Sleep -Seconds 2
    }
    Write-Host 'FIFA 22 closed.'
} finally {
    if ($ownedServer -and -not $ownedServer.HasExited) {
        Stop-Process -Id $ownedServer.Id -Force -ErrorAction SilentlyContinue
        Write-Host 'Local FUT service stopped.'
    }
    Pop-Location
}
