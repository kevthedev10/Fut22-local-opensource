$ErrorActionPreference = 'Stop'
$candidates = @()
if ($env:LOCALFUT_PYTHON) { $candidates += $env:LOCALFUT_PYTHON }

$archive = Join-Path $PSScriptRoot 'vendor\python-3.12-win_amd64.zip'
if (Test-Path -LiteralPath $archive -PathType Leaf) {
    $digest = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.Substring(0,16)
    $runtimeRoot = if ($env:LOCALFUT_RUNTIME_ROOT) {
        $env:LOCALFUT_RUNTIME_ROOT
    } else {
        Join-Path $env:LOCALAPPDATA 'LocalFUT22'
    }
    $runtimeDir = Join-Path $runtimeRoot "python-$digest"
    $bundled = Join-Path $runtimeDir 'python.exe'
    if (-not (Test-Path -LiteralPath $bundled -PathType Leaf)) {
        New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null
        Expand-Archive -LiteralPath $archive -DestinationPath $runtimeDir -Force
    }
    $candidates += $bundled
}

$onPath = Get-Command python.exe -CommandType Application -ErrorAction SilentlyContinue
if ($onPath) { $candidates += $onPath.Source }

foreach ($candidate in ($candidates | Select-Object -Unique)) {
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
    & $candidate -c 'import sys, sqlite3, cryptography; assert sys.version_info >= (3, 12)' 2>$null
    if ($LASTEXITCODE -eq 0) { return $candidate }
}

throw 'The bundled Python runtime is missing or damaged. Recopy the vendor folder, or set LOCALFUT_PYTHON to a Python 3.12+ python.exe with cryptography.'
