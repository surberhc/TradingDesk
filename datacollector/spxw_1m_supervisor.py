"""
spxw_1m_supervisor.py — self-healing runner for the SPXW 1-minute collector.

Mirrors supervisor.py. Launched by Windows Task Scheduler (run_spxw_1m.bat) so it
is INDEPENDENT of any Claude session — it survives the app closing, the session
dropping, and a reboot+logon. On each cycle it:

  1. Confirms the local ThetaData Terminal is answering (the collector itself
     also waits/retries, but a quick gate here avoids a tight crash loop).
  2. Runs collect_spxw_1m.py (resumable) and WATCHDOGS it: if no new parquet
     appears in the options_1m tree for STALL_SECS (a stall — dead terminal, hung
     request) it kills and restarts the cycle. The collector resumes via its
     skip-done logic, so a kill mid-day just re-pulls that one day.
  3. Restarts on crash/non-zero exit; EXITS cleanly (0) when the collector
     reports the full window is done.

Singleton guard via heartbeat file freshness so two supervisors don't race the
same tree (which could tear parquet writes).

Logs to spxw_1m_supervisor.log; updates spxw_1m_supervisor_heartbeat.txt every 30s.
"""

from __future__ import annotations

import datetime as dt
import subprocess
import sys
import time

import config
import collect_spxw_1m as collector

LOG = config.DATA_ROOT / "spxw_1m_supervisor.log"
HEARTBEAT = config.DATA_ROOT / "spxw_1m_supervisor_heartbeat.txt"
PY = sys.executable
HERE = config.CODE_ROOT
COLLECTOR = HERE / "collect_spxw_1m.py"

# OHLC day-pulls are ~3-5 min; the big QUOTE call alone is ~50s. A single hung
# expiration could stall for a couple minutes on retries. Give a generous window
# before declaring a stall — the terminal is also serving a GEX rebuild + another
# pull right now.
STALL_SECS = 1200          # 20 min with zero new files = stalled
TICK = 30


def log(msg: str) -> None:
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def file_count() -> int:
    """Number of completed parquet files in the 1-minute tree (quote + ohlc)."""
    try:
        return sum(1 for _ in collector.ROOT_1M.rglob("*.parquet"))
    except OSError:
        return 0


def run_collector() -> str:
    """Run the collector with a stall watchdog.
    -> 'done' (exit 0) | 'exited' (non-zero) | 'stalled'."""
    proc = subprocess.Popen([PY, "-u", str(COLLECTOR)], cwd=str(HERE))
    last_n, last_progress = file_count(), time.time()
    while True:
        time.sleep(TICK)
        n = file_count()
        try:
            HEARTBEAT.write_text(
                f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  files={n}  "
                f"collector_pid={proc.pid}")
        except OSError:
            pass
        if proc.poll() is not None:
            return "done" if proc.returncode == 0 else "exited"
        if n > last_n:
            last_n, last_progress = n, time.time()
        elif time.time() - last_progress > STALL_SECS:
            log(f"STALL: no new file for {STALL_SECS // 60} min -> "
                "killing & restarting (collector resumes via skip-done)")
            proc.terminate()
            time.sleep(5)
            if proc.poll() is None:
                proc.kill()
            return "stalled"


def main() -> None:
    # Singleton guard: a fresh heartbeat (<90s) means another supervisor is live.
    if HEARTBEAT.exists() and time.time() - HEARTBEAT.stat().st_mtime < 90:
        log("another spxw_1m supervisor appears active (fresh heartbeat) -> "
            "exiting to avoid a duplicate")
        return
    log("=== spxw_1m supervisor start ===")
    while True:
        try:
            if not collector.connected():
                log("terminal not answering — waiting 60s before cycle")
                collector.wait_for_terminal(max_wait=600)
                if not collector.connected():
                    log("terminal still down; retry whole cycle in 60s")
                    time.sleep(60)
                    continue
            result = run_collector()
            if result == "done":
                log("=== collector reports the full window DONE — supervisor done ===")
                break
            log(f"cycle ended ({result}); restarting in 15s")
            time.sleep(15)
        except Exception as e:        # never let the supervisor itself die
            log(f"supervisor error: {e!r}; retry in 30s")
            time.sleep(30)
    try:
        HEARTBEAT.write_text(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  COMPLETE")
    except OSError:
        pass
    log("spxw_1m supervisor exiting — collection complete")


if __name__ == "__main__":
    main()
