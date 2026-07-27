@echo off
setlocal enableextensions
REM ===========================================================================
REM  run_verify_ibkr_nightly.cmd - launcher for the ONE-SHOT "did tonight's IBKR
REM  EOD pull succeed?" checker (datacollector\verify_ibkr_nightly.py). Intended
REM  for a WEEKNIGHT ~18:45 CT Scheduled Task, AFTER IbkrForwardEodDaily (17:30 CT)
REM  has had time to finish. Task REGISTRATION is a SEPARATE, reviewed step -- this
REM  build deliberately does NOT register any task.
REM
REM  ONE-SHOT: a single check that reads today's warehouse parquets, sends at most
REM  one PASS/FAIL email, and exits. NOT a long-running service, so there is NO
REM  orphan-reap and NO single-instance lock -- there is no persistent python child
REM  to orphan and no clientId to hold (this checker makes NO IB connection; it only
REM  reads files and may send one email through the existing dailyreport mailer).
REM
REM  SINGLE-PROCESS LAUNCH (matches run_s8_morning_watchdog.cmd): the venv's
REM  Scripts\python.exe is a RELAUNCHER STUB, so we resolve the REAL base
REM  interpreter (sys._base_executable) and the venv site-packages once, then launch
REM  with the base interpreter + PYTHONPATH=venv-site-packages so all venv deps
REM  (pandas/pyarrow) still import -- exactly ONE process.
REM
REM  IMPORTS: the checker does `import config` (datacollector\config.py) and lazily
REM  imports the dailyreport mailer + repo connections. The venv editable installs
REM  still point at the deleted pre-2026-07-16 My Drive path, so we ALSO put the
REM  datacollector dir (for config), connections\ and dailyreport\ on PYTHONPATH
REM  (belt-and-suspenders; the module self-inserts its own dir from __file__).
REM
REM  LOGGING IS BEST-EFFORT; THE LAUNCH IS MANDATORY. Output is appended to an
REM  off-Drive, per-day log under the warehouse logs\ dir. If the day log cannot be
REM  opened we fall back to a uniquely-suffixed file; if that also fails we log
REM  nowhere -- and in ALL cases python is launched. Exit with python's rc.
REM ===========================================================================

set "VENV_PY=C:\TradingDesk-Local\venv\Scripts\python.exe"
set "REPO=%~dp0.."
set "DCDIR=%~dp0."
set "LOGDIR=C:\TradingDesk-Local\warehouse\logs"

REM --- Resolve the real base interpreter + venv site-packages (avoid the stub) ---
for /f "usebackq delims=" %%i in (`%VENV_PY% -c "import sys;print(getattr(sys,'_base_executable',None) or sys.executable)"`) do set "BASE_PY=%%i"
for /f "usebackq delims=" %%i in (`%VENV_PY% -c "import sys;print(next((p for p in sys.path if p.endswith('site-packages') and 'venv' in p.lower()), ''))"`) do set "VENV_SITE=%%i"

if not defined BASE_PY set "BASE_PY=%VENV_PY%"

REM --- PYTHONPATH: venv deps + repo packages (datacollector/connections/dailyreport) ---
set "PYTHONPATH=%VENV_SITE%;%DCDIR%;%REPO%\connections;%REPO%\dailyreport;%PYTHONPATH%"

if not exist "%LOGDIR%" mkdir "%LOGDIR%"

REM One (stable, locale-independent) PowerShell call resolves the per-day filename
REM plus a unique HHmmss stamp for the fallback name.
for /f "usebackq tokens=1,2 delims=_" %%a in (`powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"`) do (
  set "TODAY=%%a"
  set "STAMP=%%b"
)
set "LOGFILE=%LOGDIR%\verify_ibkr_nightly_%TODAY%.log"
set "FALLBACK_LOG=%LOGDIR%\verify_ibkr_nightly_%TODAY%_%STAMP%_%RANDOM%.log"

REM --- Best-effort logging: probe the day's log, else a unique fallback, else none
set "LOGOK="
call :probe_log "%LOGFILE%"
if not defined LOGOK (
  echo [run_verify_ibkr_nightly.cmd] WARNING: cannot write "%LOGFILE%" ^(locked, or otherwise unwritable^); falling back to "%FALLBACK_LOG%".
  set "LOGFILE=%FALLBACK_LOG%"
  call :probe_log "%FALLBACK_LOG%"
  if defined LOGOK (
    >>"%FALLBACK_LOG%" echo [%DATE% %TIME%] NOTE: fell back to this file -- the per-day log verify_ibkr_nightly_%TODAY%.log could not be written.
  ) else (
    echo [run_verify_ibkr_nightly.cmd] WARNING: fallback log "%FALLBACK_LOG%" also unwritable; launching python with NO file logging.
    set "LOGFILE="
  )
)

REM Redirection-FIRST form (>>"file" echo ...) so a trailing digit in the message
REM can never be misparsed by cmd as a stream-handle redirection.
if defined LOGFILE (
  >>"%LOGFILE%" echo ============================================================
  >>"%LOGFILE%" echo [%DATE% %TIME%] run_verify_ibkr_nightly.cmd START base_py=%BASE_PY%
)

REM --- MANDATORY LAUNCH. Both branches run python; neither depends on logging. ---
if not exist "%BASE_PY%" (
  echo [run_verify_ibkr_nightly.cmd] ERROR: interpreter not found: "%BASE_PY%" -- python NOT launched.
  if defined LOGFILE >>"%LOGFILE%" echo [%DATE% %TIME%] run_verify_ibkr_nightly.cmd ERROR interpreter not found: %BASE_PY% -- python NOT launched.
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
>>"%LOGFILE%" 2>&1 ( set "RAN=1" & "%BASE_PY%" -u "%REPO%\datacollector\verify_ibkr_nightly.py" & call set "RC=%%ERRORLEVEL%%" )
if defined RAN goto :launched
echo [run_verify_ibkr_nightly.cmd] WARNING: log redirect failed at launch time; relaunching with NO file logging so python still starts.
set "LOGFILE="

:launch_nolog
set "RAN=1"
"%BASE_PY%" -u "%REPO%\datacollector\verify_ibkr_nightly.py"
set "RC=%ERRORLEVEL%"

:launched
if not defined RAN (
  echo [run_verify_ibkr_nightly.cmd] ERROR: python was NOT launched.
  endlocal & exit /b 92
)

if defined LOGFILE >>"%LOGFILE%" echo [%DATE% %TIME%] run_verify_ibkr_nightly.cmd EXIT rc=%RC%
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
