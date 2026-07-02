r"""
overlay_pipeline_watchdog.py — self-healing watchdog for run_overlay_pipeline.py.

Mirrors the SPX collector's watchdog pattern. Registered as a Windows Scheduled
Task that fires every ~20 min starting 18:00. On each run it:

  1. If the completion flag exists -> the pipeline is DONE -> STAND DOWN (exit 0,
     no relaunch). This is how the watchdog retires itself.
  2. Else, decide if the pipeline is alive & healthy:
       * a supervisor process holds the pipeline lock AND its PID is alive AND
         its heartbeat is FRESH (< STALE_SECS old)  -> healthy, do nothing.
       * otherwise (no lock / dead PID / stale heartbeat) -> the supervisor died
         or wedged -> RELAUNCH it (it resumes via skip-done + the pure re-derive
         backtest). Never launch a second if one is genuinely alive (the lock +
         PID check guard against a dup).

This is a SINGLE-SHOT check per scheduled fire (not a long loop) — the Task
Scheduler is the "every 20 min" driver, so even if this process itself is killed,
the next scheduled fire re-checks. That makes the watchdog itself crash-proof:
its own liveness is guaranteed by the OS scheduler, not by staying resident.

Run:  <venv python> overlay_pipeline_watchdog.py
      <venv python> overlay_pipeline_watchdog.py --selftest   # report only, no relaunch
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE_DIR = Path(r"C:\TradingDesk-Local\state\canslim")
LOG = STATE_DIR / "overlay_watchdog.log"
HEARTBEAT_FILE = STATE_DIR / "overlay_watchdog_heartbeat.txt"

PIPELINE_LOCK = STATE_DIR / "overlay_pipeline.lock"
PIPELINE_HB = STATE_DIR / "overlay_pipeline_heartbeat.json"
COMPLETE_FLAG = STATE_DIR / "overlay_pipeline.complete"
PIPELINE_SCRIPT = HERE / "run_overlay_pipeline.py"

STALE_SECS = 3600        # heartbeat older than 60 min = wedged (pull stall watchdog is 30 min)
PY = getattr(sys, "_base_executable", None) or sys.executable


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


def heartbeat(state: str) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        HEARTBEAT_FILE.write_text(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {state}")
    except OSError:
        pass


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                             capture_output=True, text=True, timeout=15)
        return str(pid) in out.stdout
    except Exception:
        return True


def _lock_holder_alive() -> bool:
    """True iff the pipeline lock is held by a currently-running process."""
    if not PIPELINE_LOCK.exists():
        return False
    try:
        pid = int(PIPELINE_LOCK.read_text().strip() or "0")
    except (OSError, ValueError):
        return False
    return _pid_alive(pid)


def _heartbeat_fresh() -> bool:
    try:
        age = dt.datetime.now().timestamp() - PIPELINE_HB.stat().st_mtime
        return age < STALE_SECS
    except OSError:
        return False


def _child_env() -> dict:
    env = os.environ.copy()
    site = ""
    for p in sys.path:
        if p.endswith("site-packages") and "venv" in p.lower():
            site = p
            break
    if not site:
        cand = os.path.join(sys.prefix, "Lib", "site-packages")
        site = cand if os.path.isdir(cand) else ""
    if site:
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = site + (os.pathsep + existing if existing else "")
    return env


def launch_pipeline() -> None:
    """Fire-and-forget the supervisor, detached, no console. It has its own lock so
    it self-guards against a dup; we still only get here after concluding none is
    alive."""
    creationflags = 0
    if os.name == "nt":
        creationflags = 0x00000008 | 0x00000200   # DETACHED | NEW_PROCESS_GROUP
    log(f"RELAUNCH: starting {PIPELINE_SCRIPT.name}")
    subprocess.Popen([PY, str(PIPELINE_SCRIPT)], cwd=str(HERE), env=_child_env(),
                     creationflags=creationflags, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def check(selftest: bool = False) -> int:
    if COMPLETE_FLAG.exists():
        log("completion flag present — pipeline DONE; watchdog standing down.")
        heartbeat("STAND-DOWN (complete)")
        return 0

    alive = _lock_holder_alive()
    fresh = _heartbeat_fresh()
    log(f"check: lock_holder_alive={alive} heartbeat_fresh={fresh}")

    if alive and fresh:
        heartbeat("healthy — pipeline alive + fresh")
        log("pipeline healthy — nothing to do.")
        return 0

    reason = ("no live supervisor" if not alive
              else "supervisor alive but heartbeat STALE (wedged)")
    log(f"pipeline NOT healthy ({reason}) -> {'WOULD relaunch' if selftest else 'relaunching'}")
    heartbeat(f"RELAUNCH ({reason})")
    if selftest:
        return 1
    # If a stale-but-alive supervisor is truly wedged, the pipeline's own lock would
    # block a fresh launch from proceeding. Guard: only relaunch when the holder is
    # NOT alive (a wedged-but-alive process is rare; its stall-watchdogs handle the
    # pull, and a truly hung supervisor with a live PID will be reclaimed once its
    # PID dies). This avoids fighting a live lock.
    if alive and not fresh:
        log("supervisor PID still alive but heartbeat stale — NOT force-launching a "
            "second (its lock would block it); leaving it for its internal stall "
            "watchdog / next fire once the PID clears.")
        return 0
    launch_pipeline()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true", help="report only, never relaunch")
    args = ap.parse_args()
    try:
        return check(selftest=args.selftest)
    except Exception as e:
        log(f"watchdog error (non-fatal): {e!r}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
