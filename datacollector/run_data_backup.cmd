@echo off
REM ===========================================================================
REM  run_data_backup.cmd - verified rclone backup of C:\TradingDesk-Local (the
REM  ~99 GB / ~464k-file irreplaceable market-data warehouse) to Google Drive.
REM
REM  Launcher for the DataBackupDaily scheduled task (register with
REM  register_data_backup_task.ps1 -- Andrew runs that himself; this build
REM  deliberately does NOT register any task).
REM
REM  EXIT CODES MATTER HERE, do not swallow them:
REM    0 = rclone copy completed AND rclone check verified the remote copy is
REM        byte-identical (md5) with 0 differences / 0 errors, and the heartbeat
REM        was refreshed.
REM    non-0 = FAILED (rclone missing, copy error, or ANY checksum difference/
REM        error). The heartbeat is deliberately left cold so heartbeat_alarm.py
REM        (job "data_backup") pages on the silence. Task Scheduler will also show
REM        the failure as the task's Last Result.
REM
REM  This job only READS local files: `rclone copy` reads C:\TradingDesk-Local and
REM  writes to the REMOTE. It never deletes or modifies anything under
REM  C:\TradingDesk-Local, and it uses `copy` (additive) NOT `sync`, so a local
REM  deletion can never propagate to and nuke the backup copy.
REM ===========================================================================

set "VENV_PY=C:\TradingDesk-Local\venv\Scripts\python.exe"

"%VENV_PY%" "%~dp0data_backup.py"
exit /b %ERRORLEVEL%
