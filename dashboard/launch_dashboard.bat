@echo off
REM ===========================================================================
REM  launch_dashboard.bat - one double-click launcher for the read-only
REM  Trading Desk dashboard. Calls launch_dashboard.ps1 hidden (no console
REM  window lingers). The .ps1 starts the Streamlit server in the background
REM  if it isn't already running, then opens the default browser to
REM  http://localhost:8501. If it's already running, it just opens the browser.
REM
REM  This is the target of the Desktop shortcut "Trading Desk Dashboard".
REM ===========================================================================
start "" /min powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0launch_dashboard.ps1"
exit /b 0
