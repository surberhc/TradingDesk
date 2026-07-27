@echo off
setlocal enableextensions
REM ===========================================================================
REM  run_live_data_gateway_open.cmd - IDEMPOTENT ensure-up for the READ-ONLY
REM  live-DATA Gateway (port 4001).
REM
REM  WHY: the nightly IBKR EOD pull (IbkrForwardEodDaily, ~17:30 CT ->
REM  run_ibkr_forward_eod.cmd) connects to the port-4001 live-data Gateway. If
REM  that Gateway happens to be down at 17:30 the whole EOD grab fails. This
REM  pre-nightly wrapper (Andrew registers it ~17:20 CT; this build does NOT
REM  register any task) makes bringing 4001 up IDEMPOTENT: it launches the
REM  gateway ONLY if port 4001 has no listener. If a listener already exists it
REM  prints a note and exits 0 without touching anything. Fail-OPEN -- if the
REM  port check cannot be made, it launches anyway, so the nightly is never left
REM  without a gateway.
REM
REM  STRICTLY ADDITIVE / SAFE: this NEVER kills, reaps, or restarts anything --
REM  it only ever starts a gateway when none is listening. It launches the same
REM  IBC auto-login .bat the LiveDataGwManual task runs.
REM
REM  READ-ONLY: the live-data Gateway login has no execution capability; nothing
REM  here places, modifies, or transmits an order.
REM ===========================================================================

set "VENV_PY=C:\TradingDesk-Local\venv\Scripts\python.exe"
set "GATEWAY_BAT=C:\IBC-Live-Data\StartGatewayLiveData.bat"

REM --- best-effort per-day logging under the local warehouse (never fatal). ---
set "LOGDIR=C:\TradingDesk-Local\warehouse\logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%" 2>nul
set "DAY="
if exist "%VENV_PY%" for /f %%d in ('"%VENV_PY%" -c "import datetime;print(datetime.date.today().strftime('%%Y%%m%%d'))"') do set "DAY=%%d"
if not defined DAY set "DAY=nodate"
set "LOG=%LOGDIR%\live_data_gateway_open_%DAY%.log"

REM --- Only launch if port 4001 has NO listener (idempotent ensure-up) ---
powershell -NoProfile -NonInteractive -Command "if (Get-NetTCPConnection -LocalPort 4001 -State Listen -ErrorAction SilentlyContinue) { exit 10 } else { exit 0 }"
if "%ERRORLEVEL%"=="10" (
  echo [%date% %time%] 4001 already up, not launching>> "%LOG%"
  echo [run_live_data_gateway_open.cmd] 4001 already up, not launching.
  set "RC=0"
  goto :finish
)

REM --- Ensure-up (fail-open: any non-"already up" state reaches here and launches) ---
echo [%date% %time%] no listener on 4001 -- launching the live-data gateway>> "%LOG%"
echo [run_live_data_gateway_open.cmd] no listener on 4001 -- launching the live-data gateway.
cmd /c "%GATEWAY_BAT%">> "%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
echo [%date% %time%] StartGatewayLiveData exit=%RC%>> "%LOG%"

:finish
endlocal & exit /b %RC%
