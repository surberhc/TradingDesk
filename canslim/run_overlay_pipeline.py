r"""
run_overlay_pipeline.py — SUPERVISOR for the 18:00 real-quote overlay job.

WHAT IT DOES (idempotent, resumable, self-healing)
--------------------------------------------------
  1. PULL: run pull_equity_options.py (resumable) with a STALL WATCHDOG. If no new
     name-month parquet lands for STALL_SECS it kills & restarts the pull (which
     resumes via skip-done). Restart on crash/non-zero. Wait+retry if the Terminal
     is unreachable.
  2. BACKTEST: once the pull reports complete (or already-complete on a resume),
     run run_options_overlay_real.py to re-price the overlay on the REAL quotes.
  3. NOTIFY: write per-job status JSONs (the desk's dailyreport/status.py convention
     so EodReport picks them up) + a plain-English summary md to research/, and try
     to send a standalone email via dailyreport/mailer.py. On any FATAL error write a
     loud ALARM status.
  4. COMPLETION FLAG: on success, write a completion flag so the watchdog stands down.

SINGLETON: atomic PID lock — a second supervisor exits (no dup).
COMPLETION: writes a .complete flag; safe to re-run (it will no-op if already done).

LIVENESS RUBRIC
  crash -> restart the failing stage; stall -> stall-watchdog kill+restart;
  dup -> lock; partial -> pull resumes by skip-done, backtest is pure re-derive;
  poison -> pull skips bad items; dep-down -> wait_for_terminal; supervisor-death ->
  the separate watchdog task restarts THIS; reboot/missed-window -> the scheduled
  task re-fires and this resumes; unnoticed-death -> status JSON + ALARM + email.

Run:  <venv python> run_overlay_pipeline.py
      <venv python> run_overlay_pipeline.py --force   # ignore completion flag
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

HERE = Path(__file__).resolve().parent
DATACOLLECTOR = HERE.parent / "datacollector"
DAILYREPORT = HERE.parent / "dailyreport"

STATE_DIR = Path(r"C:\TradingDesk-Local\state\canslim")
WAREHOUSE = Path(r"C:\TradingDesk-Local\canslim\thetadata_equity")
LOG = STATE_DIR / "overlay_pipeline.log"
HEARTBEAT = STATE_DIR / "overlay_pipeline_heartbeat.json"
LOCK = STATE_DIR / "overlay_pipeline.lock"
COMPLETE_FLAG = STATE_DIR / "overlay_pipeline.complete"
PULL_HB = STATE_DIR / "pull_equity_options_heartbeat.json"

PULL_SCRIPT = HERE / "pull_equity_options.py"
BT_SCRIPT = HERE / "run_options_overlay_real.py"

# Use the base interpreter (this venv's python.exe is a relauncher STUB — spawning
# via it double-launches). Put venv site-packages on PYTHONPATH so deps import.
PY = getattr(sys, "_base_executable", None) or sys.executable

STALL_SECS = 1800        # 30 min with no new parquet AND no heartbeat bump = stalled
TICK = 30
MAX_PULL_RESTARTS = 12   # generous; each resume picks up where it left off


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


def heartbeat(stage: str, extra: dict | None = None) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        payload = {"stage": stage, "ts": dt.datetime.now().isoformat(timespec="seconds")}
        if extra:
            payload.update(extra)
        tmp = HEARTBEAT.with_name(HEARTBEAT.name + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        os.replace(tmp, HEARTBEAT)
    except OSError:
        pass


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


# --------------------------------------------------------------------------- #
# Status + notification (desk convention)
# --------------------------------------------------------------------------- #
def write_status(status: str, metrics: dict | None = None, message: str = "") -> None:
    """Write via dailyreport/status.py so EodReport aggregates it. Best-effort."""
    try:
        sys.path.insert(0, str(DAILYREPORT))
        import status as st  # type: ignore
        st.write("canslim_overlay_real", status, metrics or {}, message)
        log(f"status written: {status} — {message}")
    except Exception as e:
        log(f"WARN could not write desk status: {e!r}")


def send_email(subject: str, body: str) -> None:
    """Best-effort standalone email via dailyreport/mailer.send_html. Never fatal.
    The desk mailer sends HTML, so we wrap the plain body in a <pre> block."""
    try:
        sys.path.insert(0, str(DAILYREPORT))
        import mailer  # type: ignore
        html = ("<html><body style='font-family:system-ui,Segoe UI,sans-serif'>"
                f"<pre style='white-space:pre-wrap;font-size:14px'>{body}</pre>"
                "</body></html>")
        ok = mailer.send_html(subject, html)
        log(f"email {'sent' if ok else 'FAILED (mailer returned False)'} -> {subject}")
    except Exception as e:
        log(f"WARN email hook unavailable ({e!r}); status JSON + EodReport still notify")


# --------------------------------------------------------------------------- #
# Singleton lock
# --------------------------------------------------------------------------- #
def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                             capture_output=True, text=True, timeout=15)
        return str(pid) in out.stdout
    except Exception:
        return True


def acquire_lock() -> bool:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
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
                log(f"another pipeline supervisor is live (pid={holder}) -> exiting")
                return False
            log(f"stale lock (pid={holder} dead) -> reclaiming")
            try:
                LOCK.unlink()
            except OSError:
                pass
    return False


def release_lock() -> None:
    try:
        if LOCK.exists() and LOCK.read_text().strip() == str(os.getpid()):
            LOCK.unlink()
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Stage 1: the pull, with a stall watchdog
# --------------------------------------------------------------------------- #
def _parquet_count() -> int:
    try:
        return sum(1 for _ in WAREHOUSE.rglob("*.parquet"))
    except OSError:
        return 0


def _pull_hb_mtime() -> float:
    try:
        return PULL_HB.stat().st_mtime
    except OSError:
        return 0.0


def _pull_complete() -> bool:
    try:
        hb = json.loads(PULL_HB.read_text())
        return hb.get("phase") == "complete"
    except Exception:
        return False


def run_pull_stage() -> str:
    """Run the pull with a stall watchdog. -> 'done'|'stalled'|'exited'|'terminal_down'."""
    proc = subprocess.Popen([PY, "-u", str(PULL_SCRIPT)], cwd=str(HERE), env=_child_env())
    last_n, last_hb, last_progress = _parquet_count(), _pull_hb_mtime(), time.time()
    while True:
        time.sleep(TICK)
        n, hb = _parquet_count(), _pull_hb_mtime()
        heartbeat("pull", {"parquet_files": n, "pull_pid": proc.pid})
        rc = proc.poll()
        if rc is not None:
            if rc == 2:
                return "terminal_down"
            return "done" if rc == 0 else "exited"
        if n > last_n or hb > last_hb:      # progress = new file OR fresh pull heartbeat
            last_n, last_hb, last_progress = n, hb, time.time()
        elif time.time() - last_progress > STALL_SECS:
            log(f"STALL: no pull progress for {STALL_SECS//60} min -> kill & restart "
                "(pull resumes via skip-done)")
            proc.terminate()
            time.sleep(5)
            if proc.poll() is None:
                proc.kill()
            return "stalled"


def do_pull() -> bool:
    """Drive the pull to completion across restarts. Returns True if complete."""
    if _pull_complete():
        log("pull already complete (resume) — skipping to backtest")
        return True
    for attempt in range(1, MAX_PULL_RESTARTS + 1):
        log(f"--- pull attempt {attempt}/{MAX_PULL_RESTARTS} ---")
        result = run_pull_stage()
        if result == "done" and _pull_complete():
            log("=== pull reports COMPLETE ===")
            return True
        if result == "terminal_down":
            log("Terminal down — waiting 120s before retry")
            time.sleep(120)
            continue
        log(f"pull ended ({result}); restarting in 20s (resumes where it left off)")
        time.sleep(20)
    log("ALARM: pull did not complete within the restart budget")
    return _pull_complete()


# --------------------------------------------------------------------------- #
# Stage 2: the real backtest
# --------------------------------------------------------------------------- #
def do_backtest() -> dict | None:
    log("=== running real-quote overlay backtest ===")
    heartbeat("backtest")
    proc = subprocess.run([PY, "-u", str(BT_SCRIPT)], cwd=str(HERE), env=_child_env(),
                          capture_output=True, text=True)
    if proc.stdout:
        for ln in proc.stdout.splitlines():
            log(f"  [bt] {ln}")
    if proc.returncode != 0:
        log(f"backtest FAILED rc={proc.returncode}: {proc.stderr[-1000:]}")
        return None
    # Parse the CSV headline for the summary rather than re-running in-process.
    try:
        import csv as _csv
        res_path = HERE / "research" / "options_overlay_real_results.csv"
        stock_final = base_row = None
        rows = list(_csv.reader(open(res_path)))
        for r in rows:
            if len(r) > 11 and r[0] == "headline":
                stock_final = float(r[10])
            if len(r) > 11 and r[0] == "grid" and "6mo/ATM/d85/7%" in r[1]:
                base_row = r
        summary = {"stock_final": stock_final,
                   "base_cell": base_row[1] if base_row else None,
                   "base_final": float(base_row[10]) if base_row else None,
                   "base_medIV": base_row[15] if base_row and len(base_row) > 15 else None}
        return summary
    except Exception as e:
        log(f"WARN could not parse results CSV: {e!r}")
        return {}


# --------------------------------------------------------------------------- #
# Summary md
# --------------------------------------------------------------------------- #
def write_summary(summary: dict, ok: bool) -> None:
    p = HERE / "research" / "overlay_pipeline_summary.md"
    L = [f"# Options-overlay real-quote pipeline — run {dt.datetime.now():%Y-%m-%d %H:%M}"]
    L.append("")
    if not ok:
        L.append("**STATUS: FAILED / INCOMPLETE.** See overlay_pipeline.log for the trace.")
    else:
        L.append("**STATUS: complete.** Real ThetaData quotes pulled; overlay re-priced on them.")
        if summary:
            sf, bf = summary.get("stock_final"), summary.get("base_final")
            if sf and bf:
                delta = bf - sf
                verdict = "BEATS" if delta > 0 else "does NOT beat"
                L.append("")
                L.append(f"- Stock book final: **${int(sf):,}**")
                L.append(f"- Option (real, base 6mo/ATM/d0.85/7%) final: **${int(bf):,}** "
                         f"(median entry IV {summary.get('base_medIV')})")
                L.append(f"- On real quotes the option overlay **{verdict}** the stock at the base "
                         f"cell by **${int(delta):,}**.")
        L.append("")
        L.append("Full report: `research/options_overlay_real.md` · "
                 "grid CSV: `research/options_overlay_real_results.csv`")
    try:
        p.write_text("\n".join(L), encoding="utf-8")
        log(f"wrote summary {p}")
    except OSError as e:
        log(f"WARN could not write summary md: {e!r}")


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="ignore the completion flag")
    args = ap.parse_args()

    if COMPLETE_FLAG.exists() and not args.force:
        log("completion flag present — pipeline already done; exiting (idempotent).")
        return 0
    if not acquire_lock():
        return 0
    try:
        log("========== OVERLAY PIPELINE START ==========")
        heartbeat("start")
        write_status("partial", message="overlay pipeline started (pull -> real backtest)")

        pull_ok = do_pull()
        if not pull_ok:
            heartbeat("pull_failed")
            write_status("fail", message="ALARM: equity-options pull did not complete — "
                         "real backtest NOT run. See overlay_pipeline.log.")
            send_email("[TradingDesk ALARM] overlay pull FAILED",
                       "The 18:00 ThetaData equity-options pull did not complete within the "
                       "restart budget. The real-quote overlay backtest was NOT run. "
                       "See C:\\TradingDesk-Local\\state\\canslim\\overlay_pipeline.log.")
            write_summary({}, ok=False)
            return 1

        summary = do_backtest()
        if summary is None:
            heartbeat("backtest_failed")
            write_status("fail", message="ALARM: pull completed but the real-quote backtest "
                         "FAILED. See overlay_pipeline.log.")
            send_email("[TradingDesk ALARM] overlay backtest FAILED",
                       "The equity-options pull completed but run_options_overlay_real.py failed. "
                       "See C:\\TradingDesk-Local\\state\\canslim\\overlay_pipeline.log.")
            write_summary({}, ok=False)
            return 1

        # success
        COMPLETE_FLAG.write_text(dt.datetime.now().isoformat(timespec="seconds"))
        heartbeat("complete", summary)
        msg = "overlay pipeline COMPLETE: real quotes pulled, overlay re-priced."
        if summary.get("stock_final") and summary.get("base_final"):
            d = summary["base_final"] - summary["stock_final"]
            msg += (f" Base cell option ${int(summary['base_final']):,} vs stock "
                    f"${int(summary['stock_final']):,} ({'+' if d>=0 else ''}${int(d):,}).")
        write_status("ok", metrics=summary, message=msg)
        send_email("[TradingDesk] overlay real-quote re-run COMPLETE", msg +
                   "\n\nReport: canslim/research/options_overlay_real.md")
        write_summary(summary, ok=True)
        log("========== OVERLAY PIPELINE COMPLETE ==========")
        return 0
    except Exception as e:
        log(f"FATAL supervisor error: {e!r}")
        heartbeat("fatal", {"error": str(e)[:300]})
        write_status("fail", message=f"ALARM: pipeline fatal error: {e!r}")
        send_email("[TradingDesk ALARM] overlay pipeline fatal error", repr(e))
        return 1
    finally:
        release_lock()


if __name__ == "__main__":
    sys.exit(main())
