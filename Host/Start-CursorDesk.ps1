# CursorDesk — single launcher for install, start, stop, and restart.
#
# Usage:
#   Start-CursorDesk.bat                 Start host (foreground; Ctrl+C stops)
#   Start-CursorDesk.bat -InstallTailscale   Install / update Tailscale
#   Start-CursorDesk.bat -InstallDeps        pip install Python requirements
#   Start-CursorDesk.bat -Relaunch           Restart Cursor with CDP :9222
#   Start-CursorDesk.bat -Restart            Restart host in background
#   Start-CursorDesk.bat -Stop               Stop host only (Cursor stays open)
param(
  [switch]$Relaunch,
  [switch]$Stop,
  [switch]$Restart,
  [switch]$InstallTailscale,
  [switch]$InstallDeps,
  [int]$Port = 8765
)

$ErrorActionPreference = "Continue"
$Host.UI.RawUI.WindowTitle = "CursorDesk"
Set-Location $PSScriptRoot

function Find-Tailscale {
  $cmd = Get-Command tailscale -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  $p = Join-Path $env:ProgramFiles "Tailscale\tailscale.exe"
  if (Test-Path $p) { return $p }
  return $null
}

function Find-Python {
  $candidates = @(
    "C:\Program Files\Python312\python.exe",
    "C:\Program Files\Python311\python.exe",
    "C:\Program Files\Python310\python.exe",
    "C:\Program Files\Python39\python.exe"
  )
  foreach ($pyExe in $candidates) {
    if (Test-Path $pyExe) { return $pyExe }
  }
  $cmd = Get-Command python -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  throw "Python not found. Install Python 3.9+ and rerun with -InstallDeps."
}

function Install-TailscalePackage {
  Write-Host "Tailscale connects your phone and PC on a private network." -ForegroundColor Cyan
  Write-Host ""
  $ts = Find-Tailscale
  if (-not $ts) {
    winget install --id Tailscale.Tailscale -e --accept-package-agreements --accept-source-agreements
  } else {
    Write-Host "Tailscale already installed — checking for updates..." -ForegroundColor DarkGray
    winget upgrade --id Tailscale.Tailscale -e --accept-package-agreements --accept-source-agreements 2>$null
  }
  Write-Host ""
  Write-Host "Next:" -ForegroundColor Cyan
  Write-Host "  1. Open Tailscale on this PC and sign in"
  Write-Host "  2. Install Tailscale on your phone (same account)"
  Write-Host "  3. Run Start-CursorDesk.bat"
  Write-Host "  4. On your phone open http://<tailscale-ip>:8765"
  $app = Join-Path $env:ProgramFiles "Tailscale\Tailscale.exe"
  if (Test-Path $app) { Start-Process $app }
}

function Install-PythonDeps {
  $pyExe = Find-Python
  $req = Join-Path $PSScriptRoot "requirements.txt"
  Write-Host "Installing Python dependencies..." -ForegroundColor Cyan
  & $pyExe -m pip install --upgrade pip
  & $pyExe -m pip install -r $req
  if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
  Write-Host "Dependencies installed." -ForegroundColor Green
}

function Test-Cdp {
  try {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:9222/json" -UseBasicParsing -TimeoutSec 2
    return $r.StatusCode -eq 200
  } catch {
    return $false
  }
}

function Get-TailscaleIps {
  param($TsExe)
  if (-not $TsExe) { return @() }
  try {
    return @(
      & $TsExe ip -4 2>$null |
        ForEach-Object { $_.ToString().Trim() } |
        Where-Object { $_ -match '^\d+\.' }
    )
  } catch {
    return @()
  }
}

function Show-PhoneUrls {
  param([string[]]$TsIps, [string]$Backend, [int]$Port)
  Write-Host ""
  Write-Host "############################################################" -ForegroundColor Green
  Write-Host "#  PHONE URL (Tailscale) - open this on your phone         #" -ForegroundColor Green
  Write-Host "############################################################" -ForegroundColor Green
  if ($TsIps.Count -gt 0) {
    foreach ($ip in $TsIps) {
      Write-Host ""
      Write-Host ("    http://{0}:{1}" -f $ip, $Port) -ForegroundColor Green
    }
    Write-Host ""
    Write-Host "  Phone + PC both on Tailscale (same account)." -ForegroundColor Cyan
    Write-Host "  Same IP every time." -ForegroundColor Cyan
    $primary = "http://{0}:{1}" -f $TsIps[0], $Port
    $urlFile = Join-Path $env:LOCALAPPDATA "CursorDesk\tailscale_url.txt"
    New-Item -ItemType Directory -Force -Path (Split-Path $urlFile) | Out-Null
    Set-Content -Path $urlFile -Value $primary -Encoding UTF8
    try { Set-Clipboard -Value $primary } catch {}
    Write-Host ("  Copied to clipboard + saved: {0}" -f $urlFile) -ForegroundColor DarkGray
  } else {
    Write-Host ""
    Write-Host ("    Tailscale IP not ready (state: {0})" -f $Backend) -ForegroundColor Yellow
    Write-Host ("    Sign in on this PC, then rerun. Phone uses Tailscale IP:{0}" -f $Port) -ForegroundColor Yellow
  }
  Write-Host "############################################################" -ForegroundColor Green
  Write-Host ""
}

function Ensure-CursorCdp {
  param([switch]$Relaunch)

  Write-Host "Checking Cursor CDP :9222 ..." -ForegroundColor Cyan
  if (Test-Cdp) {
    Write-Host "CDP already enabled - leaving Cursor alone." -ForegroundColor Green
    return $true
  }

  $exe = Join-Path $env:LOCALAPPDATA "Programs\cursor\Cursor.exe"
  if (-not (Test-Path $exe)) {
    Write-Host ("Cursor.exe not found at {0}" -f $exe) -ForegroundColor Red
    return $false
  }

  $running = @(Get-Process -Name "Cursor" -ErrorAction SilentlyContinue)

  if ($running.Count -gt 0 -and -not $Relaunch) {
    Write-Host "Cursor is open without CDP. Not touching it." -ForegroundColor Yellow
    Write-Host "Desktop tab works now. For the Agent tab: Start-CursorDesk.bat -Relaunch" -ForegroundColor Cyan
    return $false
  }

  if ($running.Count -gt 0 -and $Relaunch) {
    Write-Host "Relaunch requested - closing Cursor, restarting with CDP..." -ForegroundColor Yellow
    $running | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
  }

  $launchArgs = @("--remote-debugging-port=9222")
  $project = $env:CURSORDESK_PROJECT
  if ($project -and (Test-Path $project)) { $launchArgs += $project }

  Write-Host "Starting Cursor with CDP..." -ForegroundColor Cyan
  Start-Process -FilePath $exe -ArgumentList $launchArgs

  for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Milliseconds 500
    if (Test-Cdp) {
      Write-Host "CDP ready." -ForegroundColor Green
      return $true
    }
  }

  Write-Host "Cursor started but :9222 is not up. Quit Cursor fully (tray too), then rerun." -ForegroundColor Yellow
  return $false
}

function Stop-CursorDeskHost {
  param([int]$Port)
  & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "Stop-Host.ps1") -Port $Port
  return $LASTEXITCODE
}

function Start-CursorDeskHostBackground {
  param([int]$Port)
  $env:CURSORDESK_RELOAD = "0"
  $env:CURSORDESK_PORT = "$Port"
  $pyExe = Find-Python
  Start-Process -WindowStyle Minimized -FilePath $pyExe `
    -ArgumentList (Join-Path $PSScriptRoot "cursor_window_stream.py") `
    -WorkingDirectory $PSScriptRoot
}

if ($InstallTailscale) {
  Install-TailscalePackage
  exit 0
}

if ($InstallDeps) {
  Install-PythonDeps
  exit 0
}

if ($Stop) {
  Write-Host "Stopping CursorDesk host on :$Port ..." -ForegroundColor Cyan
  Write-Host "(Does NOT quit Cursor — only the phone host.)" -ForegroundColor DarkGray
  $code = Stop-CursorDeskHost -Port $Port
  if ($code -eq 0) { Write-Host "Host stopped." -ForegroundColor Green }
  exit $code
}

if ($Restart) {
  Write-Host "Restarting CursorDesk host on :$Port ..." -ForegroundColor Cyan
  $code = Stop-CursorDeskHost -Port $Port
  if ($code -ne 0) {
    Write-Host "Could not free port $Port — host not restarted." -ForegroundColor Red
    exit 1
  }
  Start-CursorDeskHostBackground -Port $Port
  Write-Host "Host restarted in background. Same Tailscale URL still works." -ForegroundColor Green
  exit 0
}

Write-Host ""
Write-Host "CursorDesk - Agent (CDP) + Desktop stream" -ForegroundColor Cyan
Write-Host ""

$ts = Find-Tailscale
$backend = $null
$ips = @()
if (-not $ts) {
  Write-Host "Tailscale not found. Run: Start-CursorDesk.bat -InstallTailscale" -ForegroundColor Yellow
} else {
  try {
    $json = & $ts status --json 2>$null | Out-String
    if ($json) { $backend = ($json | ConvertFrom-Json).BackendState }
  } catch {}
  $ips = @(Get-TailscaleIps $ts)
  if ($backend -ne "Running" -or $ips.Count -eq 0) {
    Write-Host ("Tailscale installed but not ready (state: {0})." -f $backend) -ForegroundColor Yellow
    $app = Join-Path $env:ProgramFiles "Tailscale\Tailscale.exe"
    if (Test-Path $app) { Start-Process $app }
  }
}

netsh advfirewall firewall delete rule name="CursorDesk Stream" 2>$null | Out-Null
netsh advfirewall firewall add rule name="CursorDesk Stream" dir=in action=allow protocol=TCP localport=$Port 2>$null | Out-Null

$code = Stop-CursorDeskHost -Port $Port
if ($code -ne 0) {
  Write-Host ""
  Write-Host "Host not started: port is still in use." -ForegroundColor Red
  exit 1
}

$cdpOk = Ensure-CursorCdp -Relaunch:$Relaunch

if ($ts) { $ips = @(Get-TailscaleIps $ts) }
Show-PhoneUrls -TsIps $ips -Backend $backend -Port $Port

Write-Host ("Starting host on :{0} ..." -f $Port)
if ($cdpOk) {
  Write-Host "  Agent tab   = chat / approve (CDP OK)" -ForegroundColor DarkGray
} else {
  Write-Host "  Agent tab   = waiting for CDP (rerun with -Relaunch)" -ForegroundColor DarkYellow
}
Write-Host "  Desktop tab = window stream" -ForegroundColor DarkGray
Write-Host "  Files tab   = browse PC files" -ForegroundColor DarkGray
Write-Host ""
Write-Host "Ctrl+C stops the host. After code updates: Start-CursorDesk.bat -Restart" -ForegroundColor DarkGray
Write-Host ""

$env:CURSORDESK_RELOAD = "0"
$env:CURSORDESK_PORT = "$Port"
$pyExe = Find-Python
& $pyExe (Join-Path $PSScriptRoot "cursor_window_stream.py")
exit $LASTEXITCODE
