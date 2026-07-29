"""eventlog.py — the desk dashboard's DURABLE, plain-English event store.

This is the ONE module on the desk dashboard allowed to WRITE — and it writes to
exactly one place: its own audit database at
``C:\\TradingDesk-Local\\state\\desk_dashboard\\events.db``. It NEVER touches the
trading warehouse, any strategy/regime config, the capture store, or the gateway
arming state. It only reads log files and Windows Task Scheduler (both read-only)
and records what it finds as permanent, human-readable events.

Why it exists: the raw S8 logs and tick parts roll off / get large, but the owner
(a non-coder) needs a lasting, plain-English record of what actually happened —
morning logins, start-ups, gateway downtime (with how long and why), and the
pilot's "would-have-traded" decisions. ``scan()`` is idempotent: every event has a
stable key, so re-running it as logs grow just adds the genuinely new lines and
the history accumulates forever even after the source logs disappear.

PLAIN-ENGLISH RULE (#1): every stored ``message`` is a full, non-technical
sentence. Severity carries color on the page; the words stand on their own.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

# --------------------------------------------------------------------------- #
# Locations (all off-Drive, on local C:). The DB is the ONLY thing we write.   #
# --------------------------------------------------------------------------- #
DB_DIR = Path(r"C:\TradingDesk-Local\state\desk_dashboard")
DB_PATH = DB_DIR / "events.db"
S8_LOG_DIR = Path(r"C:\TradingDesk-Local\s8_pilot\logs")

# A gateway-down stretch shorter than this is just normal start-up jitter and is
# not worth a downtime event. Above it, we record the window with duration+reason.
_MIN_DOWNTIME_SECS = 120


# --------------------------------------------------------------------------- #
# 0. Database plumbing.                                                         #
# --------------------------------------------------------------------------- #
def _connect() -> sqlite3.Connection:
    """Open (creating the folder + table on first use) the audit DB. WRITE target
    is ONLY this file — never any trading store."""
    DB_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH))
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            event_key TEXT UNIQUE,
            ts        TEXT,
            day       TEXT,
            source    TEXT,
            category  TEXT,
            severity  TEXT,
            message   TEXT
        )
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS ix_events_day ON events(day)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts)")
    return con


def _event_key(source: str, ts: str, message: str) -> str:
    """Stable hash of (source, ts, message) so re-inserts are idempotent."""
    raw = f"{source}\x1f{ts}\x1f{message}".encode("utf-8", "replace")
    return hashlib.sha1(raw).hexdigest()[:20]


def record_event(ts: str, source: str, category: str, message: str,
                 severity: str = "info", day: str | None = None) -> bool:
    """Idempotently record ONE event. Returns True if it was newly inserted,
    False if an identical (source, ts, message) event already existed.

    Other desk modules (e.g. a future emergency.py) call this to log an action
    they took — it writes only to events.db, nothing else."""
    ts = str(ts)
    if day is None:
        day = _day_from_ts(ts)
    key = _event_key(source, ts, message)
    try:
        con = _connect()
        try:
            cur = con.execute(
                "INSERT OR IGNORE INTO events "
                "(event_key, ts, day, source, category, severity, message) "
                "VALUES (?,?,?,?,?,?,?)",
                (key, ts, day, source, category, severity, message),
            )
            con.commit()
            return cur.rowcount == 1
        finally:
            con.close()
    except Exception:
        return False


def _day_from_ts(ts: str) -> str:
    """'YYYY-MM-DD HH:MM:SS' (or similar) -> 'YYYYMMDD'. Best-effort."""
    digits = re.sub(r"[^0-9]", "", str(ts))
    return digits[:8] if len(digits) >= 8 else datetime.now().strftime("%Y%m%d")


# --------------------------------------------------------------------------- #
# 1. Small plain-English time helpers.                                         #
# --------------------------------------------------------------------------- #
def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _day(dt: datetime) -> str:
    return dt.strftime("%Y%m%d")


def fmt_clock(dt: datetime, *, seconds: bool = False) -> str:
    """A friendly 12-hour Central clock string, e.g. '8:05 AM' / '9:41:02 AM'."""
    hour12 = dt.hour % 12 or 12
    ampm = "AM" if dt.hour < 12 else "PM"
    if seconds:
        return f"{hour12}:{dt.minute:02d}:{dt.second:02d} {ampm}"
    return f"{hour12}:{dt.minute:02d} {ampm}"


def _minutes_phrase(secs: float) -> str:
    m = int(round(secs / 60.0))
    if m <= 1:
        return "about a minute"
    if m < 60:
        return f"about {m} minutes"
    h = m // 60
    rem = m % 60
    if rem == 0:
        return f"about {h} hour{'s' if h != 1 else ''}"
    return f"about {h} hour{'s' if h != 1 else ''} {rem} minutes"


# --------------------------------------------------------------------------- #
# 2. S8 pilot log parsing -> plain-English events.                             #
# --------------------------------------------------------------------------- #
# A bracketed batch-file header carries the only reliable wall-clock timestamps
# in these logs, e.g. "[Wed 07/29/2026  8:05:01.98] run_s8_service.cmd START ...".
_HEADER_RE = re.compile(
    r"\[\w{3}\s+(\d{2})/(\d{2})/(\d{4})\s+(\d{1,2}):(\d{2}):(\d{2})")
# An ISO-ish leading timestamp (reap/other tools): "2026-07-29 08:35:02 ...".
_ISO_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})")
# Intraday slot times embedded in trade ids ("...:09:15:...") and would-trade
# tags ("[Puts-80-$4@08:45]") let us anchor events to the actual trading minute.
_SLOT_ID_RE = re.compile(r"\b\d{8}:[^\s:]+:(\d{2}):(\d{2}):")
_SLOT_AT_RE = re.compile(r"@(\d{2}):(\d{2})\]")
_ACCEPTED_RE = re.compile(r"accepted the connection after (\d+) attempt", re.I)
_WOULD_RE = re.compile(r"WOULD HAVE TRANSMITTED:\s*(.*)$", re.I)
_WOULD_TAG_RE = re.compile(r"\[([A-Za-z]+)-[^\]]*@(\d{2}):(\d{2})\]")
_WOULD_FIELDS_RE = re.compile(
    r"(Puts|Calls)\b.*?short=(\d+)/long=(\d+)\s+qty=(\d+).*?stop=([\d.]+)", re.I)


def _date_from_name(path: Path) -> datetime:
    m = re.search(r"(\d{8})", path.name)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y%m%d")
        except ValueError:
            pass
    try:
        return datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return datetime.now()


def _advance_to_slot(base_day: datetime, cur: datetime, hh: int, mm: int) -> datetime:
    """Return a datetime on base_day at hh:mm if it's not earlier than cur (times
    only move forward through a session); otherwise keep cur."""
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return cur
    cand = base_day.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return cand if cand >= cur else cur


def _parse_service_log(path: Path, source: str) -> list[dict]:
    """Turn one S8 *service* log into plain events: morning connect, the morning
    downtime window (collapsed), clean end-of-day shutdown, and pilot would-trade
    decisions. Unknown / noisy lines (per-tick, 'harvested', cancelMktData) are
    ignored."""
    base_day = _date_from_name(path)
    cur = base_day  # carried wall-clock; refined by headers + slot anchors
    events: list[dict] = []

    waiting_active = False
    waiting_start: datetime | None = None
    session_started = False
    seen_would: set[str] = set()
    # When the gateway "accepted", the accept line itself carries no clock — the
    # governing header is the last START (e.g. 8:35), but the N retries actually
    # ran on past it (the 8:45 watchdog proves it). So we DEFER the connect +
    # downtime-window events until the next real activity anchor (first trade slot
    # / next header) reveals a truer "back up" time — bounded so a trade-less day
    # can't push it hours later.
    pending: dict | None = None
    _REFINE_MAX_SECS = 1800  # don't refine the connect time more than 30 min forward

    def _emit_connect(connect_dt: datetime, info: dict) -> None:
        attempts = info["attempts"]
        if attempts <= 1:
            retry_phrase = "and it answered right away"
        else:
            retry_phrase = f"after {attempts} tries to wake it up"
        events.append({
            "ts": _ts(connect_dt), "day": _day(connect_dt), "source": source,
            "category": "gateway", "severity": "good",
            "message": (
                "Live trading gateway connected for the day "
                f"({retry_phrase} following the morning login)."
            ),
        })
        w_start = info.get("waiting_start")
        if w_start is not None:
            downtime = (connect_dt - w_start).total_seconds()
            if downtime >= _MIN_DOWNTIME_SECS:
                events.append({
                    "ts": _ts(w_start), "day": _day(w_start),
                    "source": source, "category": "gateway", "severity": "warn",
                    "message": (
                        "Live trading gateway was not accepting connections "
                        f"from {fmt_clock(w_start)} to {fmt_clock(connect_dt)} "
                        f"Central ({_minutes_phrase(downtime)}) — it was waking "
                        "up after the morning login, and reconnected on its own."
                    ),
                })

    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return events

    for line in lines:
        # 1) Refresh the carried timestamp from any reliable clock on this line.
        mh = _HEADER_RE.search(line)
        if mh:
            _mo, _da, _yr, _h, _mi, _se = (int(x) for x in mh.groups())
            try:
                cur = datetime(_yr, _mo, _da, _h, _mi, _se)
            except ValueError:
                pass
        else:
            mi = _ISO_RE.match(line)
            if mi:
                _yr, _mo, _da, _h, _min, _se = (int(x) for x in mi.groups())
                try:
                    cur = datetime(_yr, _mo, _da, _h, _min, _se)
                except ValueError:
                    pass
            else:
                ms = _SLOT_ID_RE.search(line) or _SLOT_AT_RE.search(line)
                if ms:
                    cur = _advance_to_slot(base_day, cur,
                                           int(ms.group(1)), int(ms.group(2)))

        # 1b) If a connect is pending, the first later anchor gives a truer "back
        #     up" time (bounded); emit connect + downtime then.
        if pending is not None and cur > pending["lower"]:
            forward = (cur - pending["lower"]).total_seconds()
            connect_dt = cur if forward <= _REFINE_MAX_SECS else pending["lower"]
            _emit_connect(connect_dt, pending)
            pending = None

        # 2) Session start (first START header of the file).
        if not session_started and "run_s8_service.cmd START" in line:
            session_started = True
            events.append({
                "ts": _ts(cur), "day": _day(cur), "source": source,
                "category": "session", "severity": "info",
                "message": (
                    "Strategy 8 pilot session started for the day — the all-day "
                    "monitor came up and began watching (it never sends real orders)."
                ),
            })
            continue

        # 3) Track the morning "not accepting connections" stretch.
        if "not accepting API connections" in line:
            if not waiting_active:
                waiting_active = True
                waiting_start = cur
            continue

        # 4) Gateway finally accepted -> DEFER the connect + downtime window until
        #    the next real activity anchor gives a truer "back up" time.
        ma = _ACCEPTED_RE.search(line)
        if ma:
            info = {"attempts": int(ma.group(1)),
                    "lower": cur,
                    "waiting_start": waiting_start if waiting_active else None}
            if pending is not None:
                # A prior accept never got refined (no anchor before this one) —
                # emit it at its lower bound before starting the new pending.
                _emit_connect(pending["lower"], pending)
            pending = info
            waiting_active = False
            waiting_start = None
            continue

        # 5) Clean end-of-day shutdown.
        if "run_s8_service.cmd EXIT rc=0" in line:
            events.append({
                "ts": _ts(cur), "day": _day(cur), "source": source,
                "category": "session", "severity": "info",
                "message": (
                    "Strategy 8 pilot session ended cleanly at the end of the "
                    "trading day — the monitor shut itself down as scheduled."
                ),
            })
            continue

        # 6) Pilot would-have-traded decision (NO real order ever sent).
        mw = _WOULD_RE.search(line)
        if mw:
            detail = mw.group(1).strip()
            slot_dt = cur
            tag = _WOULD_TAG_RE.search(line)
            if tag:
                slot_dt = _advance_to_slot(base_day, base_day,
                                           int(tag.group(2)), int(tag.group(3)))
            fields = _WOULD_FIELDS_RE.search(detail)
            if fields:
                side, short, long, qty, stop = fields.groups()
                side_word = "puts (below the market)" if side.lower() == "puts" \
                    else "calls (above the market)"
                plain = (
                    f"sell the {side_word} iron condor — short leg {short}, "
                    f"protective long leg {long}, {qty} contract"
                    f"{'s' if qty != '1' else ''}, stop at {stop}"
                )
            else:
                plain = detail
            identity = f"{_day(slot_dt)}|{fields.groups() if fields else detail}"
            if identity in seen_would:
                continue
            seen_would.add(identity)
            events.append({
                "ts": _ts(slot_dt), "day": _day(slot_dt), "source": source,
                "category": "pilot_decision", "severity": "info",
                "message": (
                    "Pilot logged a trade decision but sent NO real order: "
                    f"{plain}."
                ),
            })
            continue

    # A connect that never saw a later anchor (e.g. no trades after it) — emit at
    # its lower-bound time so it is never lost.
    if pending is not None:
        _emit_connect(pending["lower"], pending)

    return events


def _parse_watchdog_log(path: Path, source: str) -> list[dict]:
    """The morning still-down watchdog — turn its alert into a plain event."""
    base_day = _date_from_name(path)
    cur = base_day
    events: list[dict] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return events
    for line in lines:
        mh = _HEADER_RE.search(line)
        if mh:
            _mo, _da, _yr, _h, _mi, _se = (int(x) for x in mh.groups())
            try:
                cur = datetime(_yr, _mo, _da, _h, _mi, _se)
            except ValueError:
                pass
        if "NOT confirmed up" in line or "alert sent=True" in line:
            events.append({
                "ts": _ts(cur), "day": _day(cur), "source": source,
                "category": "gateway", "severity": "warn",
                "message": (
                    "Morning safety check found the live trading gateway still "
                    f"not up at {fmt_clock(cur)} Central, so an alert email was "
                    "sent (this is the reminder to approve the morning login)."
                ),
            })
            break  # one alert per watchdog run is enough
    return events


def parse_s8_logs() -> list[dict]:
    """Read the S8 pilot logs and return plain-English event dicts (not yet
    stored). Degrades to [] if the log folder is missing/unreadable."""
    out: list[dict] = []
    try:
        files = sorted(S8_LOG_DIR.glob("*.log"))
    except OSError:
        return out
    for path in files:
        name = path.name
        try:
            if name.startswith("s8_service_") and re.search(r"\d{8}", name):
                out.extend(_parse_service_log(path, "Strategy 8 pilot"))
            elif name.startswith("s8_morning_watchdog_"):
                out.extend(_parse_watchdog_log(path, "Morning safety check"))
            # collector / reap logs are tick-write + cleanup noise -> skipped.
        except Exception:
            continue
    return out


# --------------------------------------------------------------------------- #
# 3. Windows Task Scheduler history -> plain-English events.                    #
# --------------------------------------------------------------------------- #
def parse_task_history() -> list[dict]:
    """Best-effort: the most recent run of each desk-relevant scheduled task as a
    plain event ('Nightly status email ran successfully at 9:00 PM'). Degrades to
    [] on any failure (no PowerShell, no match, bad output)."""
    import json
    import subprocess

    # Reuse deskdata's curated task -> plain-description map + result phrasing.
    try:
        import deskdata as dd
        desc_map = {n: d for _g, tasks in dd.TASK_GROUPS for n, d in tasks}
        result_phrase = dd._result_to_phrase
    except Exception:
        return []

    names = list(desc_map.keys())
    if not names:
        return []
    filt = "|".join(re.escape(n) for n in names)
    cmd = (
        "Get-ScheduledTask | Where-Object { $_.TaskName -match '" + filt + "' } | "
        "ForEach-Object { $i = $_ | Get-ScheduledTaskInfo; "
        "[PSCustomObject]@{ TaskName = $_.TaskName; "
        "LastTaskResult = $i.LastTaskResult; "
        "LastRunTime = if ($i.LastRunTime) "
        "{ $i.LastRunTime.ToString('yyyy-MM-dd HH:mm:ss') } else { '' } } } "
        "| ConvertTo-Json -Compress"
    )
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", cmd],
            capture_output=True, text=True, timeout=25)
        data = json.loads(proc.stdout) if proc.stdout.strip() else []
    except Exception:
        return []
    if isinstance(data, dict):
        data = [data]

    events: list[dict] = []
    for d in data:
        name = d.get("TaskName")
        run = str(d.get("LastRunTime") or "").strip()
        if not name or not run:
            continue
        try:
            run_dt = datetime.strptime(run[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if run_dt.year < 2000:
            continue
        phrase = result_phrase(d.get("LastTaskResult"))
        if "successfully" in phrase or "ran successfully" in phrase:
            severity = "good"
        elif "error" in phrase:
            severity = "bad"
        elif "already running" in phrase:
            severity = "good"
        else:
            severity = "info"
        desc = desc_map.get(name, name)
        # Trim any parenthetical schedule hint from the description for the sentence.
        short_desc = re.sub(r"\s*\([^)]*\)\s*$", "", desc).strip()
        events.append({
            "ts": _ts(run_dt), "day": _day(run_dt),
            "source": "Scheduled tasks", "category": "scheduled_task",
            "severity": severity,
            "message": (
                f"{short_desc} — {phrase} (last run "
                f"{fmt_clock(run_dt)} Central)."
            ),
        })
    return events


# --------------------------------------------------------------------------- #
# 4. Scan + read.                                                              #
# --------------------------------------------------------------------------- #
def scan() -> int:
    """Run every parser and record each result idempotently. Safe to call as
    often as you like — that's how history keeps growing permanently even as the
    raw logs roll off. Returns the count of NEWLY recorded events."""
    new = 0
    for parser in (parse_s8_logs, parse_task_history):
        try:
            found = parser()
        except Exception:
            found = []
        for ev in found:
            try:
                if record_event(
                    ts=ev["ts"], source=ev["source"], category=ev["category"],
                    message=ev["message"], severity=ev.get("severity", "info"),
                    day=ev.get("day"),
                ):
                    new += 1
            except Exception:
                continue
    return new


def read_events(day: str | None = None, limit: int = 500) -> list[dict]:
    """Return stored events, most recent first. Optionally filter to one day
    ('YYYYMMDD'). Degrades to [] on any failure."""
    try:
        con = _connect()
        try:
            if day:
                rows = con.execute(
                    "SELECT ts, day, source, category, severity, message "
                    "FROM events WHERE day = ? ORDER BY ts DESC LIMIT ?",
                    (day, int(limit)),
                ).fetchall()
            else:
                rows = con.execute(
                    "SELECT ts, day, source, category, severity, message "
                    "FROM events ORDER BY ts DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
        finally:
            con.close()
    except Exception:
        return []
    return [
        {"ts": r[0], "day": r[1], "source": r[2], "category": r[3],
         "severity": r[4], "message": r[5]}
        for r in rows
    ]


def available_days(limit: int = 60) -> list[str]:
    """Distinct days that have at least one event, most recent first."""
    try:
        con = _connect()
        try:
            rows = con.execute(
                "SELECT DISTINCT day FROM events ORDER BY day DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        finally:
            con.close()
    except Exception:
        return []
    return [r[0] for r in rows if r[0]]
