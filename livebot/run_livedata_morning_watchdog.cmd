@echo off
setlocal enableextensions
REM ===========================================================================
REM  run_livedata_morning_watchdog.cmd - launcher for the LIVE-DATA (port 4001)
REM  weekly "still down" 2FA-tap nudge (livebot\livedata_morning_watchdog.py).
REM
REM  Registered as Windows Scheduled Task "LiveDataMorningStillDownAlarm_0845CT"
REM  (weekdays ~08:45 CT). ONE-SHOT: a single check that TCP-probes port 4001 and,
REM  if it is not serving on a trading morning, emails ONE nudge through the existing
REM  dailyreport mailer, then exits. The module's own trading-day guard skips
REM  weekends/holidays.
REM
REM  SAFE PRE-SEED: this task ONLY probes a port and may send email. It launches
REM  NOTHING and can never push a 2FA itself, so it is safe to run even before the
REM  first live-data login has been seeded. Runs as the normal user (no elevation).
REM
REM  ZERO-TRANSMIT: no order path; the live-data login is read-only. Logging is
REM  best-effort (off Drive); the launch is mandatory; exit with python's rc.
REM ===========================================================================

set "VENV_PY=C:\TradingDesk-Local\venv\Scripts\python.exe"
set "REPO=%~dp0.."
set "LOGDIR=C:\TradingDesk-Local\warehouse\logs"
set "LOG=%LOGDIR%\livedata_morning_watchdog.log"

REM --- PYTHONPATH: repo packages the module (lazily) needs. The module also inserts
REM     its own dir from __file__; this is belt-and-suspenders. ---
set "PYTHONPATH=%REPO%\livebot;%REPO%\connections;%REPO%\dailyreport;%PYTHONPATH%"

if not exist "%LOGDIR%" mkdir "%LOGDIR%" 2>nul
echo [%date% %time%] === livedata morning watchdog (probe 4001) ===>> "%LOG%"
"%VENV_PY%" "%REPO%\livebot\livedata_morning_watchdog.py" >> "%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
echo [%date% %time%] livedata_morning_watchdog exit=%RC%>> "%LOG%"
endlocal & exit /b %RC%
