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
REM
REM  LOGGING IS BEST-EFFORT; THE LAUNCH IS MANDATORY.
REM  Live testing found a SILENT failure: Stop-ScheduledTask kills the .cmd but not
REM  its python child, so an orphan can keep the day's log file handle open. The
REM  wrapper's >> redirects then hit a sharing violation ("being used by another
REM  process") and cmd NEVER RUNS the redirected command -- python was never
REM  launched, yet Task Scheduler reported rc=0. A no-op that looks like success,
REM  and the in-python single-instance orphan guard never got to run.
REM  So: we PROBE the day's log; on any failure we fall back to a uniquely-suffixed
REM  file (timestamp+random) in the same dir; if that also fails we log nowhere --
REM  and in ALL THREE cases python is launched. The launch line is never
REM  conditional on a redirect succeeding, and a genuine inability to launch exits
REM  with a clear nonzero rc and a visible message.
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
set "LOGFILE=%LOGDIR%\s8_collector_%TODAY%.log"
set "FALLBACK_LOG=%LOGDIR%\s8_collector_%TODAY%_%STAMP%_%RANDOM%.log"

REM --- Best-effort logging: probe the day's log, else a unique fallback, else none
set "LOGOK="
call :probe_log "%LOGFILE%"
if not defined LOGOK (
  echo [run_s8_collector.cmd] WARNING: cannot write "%LOGFILE%" ^(locked by another process, or otherwise unwritable^); falling back to "%FALLBACK_LOG%".
  set "LOGFILE=%FALLBACK_LOG%"
  call :probe_log "%FALLBACK_LOG%"
  if defined LOGOK (
    >>"%FALLBACK_LOG%" echo [%DATE% %TIME%] NOTE: fell back to this file -- the per-day log s8_collector_%TODAY%.log could not be written ^(likely an orphaned python child still holding its handle^).
  ) else (
    echo [run_s8_collector.cmd] WARNING: fallback log "%FALLBACK_LOG%" also unwritable; launching python with NO file logging.
    set "LOGFILE="
  )
)

REM Redirection-FIRST form (>>"file" echo ...) so a trailing digit in the message
REM (e.g. rc=0) can never be misparsed by cmd as a stream-handle redirection.
if defined LOGFILE (
  >>"%LOGFILE%" echo ============================================================
  >>"%LOGFILE%" echo [%DATE% %TIME%] run_s8_collector.cmd START base_py=%BASE_PY%
)

REM --- MANDATORY LAUNCH. Both branches run python; neither depends on logging. ---
if not exist "%BASE_PY%" (
  echo [run_s8_collector.cmd] ERROR: interpreter not found: "%BASE_PY%" -- python NOT launched.
  if defined LOGFILE >>"%LOGFILE%" echo [%DATE% %TIME%] run_s8_collector.cmd ERROR interpreter not found: %BASE_PY% -- python NOT launched.
  endlocal & exit /b 91
)

REM Redirection-FIRST on the whole launch BLOCK. If the redirect cannot be opened
REM (the log got locked in the window since the probe), cmd skips the block
REM entirely -- so RAN stays undefined and we can tell "python never ran" apart
REM from "python ran and returned rc". Without this the failed redirect would
REM leave ERRORLEVEL 0 and we would report the original silent success again.
REM `call set` defers %ERRORLEVEL% so it is read AFTER python exits, not at parse.
set "RAN="
set "RC=199"
if not defined LOGFILE goto :launch_nolog
>>"%LOGFILE%" 2>&1 ( set "RAN=1" & "%BASE_PY%" -u "%REPO%\livebot\s8_collector.py" & call set "RC=%%ERRORLEVEL%%" )
if defined RAN goto :launched
echo [run_s8_collector.cmd] WARNING: log redirect failed at launch time; relaunching with NO file logging so python still starts.
set "LOGFILE="

:launch_nolog
set "RAN=1"
"%BASE_PY%" -u "%REPO%\livebot\s8_collector.py"
set "RC=%ERRORLEVEL%"

:launched
if not defined RAN (
  echo [run_s8_collector.cmd] ERROR: python was NOT launched.
  endlocal & exit /b 92
)

if defined LOGFILE >>"%LOGFILE%" echo [%DATE% %TIME%] run_s8_collector.cmd EXIT rc=%RC%
endlocal & exit /b %RC%

REM ---------------------------------------------------------------------------
REM  :probe_log <path>  -- sets LOGOK=1 iff we can actually append to <path>.
REM  This is the same handle-open path the launch redirect uses, run immediately
REM  before it, so a sharing violation is detected rather than silently swallowed.
REM  NB: a failed redirect leaves ERRORLEVEL *0* (verified) -- that is precisely why
REM  the original bug looked like success -- so we must NOT test errorlevel here.
REM  Instead `&&` runs the set ONLY if the redirected command actually executed.
REM  `type nul` writes nothing, so a successful probe leaves the log untouched.
REM  cmd prints its own "being used by another process" line, which is the reason.
REM ---------------------------------------------------------------------------
:probe_log
set "LOGOK="
>>%1 type nul && set "LOGOK=1"
exit /b 0
