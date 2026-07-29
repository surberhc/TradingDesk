@echo off
REM Durable event-log scan for the rebuilt Trading Desk dashboard (READ-ONLY).
REM Runs the eventlog parsers and records new events idempotently into
REM C:\TradingDesk-Local\state\desk_dashboard\events.db so history accumulates
REM permanently even when nobody has the dashboard open. Writes ONLY events.db,
REM never any trading store/warehouse/config. Registered as Scheduled Task
REM "DeskEventLogScan" (every 20 minutes).
cd /d "%~dp0"
"C:\TradingDesk-Local\venv\Scripts\python.exe" -c "import sys; from pathlib import Path; REPO=Path('.').resolve().parents[1]; [sys.path.insert(0,str(REPO/s)) for s in ('paperbot','backtester','connections','strategies','dailyreport','livebot')]; sys.path.insert(0,'.'); import eventlog; n=eventlog.scan(); print('event scan added', n, 'new events')"
