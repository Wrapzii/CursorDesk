# Frees the CursorDesk host port. Never touches Cursor itself.
# Exit 0 = port free, exit 1 = still occupied.
#
# Killing the PID that netstat reports is not enough. When the host ran with
# uvicorn reload, the server lived in a multiprocessing child that inherited the
# listening socket; if the parent dies first, netstat keeps reporting the dead
# parent and the child holds the port. That produced WinError 10048 on restart.
param(
  [int]$Port = 8765,
  [switch]$Quiet
)

$ErrorActionPreference = "Continue"

function Write-Step {
  param([string]$Message, [string]$Color = "Gray")
  if (-not $Quiet) { Write-Host $Message -ForegroundColor $Color }
}

function Get-PortOwners {
  param([int]$Port)
  return @(
    Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue |
      ForEach-Object { [int]$_.OwningProcess } |
      Where-Object { $_ -gt 0 }
  )
}

function Stop-CursorDeskHost {
  param([int]$Port)

  $targets = New-Object 'System.Collections.Generic.HashSet[int]'
  foreach ($owner in Get-PortOwners -Port $Port) { [void]$targets.Add($owner) }

  $python = @(
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue
  )

  foreach ($proc in $python) {
    if ($proc.CommandLine -and $proc.CommandLine -match 'cursor_window_stream') {
      [void]$targets.Add([int]$proc.ProcessId)
    }
  }

  # Reload children inherit the socket, so include any spawn whose parent is a target.
  foreach ($proc in $python) {
    if (-not $proc.CommandLine) { continue }
    if ($proc.CommandLine -notmatch 'multiprocessing-fork|spawn_main') { continue }
    if ($proc.CommandLine -match 'parent_pid=(\d+)') {
      if ($targets.Contains([int]$Matches[1])) { [void]$targets.Add([int]$proc.ProcessId) }
    }
  }

  if ($targets.Count -eq 0) { return }

  foreach ($pidToStop in $targets) {
    Write-Step ("  stopping host process {0}" -f $pidToStop)
    Stop-Process -Id $pidToStop -Force -ErrorAction SilentlyContinue
  }
}

for ($attempt = 1; $attempt -le 3; $attempt++) {
  if ((Get-PortOwners -Port $Port).Count -eq 0) {
    Write-Step ("Port {0} is free." -f $Port) "Green"
    exit 0
  }
  Write-Step ("Freeing port {0} (attempt {1}/3) ..." -f $Port, $attempt) "Cyan"
  Stop-CursorDeskHost -Port $Port
  Start-Sleep -Milliseconds 700
}

if ((Get-PortOwners -Port $Port).Count -eq 0) {
  Write-Step ("Port {0} is free." -f $Port) "Green"
  exit 0
}

$stuck = (Get-PortOwners -Port $Port) -join ", "
Write-Host ("Port {0} is still held by PID(s): {1}" -f $Port, $stuck) -ForegroundColor Red
Write-Host "Close that process manually, then rerun." -ForegroundColor Yellow
exit 1
