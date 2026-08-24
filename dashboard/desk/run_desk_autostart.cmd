@echo off
REM ===========================================================================
REM  run_desk_autostart.cmd - bring the Trading Desk dashboard (:8502) up at
REM  logon, hidden, exactly once.
REM
REM  Registered as scheduled task "DeskDashboard_8502". Safe to double-click.
REM
REM  1. If something already serves 8502, do nothing (never start a second
REM     Streamlit - it would grab a different port).
REM  2. Resolve the read-only CRM DSN from the User-scope registry. Task
REM     Scheduler can cache its environment until a reboot, so a freshly-set
REM     User TRADINGDESK_CRM_DSN may be invisible to the inherited env, which
REM     would silently drop the desk back to the 3-account fallback roster.
REM     Guarded so it never clobbers an already-set value; never echoed
REM     (contains a password).
REM  3. Launch Streamlit hidden via pythonw (no console window lingers).
REM ===========================================================================
cd /d "%~dp0"

powershell -NoProfile -Command "exit (Test-NetConnection -ComputerName localhost -Port 8502 -InformationLevel Quiet -WarningAction SilentlyContinue)"
if %ERRORLEVEL% EQU 1 exit /b 0

if not defined TRADINGDESK_CRM_DSN (
    for /f "usebackq delims=" %%V in (`powershell -NoProfile -Command "[Environment]::GetEnvironmentVariable('TRADINGDESK_CRM_DSN','User')"`) do set "TRADINGDESK_CRM_DSN=%%V"
)

set "PYTHONPATH=C:\TradingDesk-Local\venv\Lib\site-packages;%PYTHONPATH%"
start "" /b "C:\Users\andre\AppData\Local\Programs\Python\Python312\pythonw.exe" -m streamlit run desk_app.py --server.port 8502 --server.headless true --server.address 127.0.0.1 --browser.gatherUsageStats=false
exit /b 0
