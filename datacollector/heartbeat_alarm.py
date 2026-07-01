r"""
heartbeat_alarm.py — independent staleness ALARM for the desk's data collectors.

THE GAP THIS CLOSES (2026-07-01)
  The SPXW 1-minute collector is supervised (spxw_1m_supervisor.py), which restarts
  the COLLECTOR on stall/crash and writes a heartbeat every ~30s. But nothing
  watched the SUPERVISOR ITSELF. When the supervisor PROCESS died (e.g. a machine
  reboot that its scheduled task didn't survive), the heartbeat simply went cold and
  NOBODY was told. That is exactly what happened: the collector sat dead ~15.5h with
  no alert. This is the missing outer watcher.

DESIGN
  * SINGLE-RUN checker, NOT a loop. Windows Task Scheduler runs it every 15 min
    (HeartbeatStalenessAlarm). Each run reads each monitored job's heartbeat, decides
    fresh / complete / STALE, and emails at most once per de-dupe window while cold.
  * INDEPENDENT of the thing it watches — it only reads files, never touches the
    collector, terminal, or supervisor. It cannot itself wedge the pipeline.
  * REUSES the project email path (dailyreport\mailer.py — the same Gmail STARTTLS
    creds the EOD digest uses). No new secret, no new SMTP code. Secrets are read by
    the mailer from %USERPROFILE%\rrg_secrets.env; this module never touches them.

WHY 15 MIN FOR THE SPXW COLLECTOR
  The supervisor writes its heartbeat every 30s, and its OWN stall-watchdog window is
  20 min (STALL_SECS=1200) — i.e. a merely-stalled-but-alive supervisor still writes
  heartbeats the whole time it waits. So a heartbeat that has been COLD for 15 min
  cannot be "just stalled": the supervisor PROCESS is gone. 15 min is comfortably
  past the 30s cadence yet catches a dead supervisor fast.

SUPPRESSION (no alert) when ANY of:
  * heartbeat text contains COMPLETE (supervisor finished the whole window), OR
  * progress.json shows days_done >= days_total (job legitimately finished), OR
  * the heartbeat file is FRESH (age < the job's threshold).

DE-DUPE
  A small JSON state file records the last alert time per job so we email at most
  once per COOLDOWN (default 3h) while a job stays cold, and CLEAR the cooldown once
  the job recovers (fresh again) so a future outage re-alerts immediately.

EXTENSIBILITY
  JOBS below is a simple list of dicts. Add a collector by appending one entry
  (its heartbeat path, optional progress.json, threshold, and the scheduler task
  name to name in the alert). Only the SPXW collector is wired today.

USAGE
  <venv python> heartbeat_alarm.py            # real run (the scheduler uses this)
  <venv python> heartbeat_alarm.py --dry-run  # decide + print, but NEVER send email
  <venv python> heartbeat_alarm.py --dry-run --test-stale <heartbeat_file>
                                              # self-test: force one job's heartbeat
                                              # path to a (stale) copy, decide, print
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

import config

# The project email path — same helper + creds the EOD digest uses. Imported
# lazily inside _send() so --dry-run self-tests never even need it importable.

# --------------------------------------------------------------------------- #
# Paths / tunables
# --------------------------------------------------------------------------- #
STATE_FILE = config.DATA_ROOT / "heartbeat_alarm_state.json"
LOG = config.DATA_ROOT / "heartbeat_alarm.log"
COOLDOWN_SECS = 3 * 3600            # at most one alert per job per 3h while cold

# --------------------------------------------------------------------------- #
# Monitored jobs — extensible. Add a collector = append one dict here.
#   name        : short id (state-file key + log)
#   label       : human name used in the alert subject/body
#   heartbeat   : Path to the heartbeat text file the job writes
#   progress    : Path to a progress.json (or None) — checked for days_done/days_total
#   threshold_s : seconds of no-update that counts as STALE
#   task_name   : the Windows scheduled task that OWNS the watched job (named in the
#                 alert so the reader knows exactly where to look)
# --------------------------------------------------------------------------- #
JOBS: list[dict] = [
    {
        "name": "spxw_1m",
        "label": "SPXW 1-min collector",
        "heartbeat": config.DATA_ROOT / "spxw_1m_supervisor_heartbeat.txt",
        "progress": config.DATA_ROOT / "spxw_1m_progress.json",
        # supervisor writes every 30s; its own stall window is 20 min, so 15 min
        # cold => the supervisor process is dead, not merely stalled.
        "threshold_s": 15 * 60,
        "task_name": "Spxw1mCollector",
    },
]


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
# State (de-dupe) — {job_name: {"last_alert_ts": epoch_float}}
# --------------------------------------------------------------------------- #
def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    try:
        tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(state, indent=2))
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        log(f"WARN could not write state file: {e!r}")


# --------------------------------------------------------------------------- #
# Assessment
# --------------------------------------------------------------------------- #
def _progress_complete(progress_path) -> bool:
    """True if progress.json shows days_done >= days_total (legit finish)."""
    if not progress_path:
        return False
    try:
        p = json.loads(Path(progress_path).read_text())
    except (OSError, ValueError):
        return False
    done, total = p.get("days_done"), p.get("days_total")
    try:
        return total is not None and done is not None and int(done) >= int(total)
    except (TypeError, ValueError):
        return False


def _progress_pct(progress_path) -> str:
    """A '89.7% (1052/1173)' progress string for the alert body, or 'unknown'."""
    if not progress_path:
        return "unknown"
    try:
        p = json.loads(Path(progress_path).read_text())
    except (OSError, ValueError):
        return "unknown"
    done, total, pct = p.get("days_done"), p.get("days_total"), p.get("pct")
    if done is not None and total is not None:
        pct_str = f"{pct}%" if pct is not None else "?"
        return f"{pct_str} ({done}/{total} days)"
    return "unknown"


def assess(job: dict, now: float, heartbeat_override: str | None = None) -> dict:
    """Decide one job's status WITHOUT side effects.

    Returns a dict:
      status : 'fresh' | 'complete' | 'stale' | 'missing'
      alert  : bool  (True only when status == 'stale')
      age_s  : float | None   seconds since the heartbeat was last written
      last_ts: str            last heartbeat text line (or a marker)
      hb_path: str            the heartbeat path actually used
      progress: str           human progress string
    """
    hb_path = Path(heartbeat_override) if heartbeat_override else Path(job["heartbeat"])
    progress = _progress_pct(job.get("progress"))

    if not hb_path.exists():
        # No heartbeat file at all. If progress says the job finished, that's a
        # legit finish (don't alert). Otherwise it never started / vanished.
        if _progress_complete(job.get("progress")):
            return {"status": "complete", "alert": False, "age_s": None,
                    "last_ts": "(no heartbeat file; progress shows complete)",
                    "hb_path": str(hb_path), "progress": progress}
        return {"status": "missing", "alert": True, "age_s": None,
                "last_ts": "(heartbeat file absent)",
                "hb_path": str(hb_path), "progress": progress}

    try:
        text = hb_path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        text = ""
    last_line = text.splitlines()[-1].strip() if text else "(empty heartbeat)"

    # Completion suppression #1: the literal COMPLETE marker.
    if "COMPLETE" in text.upper():
        return {"status": "complete", "alert": False, "age_s": None,
                "last_ts": last_line, "hb_path": str(hb_path), "progress": progress}

    # Completion suppression #2: progress.json says every day is done.
    if _progress_complete(job.get("progress")):
        return {"status": "complete", "alert": False, "age_s": None,
                "last_ts": last_line, "hb_path": str(hb_path), "progress": progress}

    # Freshness: mtime is the file's actual last-write time (the supervisor
    # rewrites the whole file every ~30s, so mtime == last heartbeat).
    try:
        age = now - hb_path.stat().st_mtime
    except OSError:
        age = None

    if age is None:
        return {"status": "missing", "alert": True, "age_s": None,
                "last_ts": last_line, "hb_path": str(hb_path), "progress": progress}

    if age < job["threshold_s"]:
        return {"status": "fresh", "alert": False, "age_s": age,
                "last_ts": last_line, "hb_path": str(hb_path), "progress": progress}

    return {"status": "stale", "alert": True, "age_s": age,
            "last_ts": last_line, "hb_path": str(hb_path), "progress": progress}


# --------------------------------------------------------------------------- #
# Alert
# --------------------------------------------------------------------------- #
def _fmt_age(age_s) -> str:
    if age_s is None:
        return "unknown"
    m = int(age_s // 60)
    if m < 90:
        return f"{m}m"
    return f"{m // 60}h{m % 60:02d}m"


def _build_alert(job: dict, a: dict) -> tuple[str, str]:
    """(subject, html_body) for a stale/missing job."""
    age = _fmt_age(a["age_s"])
    subject = f"[TradingDesk ALARM] {job['label']} heartbeat cold {age}"
    cause = (f"The supervisor/process appears to have DIED (heartbeat has not "
             f"updated in {age}, well past the {job['threshold_s'] // 60}-minute "
             f"staleness threshold — the supervisor writes every ~30s, so this is "
             f"not a mere stall). Check Windows Task Scheduler task "
             f"<b>{job['task_name']}</b> and restart it if needed.")
    if a["status"] == "missing":
        cause = (f"The heartbeat file is ABSENT — the job may never have started. "
                 f"Check Windows Task Scheduler task <b>{job['task_name']}</b>.")
    rows = [
        ("Job", job["label"]),
        ("Status", a["status"].upper()),
        ("Last heartbeat", a["last_ts"]),
        ("Age (cold for)", age),
        ("Progress", a["progress"]),
        ("Heartbeat file", a["hb_path"]),
        ("Owning task", job["task_name"]),
    ]
    rows_html = "".join(
        f'<tr><td style="padding:2px 14px 2px 0;color:#6b7280;white-space:nowrap;">{k}</td>'
        f'<td style="padding:2px 0;color:#111827;">{v}</td></tr>'
        for k, v in rows)
    html = (
        f'<div style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;'
        f'max-width:640px;margin:0 auto;color:#111827;">'
        f'<div style="font-size:18px;font-weight:700;color:#ef4444;">'
        f'&#9679; TradingDesk ALARM — collector heartbeat cold</div>'
        f'<div style="font-size:13px;color:#374151;margin:8px 0;">{cause}</div>'
        f'<table style="border-collapse:collapse;font-size:13px;margin-top:6px;">'
        f'{rows_html}</table>'
        f'<div style="font-size:11px;color:#9ca3af;margin-top:10px;">'
        f'Automated staleness alarm · TradingDesk\\datacollector\\heartbeat_alarm.py</div></div>')
    return subject, html


def _send(subject: str, html: str) -> bool:
    """Send via the project mailer (same creds as the EOD digest). Never raises."""
    try:
        # dailyreport is a sibling package on Drive; add it to sys.path so the same
        # mailer that the EOD digest uses is reused verbatim (one email path).
        mailer_dir = config.CODE_ROOT.parent / "dailyreport"
        if str(mailer_dir) not in sys.path:
            sys.path.insert(0, str(mailer_dir))
        import mailer  # noqa: E402
        return mailer.send_html(subject, html)
    except Exception as e:  # noqa: BLE001 — the alarm must never crash the run
        log(f"EMAIL PATH FAILED: {type(e).__name__}: {e}")
        return False


# --------------------------------------------------------------------------- #
# Per-job handling (de-dupe + send) and one-line status
# --------------------------------------------------------------------------- #
def handle_job(job: dict, state: dict, now: float, dry_run: bool,
               heartbeat_override: str | None = None) -> str:
    a = assess(job, now, heartbeat_override=heartbeat_override)
    name = job["name"]
    js = state.setdefault(name, {})
    age = _fmt_age(a["age_s"])

    if not a["alert"]:
        # Recovered / fresh / complete -> clear any cooldown so a future outage
        # re-alerts immediately.
        if js.pop("last_alert_ts", None) is not None:
            log(f"{name}: recovered ({a['status']}) — cleared alert cooldown")
        return (f"{name}: {a['status'].upper()} (age {age}, "
                f"progress {a['progress']}) — no alert")

    # Alert-worthy (stale/missing). Apply the de-dupe cooldown.
    last = js.get("last_alert_ts")
    cool_remaining = (last + COOLDOWN_SECS - now) if last else 0
    if last and cool_remaining > 0:
        return (f"{name}: {a['status'].upper()} (age {age}) — alert SUPPRESSED "
                f"(cooldown {int(cool_remaining // 60)}m left)")

    subject, html = _build_alert(job, a)
    if dry_run:
        log(f"WOULD-SEND: {subject}")
        # In dry-run we do NOT record the alert time (so repeated self-tests behave
        # identically and we never suppress a real future alert).
        return f"{name}: {a['status'].upper()} (age {age}) — WOULD-SEND (dry-run)"

    sent = _send(subject, html)
    if sent:
        js["last_alert_ts"] = now
        return f"{name}: {a['status'].upper()} (age {age}) — ALERT SENT"
    return f"{name}: {a['status'].upper()} (age {age}) — SEND FAILED (will retry next run)"


# --------------------------------------------------------------------------- #
# Main — single run
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="Data-collector heartbeat staleness alarm (single run).")
    ap.add_argument("--dry-run", action="store_true",
                    help="assess and print, but never send an email.")
    ap.add_argument("--test-stale", default=None, metavar="HEARTBEAT_FILE",
                    help="self-test only: override the FIRST job's heartbeat path "
                         "with this file (e.g. a stale copy) to exercise the stale "
                         "branch without touching the real collector.")
    args = ap.parse_args()

    now = dt.datetime.now().timestamp()
    state = _load_state()
    lines: list[str] = []

    for i, job in enumerate(JOBS):
        override = args.test_stale if (args.test_stale and i == 0) else None
        try:
            line = handle_job(job, state, now, args.dry_run, heartbeat_override=override)
        except Exception as e:  # noqa: BLE001 — one bad job must not kill the sweep
            line = f"{job.get('name', '?')}: CHECK ERROR — {type(e).__name__}: {e}"
        lines.append(line)
        log(line)

    if not args.dry_run:
        _save_state(state)

    # One consolidated status line every run (in addition to per-job lines).
    log("sweep done: " + " | ".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
