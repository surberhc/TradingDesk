@echo off
setlocal enableextensions
REM ===========================================================================
REM  run_livedata_chained_open.cmd - launcher for the DEPENDENCY-GATED morning
REM  bring-up of the read-only live-DATA Gateway (port 4001), chained behind 4003
REM  (livebot\livedata_chained_open.py).
REM
REM  Registered as Windows Scheduled Task "LiveDataGatewayChainedOpen_0805CT"
REM  (weekdays, every ~5 min, 08:05-12:00 CT), launched hidden via run_hidden.vbs.
REM  Each cycle: if 4001 is already up -> nothing; else if 4003 is confirmed up ->
REM  launch 4001 (fires its own 2FA push); else (4003 still pending) -> wait. This
REM  guarantees only ONE pending IBKR Mobile 2FA at a time.
REM
REM  Runs as the normal user WHEN LOGGED ON (no elevation) so the Gateway GUI / 2FA
REM  renders on the desktop. The launch goes through ibkr_live_data.ensure_gateway()
REM  (mutex-guarded, shared with the watchdog -> no orphan pileup).
REM
REM  READ-ONLY lane: the live-data login has no execution capability; nothing here
REM  places or transmits an order. Only OBSERVES 4003's port (never touches 4003).
REM  Logging best-effort (off Drive); launch mandatory; exit with python's rc.
REM ===========================================================================

set "VENV_PY=C:\TradingDesk-Local\venv\Scripts\python.exe"
set "REPO=%~dp0.."
set "LOGDIR=C:\TradingDesk-Local\warehouse\logs"
set "LOG=%LOGDIR%\livedata_chained_open.log"

REM --- PYTHONPATH: repo packages the module (lazily) needs (s8_gateway_alert in
REM     livebot; connections.ibkr_live_data for the gated launch). ---
set "PYTHONPATH=%REPO%\livebot;%REPO%\connections;%PYTHONPATH%"

if not exist "%LOGDIR%" mkdir "%LOGDIR%" 2>nul
echo [%date% %time%] === livedata chained open (gate 4001 behind 4003) ===>> "%LOG%"
"%VENV_PY%" "%REPO%\livebot\livedata_chained_open.py" >> "%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
echo [%date% %time%] livedata_chained_open exit=%RC%>> "%LOG%"
endlocal & exit /b %RC%
