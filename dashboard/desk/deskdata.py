"""deskdata.py — pure, cached, READ-ONLY data layer for the rebuilt desk dashboard.

Nothing in this module places, arms, or transmits an order; nothing writes to any
store, warehouse, or config. Gateway reads are cheap TCP port probes only (the same
socket pattern app.py uses) — never an ib_async connection. Every reader degrades to
an honest, safe value on failure rather than raising.

All user-facing strings are FULL plain-English phrases (the #1 rule): no shorthand,
no bare "OK/DOWN/WARN".
"""
from __future__ import annotations

import json
import socket
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st

CT_ZONE = ZoneInfo("America/Chicago")
STATUS_DIR = Path(r"C:\TradingDesk-Local\state\dailyreport\status")


# --------------------------------------------------------------------------- #
# 0. Cheap TCP port probe (no ib_async, milliseconds) — mirrors app.py.        #
# --------------------------------------------------------------------------- #
def _port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    """'Is something listening?' — a cheap TCP probe. No trading session opened.
    Same pattern as dashboard/app.py::_port_open."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _is_weekend(now: datetime | None = None) -> bool:
    now = now or datetime.now(tz=CT_ZONE)
    return now.weekday() >= 5


def _is_market_hours(now: datetime | None = None) -> bool:
    """Roughly: a weekday between 8:30 AM and 3:00 PM Central (regular session)."""
    now = now or datetime.now(tz=CT_ZONE)
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return (8 * 60 + 30) <= minutes <= (15 * 60)


# --------------------------------------------------------------------------- #
# 1. Gateways.                                                                  #
# --------------------------------------------------------------------------- #
@st.cache_data(ttl=15)
def gateway_status() -> list[dict]:
    """The 3 IBKR gateways as plain-English up/down rows. TCP probe only."""
    now = datetime.now(tz=CT_ZONE)
    weekend = _is_weekend(now)

    def _row(port: int, label: str, down_reason: str) -> dict:
        up = _port_open("127.0.0.1", port)
        return {
            "port": port,
            "label": label,
            "up": up,
            "tier": "good" if up else "bad",
            "phrase": ("Connected and responding" if up else down_reason),
        }

    live_data = _row(
        4001, "Live market-data gateway (port 4001)",
        "Not responding — used for the evening data pulls",
    )
    if weekend:
        paper_down = "Not responding — expected outside market hours / on weekends"
    elif _is_market_hours(now):
        paper_down = "Not responding — should be up now (weekday market hours)"
    else:
        paper_down = "Not responding — expected outside market hours"
    paper = _row(4002, "Paper trading gateway (port 4002)", paper_down)
    # A down paper gateway outside market hours is expected, not a red alarm.
    if not paper["up"] and (weekend or not _is_market_hours(now)):
        paper["tier"] = "unknown"

    live_trade = _row(
        4003, "Live trading gateway (port 4003)",
        "Not responding — expected when the S8 pilot session is closed",
    )
    if not live_trade["up"]:
        live_trade["tier"] = "unknown"

    return [live_data, paper, live_trade]


# --------------------------------------------------------------------------- #
# 2. Live scheduled tasks (curated) with plain-English state.                  #
# --------------------------------------------------------------------------- #
TASK_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    ("S8 live pilot (0-days-to-expiry iron condors, zero real orders)", [
        ("LiveTradeGatewayOpen_0800CT",
         "Opens the live trading gateway each morning (8:00 AM Central)"),
        ("S8UnifiedService_Session",
         "S8 all-day strategy service — schedules and monitors trades, "
         "transmits nothing (pilot)"),
        ("S8Collector_Session",
         "S8 market-data collector — records live option quotes all day"),
        ("S8SessionTeardown",
         "Closes the S8 session and gateway at end of day (3:05 PM Central)"),
        ("S8MorningStillDownAlarm_0845CT",
         "Alarm if the S8 pilot is still down at 8:45 AM Central"),
    ]),
    ("Nightly data & report chain", [
        ("IbkrForwardEodDaily",
         "Nightly options end-of-day data pull from Interactive Brokers "
         "(5:30 PM Central)"),
        ("TiingoDailyUpdate",
         "Nightly stock end-of-day price pull for Strategy 0 "
         "(Tiingo; 7:00 & 8:45 PM Central)"),
        ("GexDailyBuild",
         "Nightly dealer gamma-exposure feature build (7:30 PM Central)"),
        ("EodReport",
         "Nightly end-of-day status email (9:00 PM Central)"),
        ("HeartbeatStalenessAlarm",
         "Watchdog — alerts if any data feed goes stale (every 15 minutes)"),
    ]),
    ("Gateways, backup & Strategy 0", [
        ("LiveDataGatewayEnsureUp_1720CT",
         "Makes sure the market-data gateway is up before the evening pull "
         "(5:20 PM Central)"),
        ("GatewayArmRestart",
         "Restarts and re-arms the gateway when needed"),
        ("RepoBackupDaily",
         "Nightly code backup to Google Drive (8:00 PM Central)"),
        ("MorningExecuteDaily",
         "Morning Strategy 0 execution check"),
    ]),
]

RETIRED_TASKS: list[tuple[str, str]] = [
    ("AccountMonitorDaily",
     "Paper account drift monitor — paused during the account migration"),
    ("GatewayWatchdog", "Old paper-gateway watchdog — retired"),
    ("ThetaEodDaily",
     "ThetaData options pipeline — retired (subscription lapsed, moved to "
     "Interactive Brokers)"),
    ("ThetaTerminalWatchdog",
     "ThetaData options pipeline — retired (subscription lapsed, moved to "
     "Interactive Brokers)"),
    ("ThetaBackfillWatchdog",
     "ThetaData options pipeline — retired (subscription lapsed, moved to "
     "Interactive Brokers)"),
    ("ThetaFinal1mSweep_0724",
     "ThetaData options pipeline — retired (subscription lapsed, moved to "
     "Interactive Brokers)"),
    ("ForwardAbCheck_0724", "One-off data-comparison job — done"),
    ("ForwardLiveEodPull_0724", "One-off data-comparison job — done"),
    ("UniverseDownloadEod", "Old universe download — retired"),
    ("LiveTradeGatewayOpen_0815CT", "Superseded by the 8:00 AM opener"),
]


def _result_to_phrase(code) -> str:
    """Map a Windows Task Scheduler LastTaskResult code to a plain-English phrase."""
    try:
        c = int(code)
    except (TypeError, ValueError):
        return "last-run result unknown"
    mapping = {
        0x0: "ran successfully",
        0x800710E0: "an instance is already running (normal for an all-day service)",
        0x41303: "has not run yet",
        0x41306: "was stopped",
    }
    if c in mapping:
        return mapping[c]
    return f"last run reported an error (code 0x{c & 0xFFFFFFFF:X})"


def _state_to_phrase(state) -> str:
    """Windows task State (int enum or string) to a plain word."""
    enum = {1: "Disabled", 2: "Queued", 3: "Ready", 4: "Running"}
    if isinstance(state, int):
        return enum.get(state, str(state))
    return str(state) if state else "unknown"


@st.cache_data(ttl=30)
def _raw_task_info() -> dict:
    """One PowerShell call for every curated LIVE task: {name: {state, result}}.
    Degrades to {} on any failure."""
    names = [n for _grp, tasks in TASK_GROUPS for n, _d in tasks]
    filt = "|".join(names)
    cmd = (
        "Get-ScheduledTask | Where-Object { $_.TaskName -match '" + filt + "' } | "
        "ForEach-Object { $i = $_ | Get-ScheduledTaskInfo; "
        "[PSCustomObject]@{ TaskName = $_.TaskName; State = $_.State.ToString(); "
        "LastTaskResult = $i.LastTaskResult } } | ConvertTo-Json -Compress"
    )
    try:
        out = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", cmd],
            capture_output=True, text=True, timeout=25)
        data = json.loads(out.stdout) if out.stdout.strip() else []
    except Exception:
        return {}
    if isinstance(data, dict):
        data = [data]
    info: dict = {}
    for d in data:
        info[d.get("TaskName")] = {
            "state": d.get("State"),
            "result": d.get("LastTaskResult"),
        }
    return info


def live_tasks() -> list[dict]:
    """Curated LIVE scheduled tasks, grouped, each with plain description + state
    phrase + tier. Returns a list of {group, tasks:[{name,desc,state,result,phrase,tier}]}."""
    info = _raw_task_info()
    groups: list[dict] = []
    for group_name, tasks in TASK_GROUPS:
        rows = []
        for name, desc in tasks:
            rec = info.get(name)
            if rec is None:
                rows.append({
                    "name": name, "desc": desc,
                    "state": "not found", "result_phrase": "",
                    "phrase": "Not found on this machine",
                    "tier": "unknown",
                })
                continue
            state_word = _state_to_phrase(rec.get("state"))
            result_phrase = _result_to_phrase(rec.get("result"))
            if state_word == "Running":
                phrase = "Running now"
                tier = "good"
            elif state_word == "Disabled":
                phrase = "Turned off (disabled)"
                tier = "unknown"
            elif state_word in ("Ready", "Queued"):
                # Idle/ready — describe by its last outcome.
                phrase = f"Idle — last run {result_phrase}"
                if "error" in result_phrase:
                    tier = "bad"
                elif "has not run yet" in result_phrase:
                    tier = "unknown"
                else:
                    tier = "good"
            else:
                phrase = f"{state_word} — last run {result_phrase}"
                tier = "unknown"
            rows.append({
                "name": name, "desc": desc, "state": state_word,
                "result_phrase": result_phrase, "phrase": phrase, "tier": tier,
            })
        groups.append({"group": group_name, "tasks": rows})
    return groups


def retired_tasks() -> list[dict]:
    """Intentionally-off tasks so the owner can see they're deliberately disabled."""
    return [{"name": n, "reason": r, "state": "Disabled"} for n, r in RETIRED_TASKS]


# --------------------------------------------------------------------------- #
# 3. Data freshness.                                                           #
# --------------------------------------------------------------------------- #
FRESHNESS_FILES: list[tuple[str, str]] = [
    ("tiingo", "Strategy 0 stock prices (Tiingo end-of-day)"),
    ("s0_regime", "Strategy 0 market-regime signal"),
    ("gex", "Dealer gamma-exposure feature build"),
    ("forward", "Options end-of-day data (Interactive Brokers)"),
    ("eod_report", "Nightly status email"),
]

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul",
           "Aug", "Sep", "Oct", "Nov", "Dec"]


def _read_status_json(job: str) -> dict | None:
    p = STATUS_DIR / f"{job}.json"
    try:
        with open(p, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def _parse_date(raw) -> datetime | None:
    """Accept YYYYMMDD, YYYY-MM-DD, or an ISO timestamp; return a date-only datetime."""
    if not raw:
        return None
    s = str(raw).strip()
    try:
        return datetime.strptime(s[:8], "%Y%m%d")
    except Exception:
        pass
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d")
    except Exception:
        pass
    try:
        return datetime.fromisoformat(s[:19])
    except Exception:
        return None


def _fmt_date(d: datetime) -> str:
    return f"{_MONTHS[d.month - 1]} {d.day}"


def _age_days(d: datetime, today: datetime) -> int:
    return (today.date() - d.date()).days


@st.cache_data(ttl=60)
def data_freshness() -> list[dict]:
    """For each tracked status feed: plain label, its data date, how old, and tier.

    Weekend tolerance: on a Monday, Friday's data is still 'fresh' (green)."""
    today = datetime.now(tz=CT_ZONE).replace(tzinfo=None)
    weekday = today.weekday()  # Mon=0
    rows: list[dict] = []
    for job, label in FRESHNESS_FILES:
        js = _read_status_json(job)
        if not js:
            rows.append({
                "label": label, "tier": "bad",
                "phrase": "No status file found — this feed has not reported",
            })
            continue
        d = _parse_date(js.get("date")) or _parse_date(js.get("ts"))
        if d is None:
            rows.append({
                "label": label, "tier": "unknown",
                "phrase": "Reported, but with no readable date",
            })
            continue
        age = _age_days(d, today)
        # Weekend tolerance: Monday accepts Friday (age 3), Sat/Sun (age 1-2).
        fresh_limit = 1
        if weekday == 0:  # Monday
            fresh_limit = 3
        elif weekday == 5:  # Saturday accepts Friday
            fresh_limit = 1
        elif weekday == 6:  # Sunday accepts Friday
            fresh_limit = 2
        if age <= fresh_limit:
            tier = "good"
        elif age <= 4:
            tier = "warn"
        else:
            tier = "bad"
        if age == 0:
            age_word = "today"
        elif age == 1:
            age_word = "1 day ago"
        else:
            age_word = f"{age} days ago"
        rows.append({
            "label": label, "tier": tier,
            "phrase": f"Updated {_fmt_date(d)} ({age_word})",
        })
    return rows


# --------------------------------------------------------------------------- #
# 4. S8 heartbeat.                                                             #
# --------------------------------------------------------------------------- #
@st.cache_data(ttl=20)
def s8_heartbeat() -> dict:
    """Plain-English S8 pilot heartbeat, read-only over the capture store.

    Returns keys: running (bool), trades_today (int), open_now (int),
    last_entry_ts (str|None), port_up (bool), tier, phrase. Degrades safely."""
    port_up = _port_open("127.0.0.1", 4003)
    running = False
    info = _raw_task_info()
    svc = info.get("S8UnifiedService_Session")
    if svc and _state_to_phrase(svc.get("state")) == "Running":
        running = True

    trades_today = 0
    open_now = 0
    last_entry_ts: str | None = None
    read_ok = True
    try:
        import s8_store  # on sys.path via desk_app bootstrap
        records = s8_store.read_trade_records()
        today = datetime.now(tz=CT_ZONE).strftime("%Y%m%d")
        for r in records:
            if r.date != today:
                continue
            trades_today += 1
            if getattr(r, "status", "open") == "open":
                open_now += 1
            e = getattr(r, "entry", None)
            ts = getattr(e, "entry_ts", None) if e else None
            if ts and (last_entry_ts is None or str(ts) > last_entry_ts):
                last_entry_ts = str(ts)
    except Exception:
        read_ok = False

    if not read_ok:
        return {
            "running": running, "trades_today": 0, "open_now": 0,
            "last_entry_ts": None, "port_up": port_up, "tier": "unknown",
            "phrase": ("Could not read the S8 capture store — pilot status "
                       "unavailable right now (no real orders can be sent regardless)"),
        }

    lead = "Running now" if (running or port_up) else "Idle (pilot session not open)"
    phrase = (
        f"{lead} — {trades_today} trade(s) captured today, {open_now} still open, "
        f"0 real orders sent (pilot mode, transmission disabled)"
    )
    tier = "good" if (running or port_up) else "unknown"
    return {
        "running": running, "trades_today": trades_today, "open_now": open_now,
        "last_entry_ts": last_entry_ts, "port_up": port_up,
        "tier": tier, "phrase": phrase,
    }


# --------------------------------------------------------------------------- #
# 5. S0 heartbeat.                                                             #
# --------------------------------------------------------------------------- #
@st.cache_data(ttl=30)
def s0_heartbeat() -> dict:
    """Plain-English S0 heartbeat. S0 is end-of-day driven (no all-day service)
    and in a GATED real-money deploy — transmission is OFF. Read-only."""
    tiingo = _read_status_json("tiingo")
    regime = _read_status_json("s0_regime")
    paper_up = _port_open("127.0.0.1", 4002)

    tiingo_date = None
    if tiingo:
        d = _parse_date(tiingo.get("date"))
        if d:
            tiingo_date = _fmt_date(d)

    regime_fresh = False
    regime_word = "market-regime signal unavailable"
    if regime:
        d = _parse_date(regime.get("date"))
        today = datetime.now(tz=CT_ZONE).replace(tzinfo=None)
        if d is not None and _age_days(d, today) <= 3:
            regime_fresh = True
        conf = (regime.get("metrics", {}) or {}).get("confirmed_regime")
        if conf:
            regime_word = f"market-regime signal: {conf}"

    data_word = (f"data current through {tiingo_date}" if tiingo_date
                 else "latest stock-price date unavailable")
    phrase = (
        f"End-of-day strategy — {data_word}; {regime_word}. "
        f"Real-money transmission is OFF (deliberately gated)."
    )
    return {
        "tiingo_date": tiingo_date,
        "regime_fresh": regime_fresh,
        "paper_up": paper_up,
        "phrase": phrase,
        "tier": "info",
    }


# --------------------------------------------------------------------------- #
# 6. Session/market context helpers (plain-English).                          #
# --------------------------------------------------------------------------- #
def session_context() -> dict:
    now = datetime.now(tz=CT_ZONE)
    weekend = _is_weekend(now)
    market = _is_market_hours(now)
    if weekend:
        phrase = "Weekend — markets are closed"
        tier = "unknown"
    elif market:
        phrase = "Weekday, regular market hours — markets are open now"
        tier = "good"
    else:
        phrase = "Weekday, outside regular market hours — markets are closed now"
        tier = "unknown"
    hour12 = now.hour % 12 or 12
    ampm = "AM" if now.hour < 12 else "PM"
    central_time = (f"{now.strftime('%a %b %d, %Y')}  "
                    f"{hour12}:{now.minute:02d} {ampm} Central")
    return {
        "now": now, "weekend": weekend, "market_hours": market,
        "phrase": phrase, "tier": tier, "central_time": central_time,
    }
