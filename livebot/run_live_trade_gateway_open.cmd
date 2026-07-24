@echo off
setlocal enableextensions
REM ===========================================================================
REM  run_live_trade_gateway_open.cmd - PORT-AWARE cold-start for the S8 live
REM  pilot's LIVE-TRADING Gateway (port 4003).
REM
REM  WHY: the scheduled task used to run StartGatewayLiveTrade.bat DIRECTLY, with
REM  no check of whether a gateway was already up. If a self-heal (or a leftover
REM  instance) had 4003 bound / still booting, that second launch lost the bind
REM  race and became an unbound orphan (incident 2026-07-23). This wrapper makes
REM  the cold start IDEMPOTENT: reap orphans, then launch the gateway ONLY if 4003
REM  has no listener. Fail-OPEN -- if the port check cannot be made, it launches
REM  anyway, so a cold morning is never left without a gateway.
REM
REM  ZERO-TRANSMIT: launches the same IBC auto-login .bat as before; no order path.
REM  Registered as the action for scheduled task "LiveTradeGatewayOpen_0815CT".
REM ===========================================================================

set "VENV_PY=C:\TradingDesk-Local\venv\Scripts\python.exe"
set "REPO=%~dp0.."
set "GATEWAY_BAT=C:\IBC-Live-Trade\StartGatewayLiveTrade.bat"

REM --- Resolve the real base interpreter + venv site-packages (avoid the stub) ---
for /f "usebackq delims=" %%i in (`%VENV_PY% -c "import sys;print(getattr(sys,'_base_executable',None) or sys.executable)"`) do set "BASE_PY=%%i"
for /f "usebackq delims=" %%i in (`%VENV_PY% -c "import sys;print(next((p for p in sys.path if p.endswith('site-packages') and 'venv' in p.lower()), ''))"`) do set "VENV_SITE=%%i"
if not defined BASE_PY set "BASE_PY=%VENV_PY%"
set "PYTHONPATH=%VENV_SITE%;%REPO%\connections;%REPO%\strategies;%REPO%\paperbot;%PYTHONPATH%"

REM --- 1) Reap any orphaned live-trade gateway first (best-effort, never blocks) ---
if exist "%BASE_PY%" (
  "%BASE_PY%" -u "%REPO%\livebot\s8_gateway_reap.py"
) else (
  echo [run_live_trade_gateway_open.cmd] WARN: interpreter not found: "%BASE_PY%" -- skipping orphan reap.
)

REM --- 2) Only launch if port 4003 has NO listener (idempotent cold start) ---
powershell -NoProfile -NonInteractive -Command "if (Get-NetTCPConnection -LocalPort 4003 -State Listen -ErrorAction SilentlyContinue) { exit 10 } else { exit 0 }"
if "%ERRORLEVEL%"=="10" (
  echo [run_live_trade_gateway_open.cmd] port 4003 already has a listener -- gateway already up, NOT launching a second one.
  set "RC=0"
  goto :finish
)

REM --- 3) Cold start (fail-open: any non-"already up" state reaches here and launches) ---
echo [run_live_trade_gateway_open.cmd] no listener on 4003 -- launching the live-trade gateway.
cmd /c "%GATEWAY_BAT%"
set "RC=%ERRORLEVEL%"

:finish
endlocal & exit /b %RC%
