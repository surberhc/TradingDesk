@echo off
REM ===========================================================================
REM  run_repo_backup_wrap.cmd - the `wrap` backup of the C:\TradingDesk repo.
REM
REM  Launcher for CLAUDE.md's `wrap` force-word: the session runs this AFTER the
REM  conductor render/commit, so the commit it just made is inside the bundle.
REM  Interactive by design -- Andrew is waiting on it -- so it prints progress and
REM  ends with ONE line of compact JSON the session reports the outcome from.
REM
REM  THIS IS THE PRIMARY TRIGGER, NOT A REPLACEMENT. run_repo_backup.cmd
REM  (RepoBackupDaily, 20:00 + AtLogon) STAYS: Andrew does not always wrap, and its
REM  daily cadence is what feeds heartbeat_alarm.py's 26h staleness check. Wrap-only
REM  would go silent every quiet weekend and page for a backup that was never
REM  missing -- which is how an alarm gets trained into noise.
REM
REM  EXIT CODES MATTER HERE, do not swallow them -- same contract as the scheduled
REM  path, --wrap changes the OUTPUT and nothing else:
REM    0 = a bundle verified okay-with-complete-history, is on the real Drive volume,
REM        re-verified there, records the current HEAD, and the heartbeat was
REM        refreshed. (This includes a run that created NO new bundle because HEAD
REM        had not moved -- it re-proved all of the above about the existing one.
REM        The JSON's "state" says which; "proves" names the bundle.)
REM    non-0 = FAILED. The heartbeat is deliberately left cold so heartbeat_alarm.py
REM        (job "repo_backup") pages on the silence.
REM
REM  This job is READ-ONLY with respect to git: it only runs bundle
REM  create/verify/list-heads, rev-parse and rev-list. It never commits, stages,
REM  resets, or cleans -- committing is the session's job, not the backup's.
REM ===========================================================================

set "VENV_PY=C:\TradingDesk-Local\venv\Scripts\python.exe"

"%VENV_PY%" "%~dp0repo_backup.py" --wrap
exit /b %ERRORLEVEL%
