@echo off
setlocal enableextensions
REM ===========================================================================
REM  run_s8_collector.cmd - launcher for the S8 intraday ATM-band market-context
REM  COLLECTOR (livebot\s8_collector.py). Intended for a WEEKDAY market-session
REM  Scheduled Task (registration is a SEPARATE, reviewed step -- this build
REM  deliberately does NOT register any task).
REM
REM  ZERO-TRANSMIT: s8_collector connects readonly=True to the live-trading Gateway
REM  (port 4003) and only ever reqMktData/cancelMktData/reads -- no order path. This
REM  wrapper adds no transmit capability of any kind -- it only launches the reader.
REM
REM  SINGLE-PROCESS LAUNCH (matches datacollector\spxw_1m_supervisor.py):
REM  the venv's Scripts\python.exe is a RELAUNCHER STUB -- it re-execs the base
REM  interpreter as a child, so launching through it would leave a stub+worker PAIR
REM  (two processes). We resolve the REAL base interpreter (sys._base_executable)
REM  and the venv site-packages once, then launch with the base interpreter +
REM  PYTHONPATH=venv-site-packages so all venv deps still import -- exactly ONE
REM  collector process.
REM
REM  IMPORTS: the venv editable installs for connections/strategies still point at
REM  the deleted pre-2026-07-16 My Drive path, so we ALSO put the repo's own
REM  connections\ and strategies\ (and paperbot\) on PYTHONPATH. s8_collector already
REM  self-inserts these from __file__; this is belt-and-suspenders so the imports
REM  work regardless of the broken editable installs.
REM
REM  Output is appended to an off-Drive, per-day rotating log under the s8_pilot
REM  store's logs\ dir (never a My Drive path).
REM ===========================================================================

set "VENV_PY=C:\TradingDesk-Local\venv\Scripts\python.exe"
set "REPO=%~dp0.."
set "LOGDIR=C:\TradingDesk-Local\s8_pilot\logs"

REM --- Resolve the real base interpreter + venv site-packages (avoid the stub) ---
for /f "usebackq delims=" %%i in (`%VENV_PY% -c "import sys;print(getattr(sys,'_base_executable',None) or sys.executable)"`) do set "BASE_PY=%%i"
for /f "usebackq delims=" %%i in (`%VENV_PY% -c "import sys;print(next((p for p in sys.path if p.endswith('site-packages') and 'venv' in p.lower()), ''))"`) do set "VENV_SITE=%%i"

if not defined BASE_PY set "BASE_PY=%VENV_PY%"

REM --- PYTHONPATH: venv deps + repo packages (connections/strategies/paperbot) ---
set "PYTHONPATH=%VENV_SITE%;%REPO%\connections;%REPO%\strategies;%REPO%\paperbot;%PYTHONPATH%"

if not exist "%LOGDIR%" mkdir "%LOGDIR%"
REM One (stable, locale-independent) PowerShell call resolves the per-day filename.
for /f "usebackq delims=" %%d in (`powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"`) do set "TODAY=%%d"
set "LOGFILE=%LOGDIR%\s8_collector_%TODAY%.log"

REM Redirection-FIRST form (>>"file" echo ...) so a trailing digit in the message
REM (e.g. rc=0) can never be misparsed by cmd as a stream-handle redirection.
>>"%LOGFILE%" echo ============================================================
>>"%LOGFILE%" echo [%DATE% %TIME%] run_s8_collector.cmd START base_py=%BASE_PY%

"%BASE_PY%" -u "%REPO%\livebot\s8_collector.py" >> "%LOGFILE%" 2>&1
set "RC=%ERRORLEVEL%"

>>"%LOGFILE%" echo [%DATE% %TIME%] run_s8_collector.cmd EXIT rc=%RC%
endlocal & exit /b %RC%
