@echo off
REM ===========================================================================
REM  run_ibkr_forward_eod.cmd - nightly EOD option-chain grab via the validated
REM  IBKR forward collector (ThetaData->IBKR cutover, 2026-07-27).
REM
REM  Launcher for the IbkrForwardEodDaily scheduled task (~17:30 CT; Andrew
REM  registers the task himself -- this build deliberately does NOT register any
REM  task). Replaces the retired ThetaEodDaily/eod_daily.py path.
REM
REM  Runs forward_daily_live.py against the SECOND, read-only-only live-data
REM  Gateway (port 4001) for the INDEX-ONLY universe SPX SPXW RUT NDX. These are
REM  the only roots with an IBKR options-data entitlement; SPY/QQQ/ETFs were
REM  dropped in the cutover (no entitlement -> Error 10091). The collector writes
REM  the MAIN warehouse namespace (raw/options) and the canonical "forward"
REM  jobstatus key that the EOD report + heartbeat_alarm's "forward" watchdog read.
REM
REM  This job is READ-ONLY market data: the live-data Gateway login has no
REM  execution capability and connections.ibkr_live_data.connect() is hardcoded
REM  read-only. Nothing here places, modifies, or transmits an order.
REM ===========================================================================

set "VENV_PY=C:\TradingDesk-Local\venv\Scripts\python.exe"
set "REPO=%~dp0.."

REM --- base interpreter + PYTHONPATH shim so `from connections import ...`
REM     resolves (the connections package lives under %REPO%\connections). The
REM     script's own dir (datacollector) is added to sys.path[0] automatically, so
REM     `import config` / `import ibkr_forward_live` resolve from cwd below. ---
for /f "usebackq delims=" %%i in (`%VENV_PY% -c "import sys;print(next((p for p in sys.path if p.endswith('site-packages') and 'venv' in p.lower()), ''))"`) do set "VENV_SITE=%%i"
set "PYTHONPATH=%VENV_SITE%;%REPO%\connections;%PYTHONPATH%"

cd /d "%~dp0"

REM --- best-effort per-day logging under the local warehouse (never fatal). ---
set "LOGDIR=C:\TradingDesk-Local\warehouse\logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%" 2>nul
for /f %%d in ('%VENV_PY% -c "import datetime;print(datetime.date.today().strftime('%%Y%%m%%d'))"') do set "DAY=%%d"
set "LOG=%LOGDIR%\ibkr_forward_eod_%DAY%.log"

echo [%date% %time%] === IBKR forward EOD grab: SPX SPXW RUT NDX ===>> "%LOG%"
"%VENV_PY%" forward_daily_live.py SPX SPXW RUT NDX >> "%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
echo [%date% %time%] forward_daily_live exit=%RC%>> "%LOG%"
exit /b %RC%
