@echo off
REM ===========================================================================
REM  run_s0_cash_deploy_check.cmd - S0 idle-cash "consider deploying" check.
REM
REM  Launcher for the S0CashDeployCheck scheduled task (runs every weekday ~2:55pm CT,
REM  while the live-trading Gateway is still up before the ~3:05pm teardown; Andrew
REM  registers the task himself -- this build deliberately does NOT register any task).
REM  Connects READ-ONLY to the live-trading Gateway (port 4003, clientId
REM  s0_cash_deploy_check) and reads the S0 account's NetLiquidation + TotalCashValue.
REM  When free cash held ABOVE the standing cash buffer exceeds the operational
REM  fraction-of-NAV threshold, it posts an "idle cash -- consider deploying" notice to
REM  the in-app Action Center; below the threshold it posts nothing. INFORMATIONAL +
REM  READ-ONLY: builds no order, calls no order-placement method, transmits nothing
REM  (readonly=True is the wall). Not order-affecting.
REM ===========================================================================

set "VENV_PY=C:\TradingDesk-Local\venv\Scripts\python.exe"
set "REPO=%~dp0.."

REM --- PYTHONPATH shim so `from connections import ...` resolves; the script also adds
REM     connections + paperbot + dashboard\desk to sys.path itself (belt-and-suspenders). ---
for /f "usebackq delims=" %%i in (`%VENV_PY% -c "import sys;print(next((p for p in sys.path if p.endswith('site-packages') and 'venv' in p.lower()), ''))"`) do set "VENV_SITE=%%i"
set "PYTHONPATH=%VENV_SITE%;%REPO%\connections;%PYTHONPATH%"

cd /d "%~dp0"

REM --- best-effort per-day logging under the local state dir (never fatal). ---
set "LOGDIR=C:\TradingDesk-Local\state\dailyreport"
if not exist "%LOGDIR%" mkdir "%LOGDIR%" 2>nul
for /f %%d in ('%VENV_PY% -c "import datetime;print(datetime.date.today().strftime('%%Y%%m%%d'))"') do set "DAY=%%d"
set "LOG=%LOGDIR%\s0_cash_deploy_check_%DAY%.log"

echo [%date% %time%] === S0 idle-cash deploy check ===>> "%LOG%"
"%VENV_PY%" s0_cash_deploy_check.py >> "%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
echo [%date% %time%] s0_cash_deploy_check exit=%RC%>> "%LOG%"
exit /b %RC%
