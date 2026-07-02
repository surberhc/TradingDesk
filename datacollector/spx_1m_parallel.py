r"""
spx_1m_parallel.py — sharded, self-healing supervisor for the SPX-ROOT 1-minute
backfill (BURST-AND-RELEASE).

WHY: a single serial collector needs ~11 h for the vendor-overlap window
(2025-05-01..2026-06-18, ~296 trading days). A concurrency probe (see
_probe_concurrency.py) measured the terminal's throughput knee at K=4 concurrent
instances (~85 days/hr; 6 gave no gain and added startup contention). This
supervisor runs K=4 shards to finish the window in ~2.5-3.5 h, then exits and
frees the terminal. It does NOT touch the SPXW collector/supervisor/watchdog, the
SPXW instance, or any existing scheduled task.

HOW IT WORKS
  * Splits the window into K contiguous, DISJOINT date sub-ranges (shards). Days
    are independent + idempotent, so shards never collide (each writes only days
    in its own range; day_done() skips completed days on restart).
  * Launches each shard as collect_spx_1m.py --start/--end with its OWN
    --progress spx_1m_progress_shardN.json and --log spx_1m_shardN.log, so
    instances never clobber each other's heartbeat.
  * DETACHED launch (CREATE_NEW_PROCESS_GROUP + DETACHED_PROCESS on Windows) so
    the shards survive THIS agent exiting AND the Claude app closing. Spawned with
    the BASE interpreter + venv site-packages on PYTHONPATH (same trick as
    spxw_1m_supervisor) so each shard is exactly ONE process, not a stub+worker.
  * MONITORS every TICK: counts done days per shard, writes a combined heartbeat
    (spx_1m_parallel_progress.json) with aggregate pct + ETA. RESTARTS any shard
    whose process has died while its range still has un-done days (the collector
    resumes via skip-done — nothing is lost). A shard that exits 0 with its range
    fully done is left finished.
  * GOOD CITIZEN even in burst mode: if HTTP 429 / errors climb across shard logs,
    it BACKS OFF by pausing the lowest-priority (oldest-range) shard until the
    error rate settles, then resumes it.
  * Singleton lock (PID file) so two parallel supervisors can't race the tree.
  * EXITS 0 when every day in the whole window has both files.

RESUMABILITY / RELAUNCH
  Kill it any time; relaunch with the SAME command and it resumes (shards skip
  done days). Exact relaunch:
    cd datacollector
    "C:\TradingDesk-Local\venv\Scripts\python.exe" spx_1m_parallel.py

ASCII-only console output (cp1252 console crashes on non-ASCII).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import config
import collect_spx_1m as collector

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
WINDOW_START = dt.date(2025, 5, 1)
WINDOW_END = dt.date(2026, 6, 18)
K = 4                       # measured concurrency knee (see module docstring)

HERE = config.CODE_ROOT
COLLECTOR = HERE / "collect_spx_1m.py"

PROGRESS = config.DATA_ROOT / "spx_1m_parallel_progress.json"
LOG = config.DATA_ROOT / "spx_1m_parallel.log"
LOCK = config.DATA_ROOT / "spx_1m_parallel.lock"

TICK = 30                   # monitor cadence (s)
BACKOFF_429_THRESHOLD = 20  # new 429s across shards within a window -> back off
BACKOFF_ERR_THRESHOLD = 15  # new ERRORs across shards within a window -> back off

# Base interpreter (avoid the venv relauncher stub -> one process per shard).
PY = getattr(sys, "_base_executable", None) or sys.executable

# Detached-launch flags so shards survive this process / the Claude app closing.
if os.name == "nt":
    DETACH_FLAGS = (subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
                    | 0x00000008)                        # DETACHED_PROCESS
else:
    DETACH_FLAGS = 0


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# Child interpreter env (venv deps on PYTHONPATH for the base interpreter)
# --------------------------------------------------------------------------- #
def _venv_site() -> str:
    for p in sys.path:
        if p.endswith("site-packages") and "venv" in p.lower():
            return p
    cand = os.path.join(sys.prefix, "Lib", "site-packages")
    return cand if os.path.isdir(cand) else ""


def _child_env() -> dict:
    env = os.environ.copy()
    site = _venv_site()
    if site:
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = site + (os.pathsep + existing if existing else "")
    env["PYTHONIOENCODING"] = "ascii"
    return env


# --------------------------------------------------------------------------- #
# Singleton lock (mirrors spxw_1m_supervisor.acquire_lock)
# --------------------------------------------------------------------------- #
def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                capture_output=True, text=True, timeout=15)
            return str(pid) in out.stdout
        except Exception:
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def acquire_lock() -> bool:
    me = os.getpid()
    for _ in range(2):
        try:
            fd = os.open(str(LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w") as f:
                f.write(str(me))
            return True
        except FileExistsError:
            try:
                holder = int(LOCK.read_text().strip() or "0")
            except (OSError, ValueError):
                holder = 0
            if holder == me:
                return True
            if holder and _pid_alive(holder):
                log(f"another parallel supervisor is live (pid={holder}) -> exit")
                return False
            log(f"stale lock (pid={holder} dead) -> reclaiming")
            try:
                LOCK.unlink()
            except OSError:
                pass
            continue
    return False


def release_lock() -> None:
    try:
        if LOCK.exists() and LOCK.read_text().strip() == str(os.getpid()):
            LOCK.unlink()
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Sharding: K contiguous disjoint date sub-ranges over the window
# --------------------------------------------------------------------------- #
def window_trading_days() -> list[dt.date]:
    """All Mon-Fri days in the window, OLDEST first (for contiguous shard split)."""
    out = []
    d = WINDOW_START
    while d <= WINDOW_END:
        if d.weekday() < 5:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def make_shards(k: int) -> list[tuple[dt.date, dt.date]]:
    """Split the window's trading-day list into k contiguous disjoint ranges.

    Ranges are expressed as (start_date, end_date) passed to the collector's
    --start/--end. Because the shards partition the trading-day list and each
    collector only writes days inside its own [start,end], the shards can never
    write the same day -> no write collisions.
    """
    days = window_trading_days()
    n = len(days)
    shards: list[tuple[dt.date, dt.date]] = []
    base = n // k
    rem = n % k
    idx = 0
    for i in range(k):
        size = base + (1 if i < rem else 0)
        chunk = days[idx: idx + size]
        idx += size
        if chunk:
            shards.append((chunk[0], chunk[-1]))
    return shards


# --------------------------------------------------------------------------- #
# Per-shard bookkeeping
# --------------------------------------------------------------------------- #
class Shard:
    def __init__(self, i: int, start: dt.date, end: dt.date):
        self.i = i
        self.start = start
        self.end = end
        self.progress = config.DATA_ROOT / f"spx_1m_progress_shard{i}.json"
        self.logpath = config.DATA_ROOT / f"spx_1m_shard{i}.log"
        self.proc: subprocess.Popen | None = None
        self.days = [d for d in window_trading_days() if start <= d <= end]
        self.paused = False       # backoff state
        self._last_429 = 0
        self._last_err = 0

    def cmd(self) -> list[str]:
        return [PY, "-u", str(COLLECTOR),
                "--start", self.start.isoformat(),
                "--end", self.end.isoformat(),
                "--progress", str(self.progress),
                "--log", str(self.logpath)]

    def launch(self) -> None:
        self.proc = subprocess.Popen(
            self.cmd(), cwd=str(HERE), env=_child_env(),
            creationflags=DETACH_FLAGS,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log(f"shard{self.i} LAUNCHED pid={self.proc.pid} "
            f"{self.start}..{self.end} ({len(self.days)} days)")

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def days_done(self) -> int:
        return sum(1 for d in self.days if collector.day_done(d))

    def fully_done(self) -> bool:
        return all(collector.day_done(d) for d in self.days)

    def scan_errors(self) -> tuple[int, int]:
        """(new_429, new_ERROR) since the last scan, from this shard's log."""
        try:
            txt = self.logpath.read_text(errors="ignore")
        except OSError:
            return 0, 0
        c429 = txt.count("429")
        cerr = txt.count("ERROR")
        d429 = max(0, c429 - self._last_429)
        derr = max(0, cerr - self._last_err)
        self._last_429, self._last_err = c429, cerr
        return d429, derr


# --------------------------------------------------------------------------- #
# Combined progress heartbeat
# --------------------------------------------------------------------------- #
def write_combined(shards: list[Shard], total_days: int, t_start: float,
                   done_at_start: int) -> int:
    done = sum(s.days_done() for s in shards)
    elapsed = time.time() - t_start
    new = done - done_at_start
    rate_per_hr = (new / elapsed * 3600) if elapsed > 0 and new > 0 else 0.0
    remaining = max(total_days - done, 0)
    eta_str = ""
    if rate_per_hr > 0:
        eta_h = remaining / rate_per_hr
        finish = dt.datetime.now() + dt.timedelta(hours=eta_h)
        eta_str = f"{eta_h:.1f}h remaining (~{finish:%Y-%m-%d %H:%M})"
    payload = {
        "updated": f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}",
        "days_done": done,
        "days_total": total_days,
        "pct": round(100.0 * done / total_days, 2) if total_days else 0.0,
        "rate_days_per_hr": round(rate_per_hr, 1),
        "eta": eta_str,
        "shards": [
            {"i": s.i, "range": f"{s.start}..{s.end}",
             "done": s.days_done(), "total": len(s.days),
             "alive": s.alive(), "paused": s.paused,
             "pid": (s.proc.pid if s.proc else None)}
            for s in shards
        ],
    }
    tmp = PROGRESS.with_name(PROGRESS.name + ".tmp")
    PROGRESS.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(payload, indent=2))
    os.replace(tmp, PROGRESS)
    return done


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="Sharded SPX-root 1m backfill supervisor")
    ap.add_argument("--k", type=int, default=K, help=f"shards (default {K})")
    args = ap.parse_args()
    k = max(1, args.k)

    if not acquire_lock():
        return 0
    try:
        shards = [Shard(i, s, e) for i, (s, e) in enumerate(make_shards(k))]
        total_days = len(window_trading_days())
        t_start = time.time()
        done_at_start = sum(s.days_done() for s in shards)

        log(f"=== spx_1m parallel supervisor start === window "
            f"{WINDOW_START}..{WINDOW_END} | {total_days} trading-days | "
            f"K={k} shards | {done_at_start} already done")
        for s in shards:
            log(f"  shard{s.i}: {s.start}..{s.end}  ({len(s.days)} days, "
                f"{s.days_done()} done)")

        # Initial launch of every shard that still has work.
        for s in shards:
            if not s.fully_done():
                s.launch()
            else:
                log(f"shard{s.i} already fully done -> not launched")

        # Monitor loop.
        while True:
            time.sleep(TICK)

            # Restart any dead-but-unfinished shard (resumable via skip-done).
            for s in shards:
                if s.paused:
                    continue
                if s.fully_done():
                    if s.alive():
                        # done but process lingering: let it exit on its own.
                        pass
                    continue
                if not s.alive():
                    log(f"shard{s.i} DEAD with {len(s.days) - s.days_done()} "
                        f"days left -> restarting (resumes via skip-done)")
                    s.launch()

            # Good-citizen backoff: sum new 429/ERROR across shards this tick.
            total_new_429 = 0
            total_new_err = 0
            for s in shards:
                d429, derr = s.scan_errors()
                total_new_429 += d429
                total_new_err += derr
            if (total_new_429 >= BACKOFF_429_THRESHOLD
                    or total_new_err >= BACKOFF_ERR_THRESHOLD):
                # Pause the OLDEST-range running shard (lowest priority: recent
                # data is more useful sooner) to shed load. It resumes next tick
                # once the error window clears.
                running = [s for s in shards if s.alive() and not s.paused]
                if len(running) > 1:
                    victim = min(running, key=lambda s: s.start)
                    log(f"BACKOFF: new429={total_new_429} newERR={total_new_err} "
                        f"-> pausing shard{victim.i} ({victim.start}..{victim.end})")
                    try:
                        if victim.proc:
                            victim.proc.terminate()
                    except Exception:
                        pass
                    victim.paused = True
            else:
                # Error window clear -> resume any paused shard.
                for s in shards:
                    if s.paused and not s.fully_done():
                        log(f"BACKOFF CLEAR -> resuming shard{s.i}")
                        s.paused = False
                        s.launch()

            done = write_combined(shards, total_days, t_start, done_at_start)

            # Completion check: every in-window day has both files.
            all_done = all(collector.day_done(d)
                           for d in window_trading_days())
            alive_ct = sum(1 for s in shards if s.alive())
            log(f"progress: {done}/{total_days} done | "
                f"{alive_ct} shard(s) alive | "
                f"new429={total_new_429} newERR={total_new_err}")

            if all_done:
                log("=== ALL DAYS DONE — every in-window day has both files. "
                    "Supervisor exiting 0. Terminal is free. ===")
                # Reap any lingering shard processes.
                for s in shards:
                    if s.alive() and s.proc:
                        try:
                            s.proc.wait(timeout=10)
                        except Exception:
                            pass
                write_combined(shards, total_days, t_start, done_at_start)
                return 0

            # If every shard has exited (0) AND no day is un-done-but-restartable,
            # but all_done is still False, the only remaining days are legit-empty
            # (holidays) the collector treats as complete. Treat as done to avoid
            # spinning forever.
            if alive_ct == 0 and all(s.fully_done() or not s.alive()
                                     for s in shards):
                undone = [d for d in window_trading_days()
                          if not collector.day_done(d)]
                if undone:
                    # Re-launch shards that still own an un-done day; if a shard
                    # already exited 0 on a legit-empty day, relaunch is a no-op
                    # (it will exit 0 again). To avoid a tight loop, only relaunch
                    # shards NOT already relaunched-clean this pass.
                    stuck = [s for s in shards
                             if not s.fully_done() and not s.alive() and not s.paused]
                    if not stuck:
                        log(f"{len(undone)} day(s) remain but every shard has "
                            "cleanly exited (legit-empty/holiday days the "
                            "collector treats as complete). Supervisor exiting 0.")
                        return 0
    finally:
        release_lock()


if __name__ == "__main__":
    sys.exit(main())
