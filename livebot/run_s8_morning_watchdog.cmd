@echo off
setlocal enableextensions
REM ===========================================================================
REM  run_s8_morning_watchdog.cmd - launcher for the S8 MORNING "STILL DOWN"
REM  WATCHDOG (livebot\s8_morning_watchdog.py). Intended for a WEEKDAY ~08:45 CT
REM  Scheduled Task (registration is a SEPARATE, reviewed step -- this build
REM  deliberately does NOT register any task).
REM
REM  ONE-SHOT: unlike run_s8_collector.cmd this is a single check that runs, sends
REM  at most one email, and exits. It is NOT a long-running service, so there is
REM  NO orphan-reap and NO single-instance lock -- there is no persistent python
REM  child to orphan and no clientId to hold (this module makes no IB connection;
REM  it only TCP-probes port 4003 and may send one email).
REM
REM  ZERO-TRANSMIT: s8_morning_watchdog has no order path at all -- it reads
REM  observable machine state and sends email through the existing dailyreport
REM  mailer. This wrapper adds no transmit capability of any kind.
REM
REM  SINGLE-PROCESS LAUNCH (matches run_s8_collector.cmd): the venv's
REM  Scripts\python.exe is a RELAUNCHER STUB, so we resolve the REAL base
REM  interpreter (sys._base_executable) and the venv site-packages once, then
REM  launch with the base interpreter + PYTHONPATH=venv-site-packages so all venv
REM  deps still import -- exactly ONE process.
REM
REM  IMPORTS: the venv editable installs for connections/strategies still point at
REM  the deleted pre-2026-07-16 My Drive path, so we ALSO put the repo's own
REM  connections\, strategies\ and paperbot\ on PYTHONPATH (belt-and-suspenders;
REM  the module self-inserts its own dir from __file__).
REM
REM  LOGGING IS BEST-EFFORT; THE LAUNCH IS MANDATORY. Output is appended to an
REM  off-Drive, per-day log under the s8_pilot store's logs\ dir. If the day log
REM  cannot be opened we fall back to a uniquely-suffixed file; if that also fails
REM  we log nowhere -- and in ALL cases python is launched. Exit with python's rc.
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

REM One (stable, locale-independent) PowerShell call resolves the per-day filename
REM plus a unique HHmmss stamp for the fallback name.
for /f "usebackq tokens=1,2 delims=_" %%a in (`powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"`) do (
  set "TODAY=%%a"
  set "STAMP=%%b"
)
set "LOGFILE=%LOGDIR%\s8_morning_watchdog_%TODAY%.log"
set "FALLBACK_LOG=%LOGDIR%\s8_morning_watchdog_%TODAY%_%STAMP%_%RANDOM%.log"

REM --- Best-effort logging: probe the day's log, else a unique fallback, else none
set "LOGOK="
call :probe_log "%LOGFILE%"
if not defined LOGOK (
  echo [run_s8_morning_watchdog.cmd] WARNING: cannot write "%LOGFILE%" ^(locked, or otherwise unwritable^); falling back to "%FALLBACK_LOG%".
  set "LOGFILE=%FALLBACK_LOG%"
  call :probe_log "%FALLBACK_LOG%"
  if defined LOGOK (
    >>"%FALLBACK_LOG%" echo [%DATE% %TIME%] NOTE: fell back to this file -- the per-day log s8_morning_watchdog_%TODAY%.log could not be written.
  ) else (
    echo [run_s8_morning_watchdog.cmd] WARNING: fallback log "%FALLBACK_LOG%" also unwritable; launching python with NO file logging.
    set "LOGFILE="
  )
)

REM Redirection-FIRST form (>>"file" echo ...) so a trailing digit in the message
REM can never be misparsed by cmd as a stream-handle redirection.
if defined LOGFILE (
  >>"%LOGFILE%" echo ============================================================
  >>"%LOGFILE%" echo [%DATE% %TIME%] run_s8_morning_watchdog.cmd START base_py=%BASE_PY%
)

REM --- MANDATORY LAUNCH. Both branches run python; neither depends on logging. ---
if not exist "%BASE_PY%" (
  echo [run_s8_morning_watchdog.cmd] ERROR: interpreter not found: "%BASE_PY%" -- python NOT launched.
  if defined LOGFILE >>"%LOGFILE%" echo [%DATE% %TIME%] run_s8_morning_watchdog.cmd ERROR interpreter not found: %BASE_PY% -- python NOT launched.
  endlocal & exit /b 91
)

REM Redirection-FIRST on the whole launch BLOCK. If the redirect cannot be opened
REM (the log got locked in the window since the probe), cmd skips the block
REM entirely -- so RAN stays undefined and we can tell "python never ran" apart
REM from "python ran and returned rc". `call set` defers %ERRORLEVEL% so it is
REM read AFTER python exits, not at parse.
set "RAN="
set "RC=199"
if not defined LOGFILE goto :launch_nolog
>>"%LOGFILE%" 2>&1 ( set "RAN=1" & "%BASE_PY%" -u "%REPO%\livebot\s8_morning_watchdog.py" & call set "RC=%%ERRORLEVEL%%" )
if defined RAN goto :launched
echo [run_s8_morning_watchdog.cmd] WARNING: log redirect failed at launch time; relaunching with NO file logging so python still starts.
set "LOGFILE="

:launch_nolog
set "RAN=1"
"%BASE_PY%" -u "%REPO%\livebot\s8_morning_watchdog.py"
set "RC=%ERRORLEVEL%"

:launched
if not defined RAN (
  echo [run_s8_morning_watchdog.cmd] ERROR: python was NOT launched.
  endlocal & exit /b 92
)

if defined LOGFILE >>"%LOGFILE%" echo [%DATE% %TIME%] run_s8_morning_watchdog.cmd EXIT rc=%RC%
endlocal & exit /b %RC%

REM ---------------------------------------------------------------------------
REM  :probe_log <path>  -- sets LOGOK=1 iff we can actually append to <path>.
REM  Same handle-open path the launch redirect uses, run immediately before it, so
REM  a sharing violation is detected rather than silently swallowed. A failed
REM  redirect leaves ERRORLEVEL *0*, so we must NOT test errorlevel here; `&&`
REM  runs the set ONLY if the redirected command actually executed. `type nul`
REM  writes nothing, so a successful probe leaves the log untouched.
REM ---------------------------------------------------------------------------
:probe_log
set "LOGOK="
>>%1 type nul && set "LOGOK=1"
exit /b 0
