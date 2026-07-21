r"""
backfill_watchdog.py — reboot/crash-resilient supervisor for the time-critical
ThetaData 1-minute + bulk-EOD tail backfill, then self-quiets.

WHY THIS EXISTS
---------------
The ThetaData Standard subscription lapses 2026-07-25; after that NO history is
recreatable. A manual backfill chain (collect_spxw_1m -> collect_spx_1m ->
backfill_bulk_tail_20260721) was running to fill the tail, but a REBOOT killed it
mid-run (2026-07-21). Nothing brought it back. This watchdog is that missing
supervisor: registered as Windows Scheduled Task `ThetaBackfillWatchdog`
(AtStartup + every 15 min), each tick is a fresh short-lived process that either
(a) sees the target coverage is complete and writes a DONE marker then goes quiet
forever, (b) sees a backfill already running and no-ops (this is the anti-stacking
guard — it MUST NOT start a second concurrent backfill on top of the manual one),
(c) sees the ThetaData terminal down and defers to ThetaTerminalWatchdog, or
(d) launches the resumable chain (each collector skips days already on disk).

Mirrors theta_terminal_watchdog.py's discipline: ONE check per invocation, the
scheduler supplies the cadence, main() NEVER raises and always exits 0 so a hiccup
can't wedge the task's cadence, decision logic is a pure injectable run_once(...).

TEMPORARY BY DESIGN. Once the DONE marker is written the watchdog is a no-op every
tick. Safe to delete the task after ThetaData lapses 2026-07-25.

PAPER / research infra only — reads data, places no orders, touches no account.

Run:  python backfill_watchdog.py        (registered task; via run_backfill_watchdog.bat)
"""

from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys

import config

# --------------------------------------------------------------------------- #
# Paths — all small state under LOCAL C:\TradingDesk-Local\state\backfill\
# (derived from DATA_ROOT's parent so the 2026-07-16 off-Drive move can't break it)
# --------------------------------------------------------------------------- #
LOCAL_ROOT = config.DATA_ROOT.parent                     # C:\TradingDesk-Local
STATE_DIR = LOCAL_ROOT / "state" / "backfill"
HEARTBEAT = STATE_DIR / "backfill_watchdog_heartbeat.txt"
DONE_MARKER = STATE_DIR / "backfill_1m_bulk.DONE"
LOG = STATE_DIR / "backfill_watchdog.log"
LOCK = STATE_DIR / "backfill_watchdog.lock"
CHAIN_LOG = STATE_DIR / "backfill_chain.log"

# The venv python.exe — the PROVEN invocation the manual chain uses (its site-
# packages are on its own sys.path, so no PYTHONPATH juggling is needed here).
VENV_PY = LOCAL_ROOT / "venv" / "Scripts" / "python.exe"

# --------------------------------------------------------------------------- #
# Target coverage windows (inclusive). SPXW 1-min 2026-07-02..07-20; SPX 1-min
# 2026-06-19..07-20; bulk-only EOD roots' tail defined by the backfill module.
# --------------------------------------------------------------------------- #
SPXW_START, SPXW_END = dt.date(2026, 7, 2), dt.date(2026, 7, 20)
SPX_START, SPX_END = dt.date(2026, 6, 19), dt.date(2026, 7, 20)

# Verified NYSE FULL-DAY closures that fall inside the 1-min windows above but are
# NOT in the collectors' known_empty JSONs. These days return no data and NEVER
# will, so requiring collection for them would keep completeness at "incomplete"
# forever and the watchdog could never write DONE / go quiet. Confirmed closures
# (SPY EOD holds a 0-row empty marker for each): 2026-06-19 Juneteenth (Fri),
# 2026-07-03 Independence Day observed (Fri). This is a factual holiday-calendar
# constant, not a tunable knob. (2026-07-20 is a real trading day the 1-min
# collectors DID capture — its SPY EOD marker is merely the not-yet-settled edge.)
NYSE_FULL_CLOSURES = {"20260619", "20260703"}

# A launch tick holds this lock so a second tick within the same interval can't
# also launch (belt-and-suspenders to the running-process check below).
LOCK_STALE_SECS = 900  # == the 15-min task interval

# Names whose presence in a python cmdline means a backfill is in flight.
_BACKFILL_SCRIPTS = (
    "collect_spxw_1m.py",
    "collect_spx_1m.py",
    "backfill_bulk_tail_20260721.py",
)


# --------------------------------------------------------------------------- #
# Logging / heartbeat — never raise.
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


def heartbeat(state: str) -> None:
    try:
        HEARTBEAT.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT.write_text(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {state}")
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# COMPLETENESS — coverage-based, reusing the collectors' own calendar helpers so
# the target set is byte-identical to what those collectors consider "done".
# --------------------------------------------------------------------------- #
def _oneminute_missing(mod, start: dt.date, end: dt.date) -> list[str]:
    """Missing YYYYMMDD days for a 1-min collector module over [start, end].

    Uses the module's OWN trading_days_newest_first (weekday filter),
    load_known_empty (verified NYSE closures), and day_done (both quote+ohlc
    non-empty) — so this matches the collector's notion of coverage exactly.
    """
    today = dt.date.today()
    skip = mod.load_known_empty() | NYSE_FULL_CLOSURES
    days = [d for d in mod.trading_days_newest_first(start, end)
            if mod.daystr(d) not in skip and d < today]
    return [mod.daystr(d) for d in days if not mod.day_done(d)]


def compute_missing() -> dict:
    """Scan disk for the whole target coverage. Returns a summary dict.

    Heavy imports (pandas via the collectors) are done here, lazily, so a tick
    that short-circuits on the DONE marker pays none of that cost.
    """
    import collect_spxw_1m as sw
    import collect_spx_1m as sx
    import backfill_bulk_tail_20260721 as bt
    import storage

    spxw = _oneminute_missing(sw, SPXW_START, SPXW_END)
    spx = _oneminute_missing(sx, SPX_START, SPX_END)

    bulk_roots = bt.bulk_only_roots()
    bulk_days = bt.trading_days()   # SPY-reference tail 2026-07-04..2026-07-20
    bulk_missing = [(r, d) for r in bulk_roots for d in bulk_days
                    if not storage.have_day(r, d)]

    complete = not spxw and not spx and not bulk_missing
    return {
        "complete": complete,
        "spxw_missing": spxw,
        "spx_missing": spx,
        "bulk_missing": bulk_missing,
        "bulk_roots": len(bulk_roots),
        "bulk_days": len(bulk_days),
    }


def summarize(m: dict) -> str:
    return (f"spxw_missing={len(m['spxw_missing'])} "
            f"spx_missing={len(m['spx_missing'])} "
            f"bulk_missing={len(m['bulk_missing'])} "
            f"(of {m['bulk_roots']} roots x {m['bulk_days']} days)")


# --------------------------------------------------------------------------- #
# Running-backfill detection — the ANTI-STACKING guard.
# --------------------------------------------------------------------------- #
def backfill_running() -> bool:
    """True if any python process is running one of the backfill scripts.

    Mirrors theta_terminal_watchdog.terminal_running(): enumerate process command
    lines via CIM and substring-match. On ANY uncertainty we return True (assume a
    backfill IS running) — erring toward NOT launching a second one is the safe
    default that protects the live manual chain.
    """
    if os.name != "nt":
        return False
    ps = (
        "Get-CimInstance Win32_Process "
        "-Filter \"Name='python.exe' OR Name='pythonw.exe'\" "
        "| Select-Object -ExpandProperty CommandLine"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=25,
        )
        cl = (out.stdout or "").lower()
        return any(s in cl for s in _BACKFILL_SCRIPTS)
    except Exception:
        log("WARN: could not enumerate python processes; assuming a backfill is "
            "running to avoid stacking a second one")
        return True


def terminal_up() -> bool:
    """Is the ThetaData terminal answering? Cheap one-shot (thetadata_client)."""
    try:
        import thetadata_client as td
        return bool(td.connected())
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Single-instance launch lock — two ticks can't both launch.
# --------------------------------------------------------------------------- #
def acquire_launch_lock(now: float) -> bool:
    """Claim the launch lock. True == caller may launch. A fresh lock (younger
    than one task interval) blocks; a stale one is stolen. On any FS error we
    return True (don't let lock trouble block a needed launch — the running-
    process check is the real anti-stacking guard)."""
    try:
        if LOCK.exists():
            age = now - LOCK.stat().st_mtime
            if age < LOCK_STALE_SECS:
                return False
        LOCK.parent.mkdir(parents=True, exist_ok=True)
        LOCK.write_text(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} pid={os.getpid()}")
        return True
    except OSError:
        return True


# --------------------------------------------------------------------------- #
# DONE marker.
# --------------------------------------------------------------------------- #
def write_done(m: dict) -> None:
    try:
        DONE_MARKER.parent.mkdir(parents=True, exist_ok=True)
        DONE_MARKER.write_text(
            f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  backfill COMPLETE\n"
            f"SPXW 1m {SPXW_START}..{SPXW_END}, SPX 1m {SPX_START}..{SPX_END}, "
            f"bulk-only EOD tail ({m['bulk_roots']} roots x {m['bulk_days']} days) "
            f"all present.\n",
            encoding="utf-8",
        )
    except OSError as e:
        log(f"could not write DONE marker ({e!r})")


# --------------------------------------------------------------------------- #
# Chain launcher — detached, sequential, resumable (each collector skips
# days already on disk). Returns True if the launch subprocess spawned cleanly.
# --------------------------------------------------------------------------- #
def launch_chain() -> bool:
    py = str(VENV_PY)
    redir = f'>> "{CHAIN_LOG}" 2>&1'
    chain = (
        f'"{py}" collect_spxw_1m.py --start 2026-07-02 --end 2026-07-20 {redir} & '
        f'"{py}" collect_spx_1m.py --start 2026-06-19 --end 2026-07-20 {redir} & '
        f'"{py}" backfill_bulk_tail_20260721.py {redir}'
    )
    creationflags = 0
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP: fully independent, no
        # console, not killed when this short-lived watchdog tick exits.
        creationflags = 0x00000008 | 0x00000200
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(
            ["cmd", "/c", chain],
            cwd=str(config.CODE_ROOT),
            creationflags=creationflags,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception as e:   # noqa: BLE001 — a launch hiccup must not raise out of policy
        log(f"launch_chain error: {e!r}")
        return False


# --------------------------------------------------------------------------- #
# PURE DECISION LOGIC — fully injectable (theta_terminal_watchdog.run_once style).
# Returns one of:
#   "already_done" | "complete_now" | "already_running" | "terminal_down"
#   | "lock_held" | "launched" | "launch_failed"
# --------------------------------------------------------------------------- #
def run_once(*, now, done_marker_exists, scan_missing, is_running, is_terminal_up,
             write_done_fn, acquire_lock_fn, launch_fn, log_fn):
    # (a) COMPLETENESS first.
    if done_marker_exists:
        log_fn("complete (DONE marker present) — nothing to do")
        return "already_done"
    m = scan_missing()
    if m["complete"]:
        write_done_fn(m)
        log_fn("complete — all target coverage present; wrote DONE marker, "
               "going quiet")
        return "complete_now"

    # (b) ANTI-STACKING: a backfill already running -> no-op.
    if is_running():
        log_fn(f"backfill already running, nothing to do [{summarize(m)}]")
        return "already_running"

    # (c) Terminal down -> defer to ThetaTerminalWatchdog (not our job to start it).
    if not is_terminal_up():
        log_fn("terminal down; ThetaTerminalWatchdog will recover it")
        return "terminal_down"

    # (d) Not complete, nothing running, terminal up -> launch the chain.
    if not acquire_lock_fn(now):
        log_fn("another watchdog tick holds the launch lock — not launching")
        return "lock_held"
    if launch_fn():
        log_fn("LAUNCHED backfill chain: collect_spxw_1m -> collect_spx_1m -> "
               f"backfill_bulk_tail_20260721 [{summarize(m)}]")
        return "launched"
    log_fn("launch reported failure")
    return "launch_failed"


# --------------------------------------------------------------------------- #
# main() — thin wire-up. Never raises; always exit 0 (scheduler-friendly).
# --------------------------------------------------------------------------- #
def main() -> int:
    import time
    action = "error"
    try:
        action = run_once(
            now=time.time(),
            done_marker_exists=DONE_MARKER.exists(),
            scan_missing=compute_missing,
            is_running=backfill_running,
            is_terminal_up=terminal_up,
            write_done_fn=write_done,
            acquire_lock_fn=acquire_launch_lock,
            launch_fn=launch_chain,
            log_fn=log,
        )
    except Exception as e:   # noqa: BLE001 — a transient error must never wedge the task
        log(f"unexpected error (exiting 0 so the task keeps its cadence): {e!r}")
    heartbeat(action)
    return 0


if __name__ == "__main__":
    sys.exit(main())
