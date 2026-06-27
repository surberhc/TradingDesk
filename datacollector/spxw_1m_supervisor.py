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
import os
import subprocess
import sys
import time

import config
import collect_spxw_1m as collector

LOG = config.DATA_ROOT / "spxw_1m_supervisor.log"
HEARTBEAT = config.DATA_ROOT / "spxw_1m_supervisor_heartbeat.txt"
LOCK = config.DATA_ROOT / "spxw_1m_supervisor.lock"
HERE = config.CODE_ROOT
COLLECTOR = HERE / "collect_spxw_1m.py"

# --- Interpreter selection (avoid the Windows venv "relauncher stub") --------
# This venv's Scripts\python.exe is a redirector stub: it does NOT run Python
# in-process, it re-launches the base interpreter as a CHILD. So spawning the
# collector via sys.executable produced a stub+worker PAIR (two processes per
# launch) — the visible "duplicate". We instead spawn the collector with the
# REAL base interpreter (sys._base_executable) and put the venv's site-packages
# on PYTHONPATH so all venv deps (pandas/requests/...) still import. Result:
# exactly ONE collector process, no stub.
PY = getattr(sys, "_base_executable", None) or sys.executable


def _child_env() -> dict:
    """Env for spawned children: base interpreter + venv site-packages on path."""
    env = os.environ.copy()
    site = _venv_site()
    if site:
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = site + (os.pathsep + existing if existing else "")
    return env


def _venv_site() -> str:
    """Path to the venv's site-packages (so the base interpreter can use deps)."""
    try:
        for p in sys.path:
            if p.endswith("site-packages") and "venv" in p.lower():
                return p
    except Exception:
        pass
    # Fallback: derive from sys.prefix (the venv root when running in the venv).
    cand = os.path.join(sys.prefix, "Lib", "site-packages")
    return cand if os.path.isdir(cand) else ""

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


def _pid_alive(pid: int) -> bool:
    """True if a process with this PID is currently running (Windows + POSIX)."""
    if pid <= 0:
        return False
    if os.name == "nt":
        # tasklist is the dependency-free way to test liveness on Windows.
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                capture_output=True, text=True, timeout=15,
            )
            return str(pid) in out.stdout
        except Exception:
            # If we can't tell, assume alive — safer to refuse to start a dup
            # than to risk two supervisors racing the tree.
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def acquire_lock() -> bool:
    """Atomically acquire the singleton lock.

    Robust replacement for the old heartbeat-mtime guard, which was racy at
    startup (two near-simultaneous starts both saw a stale heartbeat and both
    proceeded) and self-blocking after a kill (a just-killed supervisor left a
    fresh heartbeat, so the next start aborted).

    Strategy: create the lock file with O_CREAT|O_EXCL (atomic — only one of N
    racing starts can win). If it already exists, read the recorded PID and
    check whether that process is actually alive: if alive, another supervisor
    owns it -> refuse. If dead/garbage (stale lock from a crash or kill), take
    it over. Returns True if we now hold the lock, False otherwise.
    """
    me = os.getpid()
    for attempt in range(2):
        try:
            fd = os.open(str(LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w") as f:
                f.write(str(me))
            return True
        except FileExistsError:
            # Someone holds (or held) the lock. Inspect the recorded PID.
            try:
                holder = int(LOCK.read_text().strip() or "0")
            except (OSError, ValueError):
                holder = 0
            if holder == me:
                return True
            if holder and _pid_alive(holder):
                log(f"another spxw_1m supervisor is live (pid={holder}, "
                    f"lock={LOCK.name}) -> exiting to avoid a duplicate")
                return False
            # Stale lock (holder PID not running) -> take it over and retry.
            log(f"stale lock found (pid={holder} not running) -> reclaiming")
            try:
                LOCK.unlink()
            except OSError:
                pass
            continue
    return False


def release_lock() -> None:
    """Release the lock iff we still own it."""
    try:
        if LOCK.exists() and LOCK.read_text().strip() == str(os.getpid()):
            LOCK.unlink()
    except OSError:
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
    proc = subprocess.Popen([PY, "-u", str(COLLECTOR)], cwd=str(HERE),
                            env=_child_env())
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
    # Singleton guard: an exclusive PID lock file. Atomic at startup (only one of
    # N racing launches wins O_CREAT|O_EXCL) and self-healing after a kill (a
    # stale lock whose PID is dead gets reclaimed). At most ONE supervisor runs.
    if not acquire_lock():
        return
    try:
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
    finally:
        release_lock()


if __name__ == "__main__":
    main()
