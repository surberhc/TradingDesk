"""
supervisor.py — self-healing runner for the ThetaData grab.

The fix for the overnight death. The grab no longer rides on a fragile, session-tied
background job. This supervisor is launched by Windows Task Scheduler (run_supervisor.bat)
so it is INDEPENDENT of any Claude session — it survives the app closing, the session
dropping, and a reboot+login. On each cycle it:

  1. Ensures the Theta Terminal is up and actually SERVING data (restarts it if not —
     this recovers from the Terminal's data-farm drops, the thing that killed us).
  2. Runs download.py (resumable) and WATCHDOGS it: if no new files appear for 10 min
     (a stall — dead Terminal, hung request, etc.) it kills and restarts the cycle.
  3. Repeats until download.py completes the full root list on its own (exit 0).

It logs to C:\\TradingDesk-Local\\warehouse\\supervisor.log and updates supervisor_heartbeat.txt every
30s, so you (or the monitor) can confirm at a glance that it's alive and progressing.
"""

from __future__ import annotations

import datetime
import subprocess
import sys
import time

import config
import thetadata_client as td

LOG = config.DATA_ROOT / "supervisor.log"
HEARTBEAT = config.DATA_ROOT / "supervisor_heartbeat.txt"
PY = sys.executable
HERE = config.CODE_ROOT
STALL_SECS = 600            # 10 min with zero new files = stalled (exceeds max retry time)


def log(msg: str) -> None:
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def file_count() -> int:
    return sum(1 for _ in config.RAW_OPTIONS.rglob("*.parquet"))


def terminal_serving() -> bool:
    """Up AND actually returning data (status port can answer before auth completes)."""
    if not td.connected():
        return False
    try:
        return len(td.eod_greeks("SPY", "20260618", "20260618")) > 0
    except Exception:
        return False


def ensure_terminal() -> bool:
    if terminal_serving():
        return True
    log("Terminal not serving -> (re)starting it")
    subprocess.Popen([PY, str(HERE / "start_terminal.py")], cwd=str(HERE))
    for _ in range(48):                       # up to ~4 min to come up + auth
        time.sleep(5)
        if terminal_serving():
            log("Terminal serving again")
            return True
    log("Terminal failed to come up within 4 min")
    return False


def run_download() -> str:
    """Run download.py with a stall watchdog. -> 'done' | 'stalled' | 'exited'."""
    proc = subprocess.Popen([PY, "-u", str(HERE / "download.py")], cwd=str(HERE))
    last_n, last_progress = file_count(), time.time()
    while True:
        time.sleep(30)
        n = file_count()
        HEARTBEAT.write_text(
            f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}  files={n}  download_pid={proc.pid}")
        if proc.poll() is not None:
            return "done" if proc.returncode == 0 else "exited"
        if n > last_n:
            last_n, last_progress = n, time.time()
        elif time.time() - last_progress > STALL_SECS:
            log(f"STALL: no new files for {STALL_SECS // 60} min -> killing & restarting")
            proc.terminate()
            time.sleep(5)
            if proc.poll() is None:
                proc.kill()
            return "stalled"


def main() -> None:
    # Singleton guard: if another supervisor is actively updating the heartbeat
    # (within the last 90s), don't start a second one — two downloads racing the
    # same warehouse risks corrupt parquet writes.
    if HEARTBEAT.exists() and time.time() - HEARTBEAT.stat().st_mtime < 90:
        log("another supervisor appears active (fresh heartbeat) -> exiting to avoid a duplicate")
        return
    log("=== supervisor start ===")
    while True:
        try:
            if not ensure_terminal():
                log("retry whole cycle in 60s")
                time.sleep(60)
                continue
            result = run_download()
            if result == "done":
                log("=== download completed the full root list — DONE ===")
                break
            log(f"cycle ended ({result}); restarting in 15s")
            time.sleep(15)
        except Exception as e:                 # never let the supervisor itself die
            log(f"supervisor error: {e!r}; retry in 30s")
            time.sleep(30)
    HEARTBEAT.write_text(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}  COMPLETE")
    log("supervisor exiting — grab complete")


if __name__ == "__main__":
    main()
