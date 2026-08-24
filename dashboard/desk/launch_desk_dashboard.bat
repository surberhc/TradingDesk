@echo off
REM ===========================================================================
REM  launch_desk_dashboard.bat - one double-click launcher for the read-only
REM  Trading Desk dashboard on http://localhost:8502.
REM
REM  This is the target of the Desktop shortcut "Trading Desk Dashboard".
REM
REM  Delegates the actual start to run_desk_autostart.cmd (the same idempotent
REM  launcher the DeskDashboard_8502 scheduled task uses), so a double-click can
REM  never produce a second Streamlit server on a shifted port. Then waits for
REM  the port to accept connections (cap ~40s) and opens the browser. Opens the
REM  browser regardless, so it never hangs.
REM ===========================================================================
call "%~dp0run_desk_autostart.cmd"

powershell -NoProfile -Command "$d=(Get-Date).AddSeconds(40); do { Start-Sleep -Milliseconds 750; $up = Test-NetConnection -ComputerName localhost -Port 8502 -InformationLevel Quiet -WarningAction SilentlyContinue } while (-not $up -and (Get-Date) -lt $d)"

start "" "http://localhost:8502"
exit /b 0
