@echo off
REM ===========================================================================
REM  run_s0_month_end_snapshot.cmd - Job A: S0 month-end CLOSE-TIME holdings snapshot.
REM
REM  Launcher for the S0MonthEndSnapshot scheduled task (runs every weekday ~2:50pm CT,
REM  while the live-trading Gateway is still up before the ~3:05pm teardown; Andrew
REM  registers the task himself -- this build deliberately does NOT register any task).
REM  The script self-checks the NYSE trading calendar and acts ONLY on Strategy 0's
REM  month-end rebalance SIGNAL day (the last trading day of the month); every other
REM  weekday it does nothing and exits 0.
REM
REM  On the signal day it connects READ-ONLY to the live-trading Gateway (port 4003,
REM  clientId s0_month_end_snapshot) and writes the account's positions + NetLiquidation
REM  to an off-repo JSON that the evening verdict job (s0_month_end_notice.py, Job B)
REM  loads after the 7pm Tiingo pull. INFORMATIONAL + READ-ONLY: builds no order, calls
REM  no order-placement method, transmits nothing (readonly=True is the wall). Not
REM  order-affecting.
REM ===========================================================================

set "VENV_PY=C:\TradingDesk-Local\venv\Scripts\python.exe"
set "REPO=%~dp0.."

REM --- PYTHONPATH shim so `from connections import ...` resolves (the connections
REM     package lives under %REPO%\connections). The script also adds this path itself
REM     (belt-and-suspenders), plus its own dir for the sibling `import s0_month_end_notice`. ---
for /f "usebackq delims=" %%i in (`%VENV_PY% -c "import sys;print(next((p for p in sys.path if p.endswith('site-packages') and 'venv' in p.lower()), ''))"`) do set "VENV_SITE=%%i"
set "PYTHONPATH=%VENV_SITE%;%REPO%\connections;%PYTHONPATH%"

cd /d "%~dp0"

REM --- best-effort per-day logging under the local state dir (never fatal). ---
set "LOGDIR=C:\TradingDesk-Local\state\dailyreport"
if not exist "%LOGDIR%" mkdir "%LOGDIR%" 2>nul
for /f %%d in ('%VENV_PY% -c "import datetime;print(datetime.date.today().strftime('%%Y%%m%%d'))"') do set "DAY=%%d"
set "LOG=%LOGDIR%\s0_month_end_snapshot_%DAY%.log"

echo [%date% %time%] === S0 month-end snapshot self-check ===>> "%LOG%"
"%VENV_PY%" s0_month_end_snapshot.py >> "%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
echo [%date% %time%] s0_month_end_snapshot exit=%RC%>> "%LOG%"
exit /b %RC%
