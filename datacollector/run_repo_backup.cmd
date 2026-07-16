@echo off
REM ===========================================================================
REM  run_repo_backup.cmd - verified git-bundle backup of the C:\TradingDesk repo.
REM
REM  Launcher for the RepoBackupDaily scheduled task (register with
REM  register_repo_backup_task.ps1 -- Andrew runs that himself; this build
REM  deliberately does NOT register any task).
REM
REM  EXIT CODES MATTER HERE, do not swallow them:
REM    0 = a bundle verified okay-with-complete-history, landed on the real Drive
REM        volume, re-verified there, and the heartbeat was refreshed.
REM    non-0 = FAILED. The heartbeat is deliberately left cold so
REM        heartbeat_alarm.py (job "repo_backup") pages on the silence. Task
REM        Scheduler will also show the failure as the task's Last Result.
REM
REM  This job is READ-ONLY with respect to git: it only runs bundle create/verify,
REM  rev-parse and rev-list. It never commits, stages, resets, or cleans.
REM ===========================================================================

set "VENV_PY=C:\TradingDesk-Local\venv\Scripts\python.exe"

"%VENV_PY%" "%~dp0repo_backup.py"
exit /b %ERRORLEVEL%
