@echo off
REM ===========================================================================
REM  run_outofspec_scan_check.cmd - whole-book out-of-spec consolidated check.
REM
REM  Launcher for the OutOfSpecScanCheck scheduled task. Runs the READ-ONLY
REM  whole-book out-of-spec scan (the same pure rebalance_engine.build_plan the
REM  Control Plane whole-book panel uses -- no broker, armed=False, builds and
REM  transmits nothing) and, if any account is out of spec, posts ONE consolidated
REM  "N of M accounts out of spec -- rebalance needed" notice (with an expandable
REM  per-account detail list) to the in-app Action Center. With nothing out of spec
REM  it posts nothing. Snoozed by the operator -> it skips posting.
REM
REM  Reads the live CRM through the read-only tradingdesk_readonly Postgres role via
REM  the TRADINGDESK_CRM_DSN env var (a task running as the user inherits that User
REM  env var). INFORMATIONAL + READ-ONLY: no order object, no order-placement call,
REM  transmits nothing. Not order-affecting.
REM ===========================================================================

set "VENV_PY=C:\TradingDesk-Local\venv\Scripts\python.exe"
set "REPO=%~dp0.."

REM --- Resolve the live CRM DSN from the User-scope registry if it is not already
REM     present in this process env. Task Scheduler can cache its environment until a
REM     reboot, so a freshly-set User TRADINGDESK_CRM_DSN may be invisible to the
REM     inherited env, silently dropping the read-only CRM role back to the built-in
REM     allow-list. Pull it live here. Only set when empty (never clobber an
REM     already-correct inherited value); never echo it (it contains a password). ---
if not defined TRADINGDESK_CRM_DSN (
    for /f "usebackq delims=" %%V in (`powershell -NoProfile -Command "[Environment]::GetEnvironmentVariable('TRADINGDESK_CRM_DSN','User')"`) do set "TRADINGDESK_CRM_DSN=%%V"
)

REM --- PYTHONPATH shim so `import rebalance_engine` / `from connections import ...`
REM     resolve; the script also adds connections + paperbot + dashboard\desk to
REM     sys.path itself (belt-and-suspenders). ---
for /f "usebackq delims=" %%i in (`%VENV_PY% -c "import sys;print(next((p for p in sys.path if p.endswith('site-packages') and 'venv' in p.lower()), ''))"`) do set "VENV_SITE=%%i"
set "PYTHONPATH=%VENV_SITE%;%REPO%\connections;%REPO%\paperbot;%REPO%\dashboard\desk;%PYTHONPATH%"

cd /d "%~dp0"

REM --- best-effort per-day logging under the local state dir (never fatal). ---
set "LOGDIR=C:\TradingDesk-Local\state\dailyreport"
if not exist "%LOGDIR%" mkdir "%LOGDIR%" 2>nul
for /f %%d in ('%VENV_PY% -c "import datetime;print(datetime.date.today().strftime('%%Y%%m%%d'))"') do set "DAY=%%d"
set "LOG=%LOGDIR%\outofspec_scan_check_%DAY%.log"

echo [%date% %time%] === whole-book out-of-spec scan check ===>> "%LOG%"
"%VENV_PY%" outofspec_scan_check.py >> "%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
echo [%date% %time%] outofspec_scan_check exit=%RC%>> "%LOG%"
exit /b %RC%
