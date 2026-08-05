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
    fresh / complete / STALE, LOGS every per-job line, and updates state. That
    ASSESSMENT cadence is unchanged. What changed (2026-08-05) is only WHEN EMAIL is
    sent: at most ONE consolidated morning digest per day (see DIGEST below), never
    the old per-problem repeat-every-3h paging that woke the owner overnight.
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

SUPPRESSION (no alert) — FRESHNESS GATES COMPLETION (fix 2026-07-06):
  * the heartbeat file is FRESH (age < the job's threshold) AND the heartbeat text
    contains COMPLETE (supervisor finished the whole window), OR
  * the heartbeat file is FRESH AND progress.json shows the job finished
    (complete flag, or done >= total), OR
  * the heartbeat file is FRESH with an ordinary (in-progress) heartbeat.
  A COMPLETE marker or a progress 'complete' flag on a COLD/absent heartbeat does NOT
  suppress — a stale leftover 'complete:true' beside a dead supervisor is exactly the
  2026-07-05 silent-death this alarm now catches instead of logging "COMPLETE".

DIGEST (2026-08-05) — ONE EMAIL PER DAY, SILENT OVERNIGHT
  The owner's hard requirement: at most ONE email per day and total silence overnight.
  So the old per-problem immediate send + 3h cooldown is REPLACED by a single
  consolidated morning digest:
    * Every 15-min sweep still assesses every JOBS entry, every DEADLINE_JOBS entry,
      and the Drive-sync tripwire; still logs every per-job line + the "sweep done:"
      line; still updates state; still writes RAN_MARKER. None of that changed.
    * The sweep builds a list of CURRENTLY-OUTSTANDING problems. If (and only if) the
      local hour is in [DIGEST_SEND_HOUR, DIGEST_WINDOW_END_HOUR), we have NOT already
      sent today, and there is >=1 outstanding problem, ONE email is sent listing ALL
      outstanding problems (each with its label, status, age/detail, owning task, and
      the same remediation wording the old per-problem alerts carried). We then record
      state["_digest"]["last_sent_date"] = today.
    * Outside the window, already-sent-today, or zero problems -> assess + log only, no
      email. Overnight is silent. A problem that clears before the morning window simply
      never appears in that day's digest (recovery = absence from the digest).
  The Drive-sync tripwire (Google-Drive corruption risk) is currently immediate; per
  the one-email-a-day rule it is FOLDED INTO THE SAME DAILY DIGEST rather than sent
  separately. (Whether that one guard deserves an overnight-exception is an open owner
  decision; for now, one digest for everything.)

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
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import config

# The project email path — same helper + creds the EOD digest uses. Imported
# lazily inside _send() so --dry-run self-tests never even need it importable.

# Shared trading calendar — used to skip market-dependent deadline checks on days
# the market is closed (weekends + holidays), so a job that legitimately has no
# session to run is never mis-alarmed as "did NOT run today". Imported defensively:
# if it's unavailable or the year isn't tabled, _is_trading_day degrades to a plain
# weekday rule (Mon-Fri) so a missing calendar makes the alarm slightly noisier on
# holidays, NEVER silently wrong or crashed.
try:
    from connections import market_calendar as _mktcal
except Exception:  # noqa: BLE001 — the alarm must import even if the calendar is missing
    _mktcal = None

# The repo-backup job owns its own heartbeat path; import it rather than keeping a
# hand-maintained SECOND COPY here. That duplication is exactly what caused the
# 2026-07-09 false pages (see the deadline-lookup note below): a copied constant
# drifted from its source and the alarm fired against a reality that no longer
# existed. Imported defensively — repo_backup is stdlib-only, but if it ever fails
# to import, the alarm must still run and we fall back to the literal path.
try:
    import repo_backup as _repo_backup
except Exception:  # noqa: BLE001 — the alarm must import even if the job module doesn't
    _repo_backup = None

# The Drive-sync tripwire — a STATE ASSERTION (not a staleness check): it pages the
# moment C:\TradingDesk-Local comes under Google Drive sync/backup management (the
# 2026-07-16 wrong-folder corruption risk). Evaluated every sweep by handle_tripwire()
# below. Imported defensively — it is stdlib-only, but if it ever fails to import the
# alarm must still run; handle_tripwire() then FAILS CLOSED and pages "could not
# evaluate" rather than silently dropping the guard.
try:
    import drive_sync_tripwire as _tripwire
except Exception:  # noqa: BLE001 — the alarm must import even if the tripwire doesn't
    _tripwire = None


def _is_trading_day(d: "dt.date") -> bool:
    """Calendar-aware 'is the US market open on day d' that NEVER raises."""
    if _mktcal is not None:
        try:
            return _mktcal.is_trading_day(d)
        except Exception:  # noqa: BLE001 — un-tabled year etc. -> weekday fallback
            pass
    return d.weekday() < 5


# --------------------------------------------------------------------------- #
# Live Task Scheduler deadline lookup (2026-07-09 fix)
# --------------------------------------------------------------------------- #
# WHY: DEADLINE_JOBS used to hardcode a deadline_hhmm that was a hand-maintained
# SECOND COPY of "when does this task actually run" (the real source of truth is
# the Windows Scheduled Task's own trigger times). When TiingoDailyUpdate and
# AccountMonitorDaily got their trigger times changed, nobody updated this file's
# copy, and the alarm fired false pages against a schedule that no longer existed.
# Fix: derive the deadline LIVE from the task's own trigger(s) via `schtasks /query
# /xml`, and keep the old hardcoded value only as a defensive fallback (task
# deleted/renamed, schtasks unavailable, no time-based trigger, etc).
_task_deadline_cache: dict[str, tuple[int, int] | None] = {}


def _latest_task_trigger_hhmm(task_name: str) -> tuple[int, int] | None:
    """Query Windows Task Scheduler for task_name's trigger(s) and return the LATEST
    (H, M) local time among them, or None on ANY failure (task missing, schtasks
    unavailable/slow, malformed XML, no time-based trigger). NEVER raises. Cached
    per task_name for the life of this process (single-run script)."""
    if task_name in _task_deadline_cache:
        return _task_deadline_cache[task_name]

    result: tuple[int, int] | None = None
    try:
        proc = subprocess.run(
            ["schtasks", "/query", "/tn", task_name, "/xml"],
            capture_output=True, text=True, timeout=15, check=False,
        )
        if proc.returncode == 0 and proc.stdout:
            root = ET.fromstring(proc.stdout)
            latest: dt.time | None = None
            for el in root.iter():
                if el.tag.endswith("StartBoundary") and el.text:
                    # Format: 2026-07-09T20:45:00-05:00 (offset optional/variable).
                    # Strptime can't handle a colon-containing UTC offset uniformly
                    # across Python versions, so just parse the local H:M ourselves.
                    try:
                        time_part = el.text.split("T", 1)[1]
                        hh, mm = time_part.split(":")[:2]
                        t = dt.time(int(hh), int(mm))
                    except (IndexError, ValueError):
                        continue
                    if latest is None or t > latest:
                        latest = t
            if latest is not None:
                result = (latest.hour, latest.minute)
    except Exception:  # noqa: BLE001 — live schedule lookup is best-effort only
        result = None

    _task_deadline_cache[task_name] = result
    return result

# --------------------------------------------------------------------------- #
# Paths / tunables
# --------------------------------------------------------------------------- #
STATE_FILE = config.DATA_ROOT / "heartbeat_alarm_state.json"
LOG = config.DATA_ROOT / "heartbeat_alarm.log"

# --- Consolidated daily digest (2026-08-05) -------------------------------- #
# Owner requirement: AT MOST ONE email per day — a single morning digest listing every
# outstanding problem — and TOTAL SILENCE overnight. The 15-min sweep still ASSESSES,
# LOGS, updates state, and writes RAN_MARKER every run (unchanged); only EMAIL is gated.
# The digest may send only when the local hour is in [DIGEST_SEND_HOUR,
# DIGEST_WINDOW_END_HOUR) and only once per calendar day (tracked in the state file
# under state["_digest"]["last_sent_date"]). This REPLACES the old per-problem immediate
# send + 3h cooldown (COOLDOWN_SECS), which paged the owner day and night.
DIGEST_SEND_HOUR = 7               # earliest local (CT) hour the digest may send (07:00)
DIGEST_WINDOW_END_HOUR = 12        # exclusive upper bound of the send window (noon)
_DIGEST_STATE_KEY = "_digest"      # state sub-dict: {"last_sent_date": "YYYYMMDD"}

# Proof-of-life marker written at the end of every sweep. The EOD digest reads its
# mtime (build_alarm) so the report the user reads turns red if the alarm dies.
RAN_MARKER = config.DATA_ROOT / "heartbeat_alarm_ran.txt"

# --------------------------------------------------------------------------- #
# Monitored jobs — extensible. Add a collector = append one dict here.
#   name        : short id (state-file key + log)
#   label       : human name used in the alert subject/body
#   heartbeat   : Path to the heartbeat text file the job writes
#   progress    : Path to a progress.json (or None) — checked for days_done/days_total
#   threshold_s : seconds of no-update that counts as STALE
#   task_name   : the Windows scheduled task that OWNS the watched job (named in the
#                 alert so the reader knows exactly where to look)
#   cause_stale : OPTIONAL human explanation for the alert body, overriding the
#                 default supervisor-died wording (which is SPXW-collector specific
#                 and would be actively misleading for a non-supervised job).
#                 May use {age} and {task_name}. cause_missing likewise.
# --------------------------------------------------------------------------- #

# How long without a VERIFIED repo backup before we page. The backup is intended to
# run daily; 26h = one day + a 2h grace so a late run never false-pages.
# NOTE (Andrew's call): this assumes a DAILY cadence. The scheduled task is
# deliberately NOT registered by this build — if you schedule it at a different
# cadence, change this number to match, or it will page (too tight) or sleep through
# a real outage (too loose).
REPO_BACKUP_THRESHOLD_S = 26 * 3600

_REPO_BACKUP_HB = (
    _repo_backup.HEARTBEAT_FILE if _repo_backup is not None
    else Path(r"C:\TradingDesk-Local\backups\repo_backup_heartbeat.txt"))

JOBS: list[dict] = [
    # repo_backup — THE 2026-07-16 GAP. Google Drive silently synced the WRONG folder
    # for 9 days (2026-07-07..07-16); 85 commits never left the machine and NO ERROR
    # WAS EVER RAISED, because nothing failed — Drive faithfully synced a folder that
    # had stopped changing. A backup that can fail silently is not a backup, so the
    # only defence is an alarm that fires on SILENCE.
    #
    # THE CONTRACT: repo_backup.py refreshes this heartbeat IF AND ONLY IF a bundle
    # verified okay-with-complete-history AND landed on a confirmed Drive-managed
    # destination AND re-verified there AND sync was not paused. Every failure path
    # leaves it untouched. So a failed backup and a never-ran backup look IDENTICAL
    # from here — both go cold, both page. That symmetry is deliberate: it means this
    # alarm cannot be fooled by a job that fails in a way we never anticipated.
    {"name": "repo_backup",
     "label": "TradingDesk repo backup (git bundle -> Drive)",
     "heartbeat": _REPO_BACKUP_HB,
     "progress": None,
     "threshold_s": REPO_BACKUP_THRESHOLD_S,
     "task_name": "RepoBackupDaily",
     "cause_stale": (
         "No VERIFIED repo backup has landed in {age}. The backup job refreshes its "
         "heartbeat ONLY on a fully verified success (bundle verified okay + complete "
         "history, placed on a confirmed Drive-managed destination, re-verified there, "
         "sync not paused), so this means the backup either FAILED or never ran — and "
         "your commits may exist on exactly one machine right now. This is the 9-day "
         "silent-sync failure of 2026-07-16 repeating. Check Task Scheduler task "
         "<b>{task_name}</b>, then run repo_backup.py by hand and read its output; "
         "the status file (repo_backup_status.json) records the exact failure."),
     "cause_missing": (
         "The repo-backup heartbeat file is ABSENT — no verified backup has EVER been "
         "recorded. Either the job has never successfully run, or its very first run "
         "failed. Check Task Scheduler task <b>{task_name}</b> and run repo_backup.py "
         "by hand; the status file (repo_backup_status.json) records the exact failure."),
     },
    # data_backup REMOVED 2026-07-27 (Andrew's call): the nightly rclone copy+check of
    # the ~104 GB warehouse to Drive was over-engineered and low-value — the only
    # irreplaceable data (frozen 1-min history) is already verified in Drive (07-25
    # backup, 912k files) and everything newer is re-pullable. Its staleness job was the
    # alert-flood source once the backup stopped running, so it (plus data_backup.py, its
    # launcher, and the DataBackupDaily task) is gone. repo_backup below is UNCHANGED.
    # spxw_1m (SPXW 1-min collector / Spxw1mCollector task) REMOVED 2026-07-07: the
    # one-time historical backfill finished 2026-07-02 (1127/1127 days, 100%) and the
    # job was intentionally superseded by universe_dl (UniverseDownloadEod) below. The
    # Spxw1mCollector scheduled task is intentionally DISABLED (confirmed via
    # Get-ScheduledTask) and its heartbeat is intentionally cold forever now, so
    # monitoring it here produced a recurring false "heartbeat cold" page. See
    # conductor/STATUS.md and memory `options-warehouse` for the backfill-complete record.
    # universe_dl (expanded-universe options bulk pull / UniverseDownloadEod task)
    # STOPPED 2026-07-10: Andrew halted the pull -- its only live justification (unblocking
    # the diversified single-name tastytrade strangle-basket test) was contingent on the
    # SPX short-strangle pre-registered test passing, and it was REFUTED (collapsed to
    # equity beta, see PREREG_short_strangle_alpha_2026-07-06.md + conductor/STATUS.md).
    # A deep-dive also disproved the initial assumption that this pull was CAN SLIM-related
    # (zero code/data link found; disjoint universes -- see conductor log). The
    # UniverseDownloadEod scheduled task is being disabled so monitoring its heartbeat here
    # would produce a false "heartbeat cold" page for a deliberately-stopped job, same
    # reason spxw_1m was removed above. Data already on disk (raw/options, raw/options_snap)
    # is left untouched.
]


# --------------------------------------------------------------------------- #
# Per-job DEADLINE watchdogs — distinct from the rolling-freshness JOBS above.
# These jobs each run ONCE daily and write a status JSON (status.py) only when they
# finish. If one crashes before writing (as the EOD report did silently
# 2026-06-27..07-01), no fresh status appears and nobody is told. So: after each
# job's grace deadline, if today's status JSON is missing OR stale (date != today)
# OR status == "fail", alarm. These all ride this same 15-min task — no new
# scheduled tasks needed.
#
# Each entry:
#   name                  : status-file stem + state-file key + log id
#   label                 : human name used in the alert
#   status_file           : Path to the status JSON (status.py output)
#   deadline_hhmm_fallback: (H, M) local (CT) time by which the job must have run —
#                           used ONLY if the live Task Scheduler query fails (task
#                           renamed/deleted, schtasks unavailable, no time trigger).
#                           This is NOT the source of truth; it is a defensive copy
#                           that can drift, which is exactly what happened 2026-07-09
#                           (Tiingo/AccountMonitor trigger times changed here without
#                           updating the copy -> false pages). The live query in
#                           handle_deadline() is now the primary source.
#   deadline_buffer_min   : minutes of slack added on top of the task's LATEST live
#                           trigger time to get the actual deadline (grace period for
#                           the job to finish running once triggered).
#   task_name             : the Windows scheduled task that owns the job (named in
#                           the alert AND queried live for its trigger times)
# --------------------------------------------------------------------------- #
_STATUS_DIR = Path(r"C:\TradingDesk-Local\state\dailyreport\status")

DEADLINE_JOBS: list[dict] = [
    {"name": "eod_report", "label": "nightly EOD email",
     "status_file": _STATUS_DIR / "eod_report.json",
     "deadline_hhmm_fallback": (21, 15), "deadline_buffer_min": 15,
     "task_name": "EodReport",
     "market_dependent": False},
    {"name": "forward", "label": "IBKR forward options collector",
     "status_file": _STATUS_DIR / "forward.json",
     # IbkrForwardEodDaily TRIGGERS ~17:30 CT (the ThetaData->IBKR cutover of 2026-07-27;
     # this job replaced the retired ThetaEodDaily/eod_daily.py). The index-only IBKR
     # forward grab (SPX/SPXW/RUT/NDX, per-root deep depth) writes its "forward" status
     # after the run; SPX/SPXW alone legitimately take ~11-13 min each, so the whole
     # index run can take a while. A 60-min buffer puts the deadline at ~18:30, still
     # well before the 21:00 EodReport that consumes this data. Raise further only if the
     # grab starts running longer.
     "deadline_hhmm_fallback": (19, 0), "deadline_buffer_min": 60,
     "task_name": "IbkrForwardEodDaily",
     "market_dependent": True},
    {"name": "tiingo", "label": "Tiingo daily data refresh",
     "status_file": _STATUS_DIR / "tiingo.json",
     "deadline_hhmm_fallback": (21, 0), "deadline_buffer_min": 15,
     "task_name": "TiingoDailyUpdate",
     "market_dependent": True},
    {"name": "gex", "label": "GEX dealer-gamma build",
     "status_file": _STATUS_DIR / "gex.json",
     "deadline_hhmm_fallback": (20, 0), "deadline_buffer_min": 15,
     "task_name": "GexDailyBuild",
     "market_dependent": True},
    # account_monitor DE-LISTED 2026-07-28: AccountMonitorDaily is deliberately paused
    # (gateway quarantine); a paused job was false-paging nightly after its 21:30 deadline.
    # Re-add this entry when the monitor is revived. See eod_report.py main() for the paired change.
    # {"name": "account_monitor", "label": "account-cashflow monitor",
    #  "status_file": _STATUS_DIR / "account_monitor.json",
    #  "deadline_hhmm_fallback": (21, 30), "deadline_buffer_min": 15,
    #  "task_name": "AccountMonitorDaily",
    #  "market_dependent": True},
]


def handle_deadline(job: dict, state: dict, now: float) -> tuple[str, dict | None]:
    """Assess a once-daily job against its deadline WITHOUT sending. After the deadline,
    the job is OUTSTANDING if the status JSON's date != today OR its status == 'fail' OR
    it's missing/unreadable. Returns (one-line status, problem|None); a non-None problem
    is folded into the daily consolidated digest by main(). Recovery/pre-deadline clears
    any legacy cooldown key so state stays clean and logs stay accurate."""
    name = job["name"]
    label = job["label"]
    status_file = Path(job["status_file"])
    task_name = job["task_name"]
    buffer_min = job.get("deadline_buffer_min", 15)

    live = _latest_task_trigger_hhmm(task_name)
    if live is not None:
        dh, dm = live
        dm += buffer_min
        dh += dm // 60
        dm %= 60
        dh %= 24
    else:
        dh, dm = job["deadline_hhmm_fallback"]
        log(f"{name}: live Task Scheduler lookup failed for '{task_name}' — "
            f"using fallback deadline {dh:02d}:{dm:02d} (informational only)")

    js = state.setdefault(name, {})
    now_dt = dt.datetime.fromtimestamp(now)
    deadline = now_dt.replace(hour=dh, minute=dm, second=0, microsecond=0)
    today = now_dt.strftime("%Y%m%d")
    hhmm = f"{dh:02d}:{dm:02d}"

    if job.get("market_dependent") and not _is_trading_day(now_dt.date()):
        # Market closed today (weekend/holiday): this job has no session to run,
        # so its absence is expected, not a fault. Clear any legacy cooldown and skip.
        js.pop("last_alert_ts", None)
        return f"{name}: market closed today — {label} not expected (no check)", None

    if now_dt < deadline:
        # Pre-deadline: the job may still legitimately run — not outstanding yet.
        js.pop("last_alert_ts", None)
        return f"{name}: pre-deadline ({now_dt:%H:%M} < {hhmm}) — no check", None

    ok, detail = False, "status file absent/unreadable"
    try:
        s = json.loads(status_file.read_text())
        st = s.get("status")
        date_ok = s.get("date") == today
        if date_ok and st != "fail":
            ok = True
        else:
            detail = f"date={s.get('date')} status={st}"
    except (OSError, ValueError):
        pass

    if ok:
        if js.pop("last_alert_ts", None) is not None:
            log(f"{name}: recovered — cleared alert cooldown")
        return f"{name}: OK (today's {label} confirmed by status file)", None

    # OUTSTANDING — build a problem for the digest (no immediate send).
    cause = (
        f'It is past {hhmm} and no fresh, healthy status was recorded today for '
        f'<b>{label}</b>. The <b>{task_name}</b> task may have crashed or not fired. '
        f'Check its log and run it manually.')
    problem = {
        "label": label,
        "status": "MISSING",
        "cause": cause,
        "rows": [("Detail", detail), ("Status file", str(status_file)),
                 ("Owning task", task_name)],
    }
    return f"{name}: MISSING ({detail}) — OUTSTANDING (folded into daily digest)", problem


# --------------------------------------------------------------------------- #
# Drive-sync tripwire — a STATE ASSERTION, not a staleness check
# --------------------------------------------------------------------------- #
# Unlike the JOBS/DEADLINE_JOBS above (which page when a heartbeat/status goes
# STALE), this pages when a bad condition becomes TRUE: C:\TradingDesk-Local coming
# under Google Drive sync/backup management. It rides the same 15-min sweep and reuses
# the same COOLDOWN de-dupe + _send() path, so no new scheduled task is needed. The
# evaluation itself lives in drive_sync_tripwire.py (which reuses repo_backup's DriveFS
# helpers); this handler only owns the page decision, exactly like handle_deadline.
_TRIPWIRE_NAME = "drive_sync_tripwire"


def handle_tripwire(state: dict, now: float) -> tuple[str, dict | None]:
    """Evaluate the Drive-sync tripwire WITHOUT sending. If it TRIPPED or is UNEVALUABLE
    it becomes an OUTSTANDING problem folded into the daily digest by main().

    FAILS CLOSED: if the tripwire module could not be imported, or a check inside it
    could not be evaluated, that is itself an outstanding problem ("could not evaluate")
    rather than silence — a guard that quietly can't look is the exact silent failure the
    whole body of work exists to kill. Per the one-email-a-day rule this guard rides the
    SAME daily digest as everything else (overnight-exception is an open owner decision).
    Clears any legacy cooldown key on recovery so state stays clean."""
    name = _TRIPWIRE_NAME
    js = state.setdefault(name, {})

    if _tripwire is None:
        v = {"ok": False, "tripped": False, "unevaluable": True, "should_page": True,
             "reasons": ["[import] COULD NOT EVALUATE — drive_sync_tripwire failed to "
                         "import, so the Drive-management guard is not running"],
             "remediation": (r"C:\TradingDesk-Local appears to be under Google Drive "
                             r"sync/backup management — this is the wrong-folder "
                             r"corruption risk; disconnect it in Google Drive Desktop "
                             r"immediately."),
             "protected": [r"C:\TradingDesk-Local"]}
    else:
        v = _tripwire.evaluate()

    if not v.get("should_page"):
        if js.pop("last_alert_ts", None) is not None:
            log(f"{name}: recovered (GREEN) — cleared alert cooldown")
        return f"{name}: GREEN (TradingDesk-Local not under Drive management) — no alert", None

    kind = "TRIPPED" if v.get("tripped") else "UNEVALUABLE"

    if v.get("tripped"):
        headline = "TradingDesk-Local under Google Drive management"
        lead = v.get("remediation") or ""
    else:
        headline = "Drive-sync tripwire could not evaluate"
        lead = ("The tripwire that guards C:\\TradingDesk-Local against Google Drive "
                "sync/backup management could NOT complete a check this run, so it is "
                "FAILING CLOSED rather than going silent. Investigate — the guard is not "
                "currently proving the folder is safe. " + v.get("remediation", ""))

    reasons = v.get("reasons") or []
    reasons_html = "".join(
        f'<div style="padding:2px 0;color:#111827;">{r}</div>' for r in reasons)
    protected = ", ".join(v.get("protected") or [])
    cause = (f'<b>{headline}.</b> {lead}'
             + (f'<div style="font-size:12px;color:#6b7280;margin-top:6px;">Findings:</div>'
                f'{reasons_html}' if reasons else ''))
    problem = {
        "label": "Drive-sync tripwire (Google Drive corruption risk)",
        "status": kind,
        "cause": cause,
        "rows": [("Kind", kind), ("Protected", protected)],
    }
    return f"{name}: {kind} — OUTSTANDING (folded into daily digest)", problem


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


def _write_ran_marker(now: float) -> None:
    """Atomically stamp RAN_MARKER with the current time — proof the alarm itself is
    alive (read by eod_report.build_alarm for mutual watchdog coverage). Never raises."""
    try:
        RAN_MARKER.parent.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.fromtimestamp(now).isoformat(timespec="seconds")
        tmp = RAN_MARKER.with_name(RAN_MARKER.name + ".tmp")
        tmp.write_text(stamp + "\n")
        os.replace(tmp, RAN_MARKER)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Assessment
# --------------------------------------------------------------------------- #
def _progress_complete(progress_path) -> bool:
    """True if progress.json shows the job legitimately finished.

    Handles both progress schemas: the SPXW collector's days_done/days_total AND the
    universe downloader's done_units/total_units (or its explicit 'complete': true flag)."""
    if not progress_path:
        return False
    try:
        p = json.loads(Path(progress_path).read_text())
    except (OSError, ValueError):
        return False
    if p.get("complete") is True:
        return True
    done = p.get("days_done", p.get("done", p.get("done_units")))
    total = p.get("days_total", p.get("total", p.get("total_units")))
    try:
        return total is not None and done is not None and int(done) >= int(total)
    except (TypeError, ValueError):
        return False


def _progress_fresh(progress_path, now: float, threshold_s: float) -> bool:
    """True if the progress file exists and its mtime is within the staleness
    threshold. Used to gate 'complete'-flag suppression when there is no heartbeat
    file: a leftover complete flag on a STALE progress file must not suppress."""
    if not progress_path:
        return False
    try:
        return (now - Path(progress_path).stat().st_mtime) < threshold_s
    except OSError:
        return False


def _progress_pct(progress_path) -> str:
    """A '6.47% (25866/399600)' progress string for the alert body, or 'unknown'.

    Reads both progress schemas: the SPXW collector's days_done/days_total AND the
    universe downloader's done/total (its actual keys; done_units/total_units also
    accepted defensively). The 2026-07-05 audit found this only read days_* and so
    showed 'progress unknown' for the universe job — fixed here."""
    if not progress_path:
        return "unknown"
    try:
        p = json.loads(Path(progress_path).read_text())
    except (OSError, ValueError):
        return "unknown"
    done = p.get("days_done", p.get("done", p.get("done_units")))
    total = p.get("days_total", p.get("total", p.get("total_units")))
    pct = p.get("pct")
    unit = "days" if p.get("days_total") is not None else "units"
    if done is not None and total is not None:
        pct_str = f"{pct}%" if pct is not None else "?"
        return f"{pct_str} ({done}/{total} {unit})"
    return "unknown"


def _missing_grace_active(job: dict, now: float) -> tuple[bool, str | None]:
    """First-run grace for the ABSENT-heartbeat path ONLY.

    A newly-activated job whose FIRST scheduled run simply hasn't come due yet has a
    legitimately absent heartbeat — paging on it is a false 'never ran' alarm. An
    OPTIONAL job field `missing_ok_until` (ISO-8601 with tz offset) says "while now <
    that instant, an absent heartbeat is not-yet-due, not a failure." This does NOT
    weaken the alert — once now >= missing_ok_until an absent heartbeat pages exactly
    as before.

    Returns (grace_active, until_iso). FAILS SAFE toward the existing behaviour: if the
    field is missing OR unparseable, returns (False, ...) so the job pages as it does
    today. NEVER raises — the alarm must never crash the run, and an unparseable grace
    must never accidentally suppress a real alert."""
    raw = job.get("missing_ok_until")
    if not raw:
        return False, None
    try:
        parsed = dt.datetime.fromisoformat(str(raw))
        grace_ts = parsed.timestamp()  # tz-aware -> correct POSIX instant
    except (ValueError, TypeError, OverflowError, OSError):
        # Unparseable -> fail safe to today's behaviour (no grace -> page).
        return False, None
    return (now < grace_ts), str(raw)


def assess(job: dict, now: float, heartbeat_override: str | None = None) -> dict:
    """Decide one job's status WITHOUT side effects.

    Returns a dict:
      status : 'fresh' | 'complete' | 'stale' | 'missing' | 'pending_first_run'
      alert  : bool  (True only when status == 'stale')
      age_s  : float | None   seconds since the heartbeat was last written
      last_ts: str            last heartbeat text line (or a marker)
      hb_path: str            the heartbeat path actually used
      progress: str           human progress string
    """
    hb_path = Path(heartbeat_override) if heartbeat_override else Path(job["heartbeat"])
    progress = _progress_pct(job.get("progress"))

    if not hb_path.exists():
        # No heartbeat file at all. A 'complete' progress flag is only a legit finish
        # if the progress file that carries it is itself FRESH (written within the
        # threshold). A STALE complete flag beside an absent heartbeat is the same
        # silent-death trap as a cold heartbeat — it must NOT suppress. Otherwise the
        # job never started / vanished.
        if _progress_complete(job.get("progress")) and _progress_fresh(
                job.get("progress"), now, job["threshold_s"]):
            return {"status": "complete", "alert": False, "age_s": None,
                    "last_ts": "(no heartbeat file; progress shows complete)",
                    "hb_path": str(hb_path), "progress": progress}
        # First-run grace (ABSENT path ONLY): a newly-activated job whose first
        # scheduled run hasn't come due yet has a legitimately absent heartbeat. While
        # now < missing_ok_until this is "not yet due", not a failure — so DON'T page.
        # Fails safe (no/unparseable field -> not active -> pages as today). This never
        # touches the COLD/STALE path below: a job that ran once and went cold is a real
        # failure and still pages regardless of missing_ok_until.
        grace_active, grace_until = _missing_grace_active(job, now)
        if grace_active:
            return {"status": "pending_first_run", "alert": False, "age_s": None,
                    "last_ts": ("(heartbeat absent; first scheduled run not yet due — "
                                f"grace until {grace_until})"),
                    "hb_path": str(hb_path), "progress": progress}
        return {"status": "missing", "alert": True, "age_s": None,
                "last_ts": "(heartbeat file absent)",
                "hb_path": str(hb_path), "progress": progress}

    try:
        text = hb_path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        text = ""
    last_line = text.splitlines()[-1].strip() if text else "(empty heartbeat)"

    # Freshness FIRST — mtime is the file's actual last-write time (the supervisor
    # rewrites the whole file every ~30s, so mtime == last heartbeat). This MUST gate
    # the completion suppressions below: a "COMPLETE" marker or a progress 'complete'
    # flag is only trustworthy while the file that carries it is FRESH. A STALE
    # complete flag (left over from a prior scope/run) beside a COLD heartbeat means
    # the job died — it must NOT suppress the alert.
    #
    # THE 2026-07-05 SILENT DEATH THIS PREVENTS: during a ~3.75h terminal outage the
    # supervisor was NOT running, yet a stale progress.json still carried a leftover
    # 'complete: true'. The old ordering checked completion BEFORE freshness, so the
    # alarm logged "universe_dl: COMPLETE — no alert" for hours over a dead job — the
    # exact unnoticed-death the alarm exists to catch. Freshness now decides first.
    try:
        age = now - hb_path.stat().st_mtime
    except OSError:
        age = None

    if age is None:
        return {"status": "missing", "alert": True, "age_s": None,
                "last_ts": last_line, "hb_path": str(hb_path), "progress": progress}

    fresh = age < job["threshold_s"]

    # Completion suppressions apply ONLY when the heartbeat is FRESH. A recently
    # finished job (fresh file + COMPLETE marker or progress complete) is a legit
    # finish and is correctly suppressed here; a cold file with either flag is NOT.
    if fresh:
        # Completion suppression #1: the literal COMPLETE marker.
        if "COMPLETE" in text.upper():
            return {"status": "complete", "alert": False, "age_s": age,
                    "last_ts": last_line, "hb_path": str(hb_path), "progress": progress}
        # Completion suppression #2: progress.json says every unit is done.
        if _progress_complete(job.get("progress")):
            return {"status": "complete", "alert": False, "age_s": age,
                    "last_ts": last_line, "hb_path": str(hb_path), "progress": progress}
        return {"status": "fresh", "alert": False, "age_s": age,
                "last_ts": last_line, "hb_path": str(hb_path), "progress": progress}

    # Cold heartbeat: ALERT regardless of any stale COMPLETE / complete flag.
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


def _job_cause(job: dict, a: dict) -> str:
    """The cause/remediation sentence for a stale/missing job — the SAME wording the old
    per-problem alert carried, so the daily digest reads identically. Shared by
    _build_alert and _job_problem (the digest path)."""
    age = _fmt_age(a["age_s"])
    # Default wording assumes a SUPERVISED collector writing every ~30s. That is true
    # of the SPXW-style jobs this alarm was born for, but false — and misleading — for
    # jobs with other shapes (e.g. a once-daily repo backup). Such a job supplies its
    # own cause_stale/cause_missing text; everyone else keeps the original default.
    cause = (f"The supervisor/process appears to have DIED (heartbeat has not "
             f"updated in {age}, well past the {job['threshold_s'] // 60}-minute "
             f"staleness threshold — the supervisor writes every ~30s, so this is "
             f"not a mere stall). Check Windows Task Scheduler task "
             f"<b>{job['task_name']}</b> and restart it if needed.")
    if job.get("cause_stale"):
        cause = job["cause_stale"].format(age=age, task_name=job["task_name"])
    if a["status"] == "missing":
        cause = (f"The heartbeat file is ABSENT — the job may never have started. "
                 f"Check Windows Task Scheduler task <b>{job['task_name']}</b>.")
        if job.get("cause_missing"):
            cause = job["cause_missing"].format(age=age, task_name=job["task_name"])
    return cause


def _job_rows(job: dict, a: dict) -> list[tuple[str, str]]:
    """The detail rows for a stale/missing job (shared by _build_alert + the digest)."""
    return [
        ("Status", a["status"].upper()),
        ("Last heartbeat", a["last_ts"]),
        ("Age (cold for)", _fmt_age(a["age_s"])),
        ("Progress", a["progress"]),
        ("Heartbeat file", a["hb_path"]),
        ("Owning task", job["task_name"]),
    ]


def _job_problem(job: dict, a: dict) -> dict:
    """A digest problem descriptor for a stale/missing job."""
    return {
        "label": job["label"],
        "status": a["status"].upper(),
        "cause": _job_cause(job, a),
        "rows": _job_rows(job, a),
    }


def _build_alert(job: dict, a: dict) -> tuple[str, str]:
    """(subject, html_body) for a stale/missing job. Retained for reuse/back-compat;
    the live path now folds problems into the daily digest (see _build_digest)."""
    age = _fmt_age(a["age_s"])
    subject = f"[TradingDesk ALARM] {job['label']} heartbeat cold {age}"
    cause = _job_cause(job, a)
    rows = [("Job", job["label"])] + _job_rows(job, a)
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
def handle_job(job: dict, state: dict, now: float,
               heartbeat_override: str | None = None) -> tuple[str, dict | None]:
    """Assess one rolling-freshness job WITHOUT sending. Returns (one-line status,
    problem|None); a non-None problem (stale/missing) is folded into the daily
    consolidated digest by main(). Recovery clears any legacy cooldown key so state
    stays clean and logs stay accurate."""
    a = assess(job, now, heartbeat_override=heartbeat_override)
    name = job["name"]
    js = state.setdefault(name, {})
    age = _fmt_age(a["age_s"])

    if not a["alert"]:
        # Recovered / fresh / complete -> clear any legacy cooldown so state stays clean.
        if js.pop("last_alert_ts", None) is not None:
            log(f"{name}: recovered ({a['status']}) — cleared alert cooldown")
        return (f"{name}: {a['status'].upper()} (age {age}, "
                f"progress {a['progress']}) — no alert"), None

    # OUTSTANDING (stale/missing) — build a problem for the digest, no immediate send.
    return (f"{name}: {a['status'].upper()} (age {age}) — OUTSTANDING "
            f"(folded into daily digest)"), _job_problem(job, a)


# --------------------------------------------------------------------------- #
# Consolidated daily digest (one email/day, silent overnight)
# --------------------------------------------------------------------------- #
def _build_digest(problems: list[dict], now_dt: "dt.datetime") -> tuple[str, str]:
    """(subject, html_body) for ONE consolidated email listing every outstanding
    problem. Reuses each problem's own cause/remediation wording and detail rows, so
    the digest reads exactly like the old per-problem alerts did — just batched."""
    n = len(problems)
    noun = "item" if n == 1 else "items"
    verb = "needs" if n == 1 else "need"
    subject = f"[TradingDesk] Overnight status — {n} {noun} {verb} attention"

    sections = []
    for p in problems:
        rows_html = "".join(
            f'<tr><td style="padding:2px 14px 2px 0;color:#6b7280;white-space:nowrap;'
            f'vertical-align:top;">{k}</td>'
            f'<td style="padding:2px 0;color:#111827;">{v}</td></tr>'
            for k, v in p.get("rows", []))
        sections.append(
            f'<div style="border-left:3px solid #ef4444;padding:2px 0 8px 12px;'
            f'margin:14px 0;">'
            f'<div style="font-size:15px;font-weight:700;color:#111827;">'
            f'{p["label"]} '
            f'<span style="font-size:12px;font-weight:600;color:#ef4444;">'
            f'[{p["status"]}]</span></div>'
            f'<div style="font-size:13px;color:#374151;margin:6px 0;">{p["cause"]}</div>'
            f'<table style="border-collapse:collapse;font-size:13px;">{rows_html}</table>'
            f'</div>')

    html = (
        f'<div style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;'
        f'max-width:640px;margin:0 auto;color:#111827;">'
        f'<div style="font-size:18px;font-weight:700;color:#ef4444;">'
        f'&#9679; TradingDesk overnight status — {n} {noun} {verb} attention</div>'
        f'<div style="font-size:12px;color:#6b7280;margin:6px 0 2px;">'
        f'Consolidated once-daily digest as of {now_dt:%Y-%m-%d %H:%M} local. Items that '
        f'cleared before this morning are not listed. This is the ONLY alarm email today.'
        f'</div>'
        f'{"".join(sections)}'
        f'<div style="font-size:11px;color:#9ca3af;margin-top:12px;">'
        f'Automated daily digest · TradingDesk\\datacollector\\heartbeat_alarm.py</div></div>')
    return subject, html


def maybe_send_digest(state: dict, now: float, problems: list[dict],
                      dry_run: bool) -> str:
    """Send at most ONE consolidated email per calendar day, only inside the morning
    delivery window [DIGEST_SEND_HOUR, DIGEST_WINDOW_END_HOUR), and only when there is
    >=1 outstanding problem. Otherwise assess+log only (overnight is silent). Returns a
    one-line status for the sweep log. Records last_sent_date only on a real (non-dry)
    successful send."""
    now_dt = dt.datetime.fromtimestamp(now)
    hour = now_dt.hour
    today = now_dt.strftime("%Y%m%d")
    dstate = state.setdefault(_DIGEST_STATE_KEY, {})
    n = len(problems)

    in_window = DIGEST_SEND_HOUR <= hour < DIGEST_WINDOW_END_HOUR
    already_today = dstate.get("last_sent_date") == today

    if not in_window:
        return (f"digest: {n} outstanding — OUTSIDE send window "
                f"({hour:02d}:xx not in [{DIGEST_SEND_HOUR:02d},"
                f"{DIGEST_WINDOW_END_HOUR:02d})) — no email (silent)")
    if already_today:
        return (f"digest: {n} outstanding — already sent today ({today}) — no email")
    if n == 0:
        return ("digest: 0 outstanding — nothing to send")

    subject, html = _build_digest(problems, now_dt)
    if dry_run:
        log(f"WOULD-SEND: {subject}")
        for p in problems:
            log(f"  WOULD-SEND item: {p['label']} [{p['status']}]")
        # Dry-run never records last_sent_date (repeat self-tests stay identical).
        return f"digest: {n} outstanding — WOULD-SEND (dry-run, {n} item(s))"

    sent = _send(subject, html)
    if sent:
        dstate["last_sent_date"] = today
        return f"digest: {n} outstanding — SENT (recorded last_sent_date={today})"
    return f"digest: {n} outstanding — SEND FAILED (will retry next run)"


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
    problems: list[dict] = []  # currently-outstanding items -> consolidated digest

    for i, job in enumerate(JOBS):
        override = args.test_stale if (args.test_stale and i == 0) else None
        try:
            line, problem = handle_job(job, state, now, heartbeat_override=override)
            if problem:
                problems.append(problem)
        except Exception as e:  # noqa: BLE001 — one bad job must not kill the sweep
            line = f"{job.get('name', '?')}: CHECK ERROR — {type(e).__name__}: {e}"
        lines.append(line)
        log(line)

    for job in DEADLINE_JOBS:
        try:
            line, problem = handle_deadline(job, state, now)
            if problem:
                problems.append(problem)
        except Exception as e:  # noqa: BLE001 — one bad check must not kill the sweep
            line = f"{job.get('name', '?')}: CHECK ERROR — {type(e).__name__}: {e}"
        lines.append(line)
        log(line)

    # Drive-sync tripwire (state assertion, not staleness). Same sweep; folded into the
    # SAME daily digest per the one-email-a-day rule (overnight-exception open decision).
    try:
        line, problem = handle_tripwire(state, now)
        if problem:
            problems.append(problem)
    except Exception as e:  # noqa: BLE001 — one bad check must not kill the sweep
        line = f"{_TRIPWIRE_NAME}: CHECK ERROR — {type(e).__name__}: {e}"
    lines.append(line)
    log(line)

    # EMAIL GATE: at most one consolidated digest per day, only in the morning window.
    digest_line = maybe_send_digest(state, now, problems, args.dry_run)
    lines.append(digest_line)
    log(digest_line)

    if not args.dry_run:
        _save_state(state)

    # Mutual-watchdog proof-of-life: record that THIS alarm actually ran, so the EOD
    # digest (which the user reads) can turn red if the alarm itself dies. Atomic
    # write, wrapped so it can never raise / slow the sweep.
    _write_ran_marker(now)

    # One consolidated status line every run (in addition to per-job lines).
    log("sweep done: " + " | ".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
