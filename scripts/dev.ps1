<#
  Run Cloudbase locally for development (Windows).

    powershell -ExecutionPolicy Bypass -File scripts\dev.ps1            # start on http://127.0.0.1:7900
    powershell -ExecutionPolicy Bypass -File scripts\dev.ps1 -Seed      # also add demo apps
    powershell -ExecutionPolicy Bypass -File scripts\dev.ps1 -Reset     # wipe dev data first

  All data lives in .dev\ inside the repo; a real ~/.cloudbase is never touched.
  Login: admin / cloudbase-dev. Frontend changes show after a refresh; backend
  changes restart the server automatically. Docker is optional — without it
  apps cannot actually start, but every page can be viewed.
#>
param(
  [int]$Port = 7900,
  [switch]$Seed,
  [switch]$Reset
)

$ErrorActionPreference = 'Stop'
$Root     = Split-Path -Parent $PSScriptRoot
$Dev      = Join-Path $Root '.dev'
$DevHome  = Join-Path $Dev 'home'
$Venv     = Join-Path $Dev 'venv'
$Py       = Join-Path $Venv 'Scripts\python.exe'
$Req      = Join-Path $Root 'backend\requirements.txt'
$Password = 'cloudbase-dev'

if ($Reset -and (Test-Path $DevHome)) {
  Write-Host '[dev] removing previous dev data'
  Remove-Item -Recurse -Force $DevHome
}
New-Item -ItemType Directory -Force -Path $DevHome | Out-Null

if (-not (Test-Path $Py)) {
  Write-Host '[dev] creating virtual environment in .dev\venv'
  python -m venv $Venv
  if ($LASTEXITCODE -ne 0) { throw 'Could not create a virtual environment. Is Python 3.10+ installed and on PATH?' }
}

# Reinstall dependencies only when requirements.txt changed
$hashFile = Join-Path $Dev 'requirements.sha256'
$hash = (Get-FileHash $Req -Algorithm SHA256).Hash
$installed = if (Test-Path $hashFile) { (Get-Content $hashFile -Raw).Trim() } else { '' }
if ($installed -ne $hash) {
  Write-Host '[dev] installing backend dependencies'
  & $Py -m pip install --disable-pip-version-check -q -r $Req
  if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }
  Set-Content -Path $hashFile -Value $hash -Encoding ascii
}

# Point Cloudbase's ~/.cloudbase at the dev home (Python uses USERPROFILE on Windows)
$env:HOME        = $DevHome
$env:USERPROFILE = $DevHome

$seedScript = Join-Path $PSScriptRoot 'dev_seed.py'
& $Py $seedScript credentials $Password
if ($Seed) { & $Py $seedScript demo }

# A crashed --reload run can leave a worker process holding the port, which
# keeps serving old code. Stop whatever still listens on it.
Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | ForEach-Object {
  $owner = $_.OwningProcess
  Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -eq $owner -or $_.ParentProcessId -eq $owner } | ForEach-Object {
    Write-Host "[dev] stopping leftover process $($_.ProcessId) on port $Port"
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
  }
}

Write-Host ''
Write-Host "  Cloudbase dev server:  http://127.0.0.1:$Port"
Write-Host "  Login:                 admin / $Password"
Write-Host '  Stop with Ctrl+C'
Write-Host ''

Push-Location (Join-Path $Root 'backend')
try {
  & $Py -m uvicorn main:app --host 127.0.0.1 --port $Port --reload --reload-dir . --timeout-graceful-shutdown 3
} finally {
  Pop-Location
}
