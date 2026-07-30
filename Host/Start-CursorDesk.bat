@echo off
cd /d "%~dp0"
title CursorDesk

if /I "%~1"=="-Stop" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-CursorDesk.ps1" -Stop
  goto :done_quiet
)
if /I "%~1"=="-Restart" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-CursorDesk.ps1" -Restart
  goto :done_quiet
)
if /I "%~1"=="-InstallTailscale" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-CursorDesk.ps1" -InstallTailscale
  goto :done
)
if /I "%~1"=="-InstallDeps" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-CursorDesk.ps1" -InstallDeps
  goto :done
)
if /I "%~1"=="-Relaunch" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-CursorDesk.ps1" -Relaunch
  goto :done
)

echo.
echo CursorDesk - one script for install, start, stop, and restart.
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-CursorDesk.ps1"

:done_quiet
exit /b 0

:done
echo.
pause
