@echo off
REM ===========================================================================
REM  run_s0_month_end_notice.cmd - Job B: S0 month-end EXACT trade/no-trade verdict.
REM
REM  Launcher for the S0MonthEndNotice scheduled task (runs every weekday evening
REM  ~19:15 CT, AFTER the ~7pm Tiingo close-data pull; Andrew registers the task
REM  himself -- this build deliberately does NOT register any task). The script
REM  self-checks the NYSE trading calendar and emails ONLY on Strategy 0's month-end
REM  rebalance SIGNAL day (the last trading day of the month); every other evening it
REM  does nothing and exits 0.
REM
REM  On the signal day it loads Job A's close-time holdings snapshot, computes S0's
REM  target on the final close data, sizes the plan, and emails one of three exact
REM  verdicts: "TRADE tomorrow - N leg(s)", "NO trade tomorrow", or (fail-honest)
REM  "could not read holdings at close". INFORMATIONAL + READ-ONLY: Job B connects to
REM  NO gateway and reads NO account live -- it only reads the JSON snapshot and runs
REM  the pure offline planner, reusing the existing EOD mailer (dailyreport\mailer.py).
REM  It transmits nothing and touches no order path. Not order-affecting.
REM ===========================================================================

set "VENV_PY=C:\TradingDesk-Local\venv\Scripts\python.exe"
set "REPO=%~dp0.."

REM --- PYTHONPATH shim so `from connections import market_calendar` resolves
REM     (the connections package lives under %REPO%\connections). The script's own
REM     dir (dailyreport) is added to sys.path[0] automatically when run by name,
REM     so `import mailer` resolves from cwd below -- and the script also adds both
REM     paths itself, so this is belt-and-suspenders. ---
for /f "usebackq delims=" %%i in (`%VENV_PY% -c "import sys;print(next((p for p in sys.path if p.endswith('site-packages') and 'venv' in p.lower()), ''))"`) do set "VENV_SITE=%%i"
set "PYTHONPATH=%VENV_SITE%;%REPO%\connections;%PYTHONPATH%"

cd /d "%~dp0"

REM --- best-effort per-day logging under the local state dir (never fatal). ---
set "LOGDIR=C:\TradingDesk-Local\state\dailyreport"
if not exist "%LOGDIR%" mkdir "%LOGDIR%" 2>nul
for /f %%d in ('%VENV_PY% -c "import datetime;print(datetime.date.today().strftime('%%Y%%m%%d'))"') do set "DAY=%%d"
set "LOG=%LOGDIR%\s0_month_end_notice_%DAY%.log"

echo [%date% %time%] === S0 month-end notice self-check ===>> "%LOG%"
"%VENV_PY%" s0_month_end_notice.py >> "%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
echo [%date% %time%] s0_month_end_notice exit=%RC%>> "%LOG%"
exit /b %RC%
