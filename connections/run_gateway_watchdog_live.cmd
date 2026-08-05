@echo off
setlocal enableextensions
REM ===========================================================================
REM  run_gateway_watchdog_live.cmd - runs the LIVE-DATA (port 4001) IB Gateway
REM  auto-restart WATCHDOG once (connections\connections\gateway_watchdog_live.py).
REM
REM  Registered as Windows Scheduled Task "LiveDataGatewayWatchdog" (every ~5 min,
REM  run-whether-logged-on, HIGHEST run level) so it fires independently of any
REM  Claude session and survives reboot/logoff. Its job: keep the 24/7 live-data
REM  Gateway up -- detect a WEDGED gateway (a login that never comes up / stopped
REM  serving data) and recover it, AFTER a grace window, under a rolling-hour
REM  restart cap, NEVER inside the 02:05 ET self-restart maintenance window, and --
REM  since 2026-08-05 -- NEVER killing a gateway that is still completing login /
REM  sitting at the weekly IBKR 2FA prompt (the LOGIN/2FA grace).
REM
REM  WHY ELEVATED: the IB Gateway processes run ELEVATED. A non-elevated process
REM  cannot kill them (taskkill Access Denied) nor read their command lines, so
REM  this task MUST run with highest privileges (mirror of the paper GatewayWatchdog).
REM
REM  READ-ONLY: the live-data Gateway login has NO execution capability and
REM  ibkr_live_data.connect() is hardcoded read-only. Nothing here places, modifies,
REM  or transmits an order. State/logs are LOCAL (off Drive).
REM
REM  One check per invocation (the scheduler provides the 5-min cadence; once-per-run
REM  is reboot/crash-resilient). main() never raises and always exits 0.
REM ===========================================================================

set "VENV_PY=C:\TradingDesk-Local\venv\Scripts\python.exe"
set "REPO=%~dp0.."
set "LOGDIR=C:\TradingDesk-Local\warehouse"
set "LOG=%LOGDIR%\gateway_watchdog_live.log"

REM --- PYTHONPATH shim (belt-and-suspenders; the editable install already resolves
REM     `connections`, but this keeps the launch robust to a broken editable state). ---
set "PYTHONPATH=%REPO%\connections;%PYTHONPATH%"

if not exist "%LOGDIR%" mkdir "%LOGDIR%" 2>nul
cd /d "%REPO%\connections\connections"
"%VENV_PY%" gateway_watchdog_live.py >> "%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
endlocal & exit /b %RC%
