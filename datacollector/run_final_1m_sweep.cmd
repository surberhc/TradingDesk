@echo off
REM ===========================================================================
REM  run_final_1m_sweep.cmd - one-time FINAL 1-minute option sweep before the
REM  ThetaData subscription lapses 2026-07-25.
REM
REM  Fired once by the ThetaFinal1mSweep_0724 scheduled task (2026-07-24 18:00,
REM  InteractiveToken so it can reach the user-session ThetaData Terminal). Runs
REM  the SPXW then the SPX 1-minute collectors over the tail window
REM  2026-07-21..2026-07-24. Each collector is resumable/idempotent (a day with
REM  both non-empty quote+ohlc files is skipped), so re-running is safe.
REM
REM  NOTE (measured): the collectors exclude "today/future" (d < date.today()),
REM  so a run ON 2026-07-24 collects 07-21/07-22/07-23 but NOT 07-24 itself.
REM
REM  Isolated --progress/--log per collector (scratchpad bf dir) so this one-time
REM  sweep never clobbers the primary collectors' heartbeat/status files.
REM ===========================================================================

set "VENV=C:\TradingDesk-Local\venv\Scripts\python.exe"
set "BF=C:\Users\andre\AppData\Local\Temp\claude\C--TradingDesk\bce124f5-270d-4137-ae19-5370ce11178b\scratchpad\bf"

cd /d C:\TradingDesk\datacollector

if not exist "%BF%" mkdir "%BF%"

echo [%date% %time%] === final 1m sweep: SPXW ===
"%VENV%" collect_spxw_1m.py --start 2026-07-21 --end 2026-07-24 --progress "%BF%\final_spxw_progress.json" --log "%BF%\final_spxw.log"
echo [%date% %time%] SPXW collector exit=%ERRORLEVEL%

echo [%date% %time%] === final 1m sweep: SPX ===
"%VENV%" collect_spx_1m.py --start 2026-07-21 --end 2026-07-24 --progress "%BF%\final_spx_progress.json" --log "%BF%\final_spx.log"
echo [%date% %time%] SPX collector exit=%ERRORLEVEL%

exit /b 0
