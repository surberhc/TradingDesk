@echo off
setlocal enableextensions
REM ===========================================================================
REM  run_s8_teardown.cmd - SESSION TEARDOWN for the S8 live pilot.
REM  Runs livebot\s8_reap.py for BOTH pilot processes after the 15:00 CT close.
REM
REM  WHY: Stop-ScheduledTask kills the .cmd wrapper but not its python child. The
REM  pre-launch reap in run_s8_service.cmd / run_s8_collector.cmd covers every case
REM  where a NEW session starts -- but NOT the "stopped, and never restarted" case,
REM  where an orphan would sit holding a readonly gateway connection, its clientId
REM  and the day log's handle indefinitely. Nothing previously reaped that. This
REM  task does.
REM
REM  Normally a NO-OP: by 15:05 the service has already self-exited cleanly and
REM  released its own lock, so there is nothing to reap.
REM
REM  ZERO-TRANSMIT: s8_reap has no order path, no IB import, and no knowledge of
REM  strategy or PILOT_MODE. It only terminates a cmdline-VERIFIED s8_service /
REM  s8_collector python and clears its stale lock file.
REM
REM  Registered as the scheduled task "S8SessionTeardown" (15:05 CT, Mon-Fri).
REM  Output goes to the reaper's OWN log (s8_pilot\logs\s8_reap.log), never a day log.
REM ===========================================================================

set "VENV_PY=C:\TradingDesk-Local\venv\Scripts\python.exe"
set "REPO=%~dp0.."

REM --- Resolve the real base interpreter + venv site-packages (avoid the stub) ---
for /f "usebackq delims=" %%i in (`%VENV_PY% -c "import sys;print(getattr(sys,'_base_executable',None) or sys.executable)"`) do set "BASE_PY=%%i"
for /f "usebackq delims=" %%i in (`%VENV_PY% -c "import sys;print(next((p for p in sys.path if p.endswith('site-packages') and 'venv' in p.lower()), ''))"`) do set "VENV_SITE=%%i"

if not defined BASE_PY set "BASE_PY=%VENV_PY%"

set "PYTHONPATH=%VENV_SITE%;%REPO%\connections;%REPO%\strategies;%REPO%\paperbot;%PYTHONPATH%"

if not exist "%BASE_PY%" (
  echo [run_s8_teardown.cmd] ERROR: interpreter not found: "%BASE_PY%" -- teardown NOT run.
  endlocal & exit /b 91
)

"%BASE_PY%" -u "%REPO%\livebot\s8_reap.py" --all
endlocal & exit /b %ERRORLEVEL%
