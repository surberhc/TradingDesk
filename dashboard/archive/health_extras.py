"""
health_extras.py — ARCHIVED dashboard Health-tab content: EDGAR fundamentals
display and the full Windows scheduled-task inventory table.

Archived 2026-07-07 as part of trimming dashboard/app.py to an S0-only focus
(Andrew's direction, post-review). Both pieces here show whole-desk state that
isn't specific to S0 (EDGAR fundamentals feed CAN SLIM; the full task
inventory covers ThetaData/options-warehouse/CAN SLIM tasks too, not just
S0's pipeline). Kept here, not deleted, for when this feature needs its own
dashboard presence again.

Note: the underlying EDGAR freshness/coverage COMPUTATION lives in the shared
dailyreport/desk_health.py::edgar_coverage() (added commit 73b0036) and is
UNCHANGED/still available -- this file only holds the presentation wrapper
(app.py's old edgar_coverage()) and the Streamlit rendering snippet that
called it, plus the full scheduled_task_states() task-inventory reader. The
trimmed app.py keeps its own small, S0-relevant subset of scheduled tasks
inline (TiingoDailyUpdate, EodReport, AccountMonitorDaily, GatewayWatchdog) --
it does NOT call into this file.

Reversible-archive pattern: same as dailyreport/archive/rrg (commit d6c396f).

To reinstate:
  1. Copy edgar_coverage() (presentation wrapper) and the EDGAR display block
     from render_health_edgar_snippet() back into app.py's render_health().
  2. Copy scheduled_task_states() back into app.py (or import it from here)
     and restore the SCHEDULED_TASKS dict with the full task set (add back
     ThetaEodDaily/ThetaForwardDaily/GexDailyBuild if trimmed).
  3. Restore the "#### Scheduled tasks (Windows)" full-table rendering block.

Dependencies this file assumes are available from the caller's namespace when
reinstated: `st` (streamlit), `json`, `datetime`, `desk_health` (shared
module, dailyreport/desk_health.py), `_TIER_DOT`, `_color_text`,
`EDGAR`, `EDGAR_FUNDAMENTALS`, `EDGAR_STALE_DAYS` (path/threshold constants).
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path

import streamlit as st


# Full Windows scheduled-task inventory the desk runs (name -> friendly label).
# This is EVERY desk task (options warehouse, CAN SLIM/EDGAR, ThetaData, S0's
# own feeds) -- not S0-specific. The trimmed app.py keeps only the S0-relevant
# subset inline instead of importing this dict.
SCHEDULED_TASKS_FULL = {
    "ThetaEodDaily": "EOD options (ThetaData)",
    "ThetaForwardDaily": "Forward EOD grab",
    "TiingoDailyUpdate": "Tiingo equity EOD",
    "GexDailyBuild": "GEX daily build",
    "EodReport": "EOD status report",
}


@st.cache_data(ttl=60)
def scheduled_task_states(scheduled_tasks: dict = SCHEDULED_TASKS_FULL) -> dict:
    """Read Windows Task Scheduler states for the desk's jobs (read-only). Best-effort:
    returns {label: state} and degrades to {} if PowerShell/schtasks isn't reachable."""
    states: dict[str, str] = {}
    try:
        names = list(scheduled_tasks.keys())
        filt = "|".join(names)
        cmd = ("Get-ScheduledTask | Where-Object { $_.TaskName -match '" + filt +
               "' } | Select-Object TaskName,State | ConvertTo-Json -Compress")
        out = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", cmd],
            capture_output=True, text=True, timeout=15)
        data = json.loads(out.stdout) if out.stdout.strip() else []
        if isinstance(data, dict):
            data = [data]
        raw = {d.get("TaskName"): d.get("State") for d in data}
        # State may be an int enum on some hosts; map common values.
        enum = {3: "Ready", 4: "Running", 1: "Disabled", 2: "Queued"}
        for tn, label in scheduled_tasks.items():
            v = raw.get(tn)
            if isinstance(v, int):
                v = enum.get(v, str(v))
            states[label] = v or "not found"
    except Exception:
        return {}
    return states


@st.cache_data(ttl=120)
def edgar_coverage(desk_health, EDGAR: Path, EDGAR_FUNDAMENTALS: Path,
                    EDGAR_STALE_DAYS: int) -> dict:
    """EDGAR point-in-time fundamentals freshness/coverage — presentation shaping
    on top of the shared desk_health.edgar_coverage() computation (also used by
    dailyreport/eod_report.py::build_edgar()). Returns a summary dict shaped for
    the Streamlit tiles; degrades gracefully to an 'info: build landing' state
    when the table isn't there yet.

    tier: good (fresh) / warn (stale or in-progress) / unknown (not landed yet)."""
    ec = desk_health.edgar_coverage(EDGAR, EDGAR_FUNDAMENTALS, EDGAR_STALE_DAYS)

    if not ec["dir_present"]:
        return {"tier": "unknown", "state": "build landing",
                "companies": None, "size_gb": 0.0, "n_files": 0,
                "table_present": False, "last_refresh": "—", "age_days": None,
                "newest_file": "—", "headline": "EDGAR warehouse dir not present yet."}

    companies = ec["n_companies"]
    table_present = ec["table_present"]
    refresh_dt = ec["refresh_dt"]
    age_days = ec["age_days"]
    newest_name = ec["newest_file"]
    newest_mtime = ec["newest_mtime"]

    if ec["recent_activity"]:
        tier, state = "warn", "refresh in progress"
        headline = (f"Build/refresh in progress — newest file {newest_name} "
                    "updated in the last 15 min. Table not final.")
    elif not table_present:
        tier, state = "unknown", "build landing"
        headline = "Fundamentals table not written yet."
    elif ec["state"] == "stale":
        tier, state = "warn", "stale"
        headline = (f"Stale — last refresh {age_days}d ago "
                    f"(> {EDGAR_STALE_DAYS}d periodic threshold). Time to re-pull EDGAR.")
    else:
        tier, state = "good", "fresh"
        headline = (f"Fresh — {companies:,} companies, last refresh {age_days}d ago."
                    if companies is not None else f"Fresh — last refresh {age_days}d ago.")

    return {
        "tier": tier, "state": state, "companies": companies,
        "size_gb": ec["size_bytes"] / 1e9, "n_files": ec["n_files"],
        "table_present": table_present,
        "last_refresh": refresh_dt.strftime("%Y-%m-%d %H:%M") if refresh_dt else "—",
        "age_days": age_days,
        "newest_file": f"{newest_name} @ {datetime.fromtimestamp(newest_mtime):%Y-%m-%d %H:%M}"
                       if newest_name else "—",
        "headline": headline,
    }


def render_edgar_section(ec: dict, _TIER_DOT: dict, _color_text) -> None:
    """The EDGAR fundamentals freshness/coverage display snippet that used to
    live inline in app.py::render_health(). Call with the dict returned by
    edgar_coverage() above."""
    st.markdown("#### EDGAR fundamentals (point-in-time)")
    et = ec["tier"]
    st.markdown(
        f"{_TIER_DOT[et]} **{_color_text(ec['state'], et)}** — {ec['headline']}",
        unsafe_allow_html=True)
    c1, c2, c3, c4 = st.columns(4)
    comp = ec["companies"]
    c1.metric("Companies", f"{comp:,}" if comp is not None else "—")
    c2.metric("Warehouse size", f"{ec['size_gb']:.2f} GB")
    age = ec["age_days"]
    c3.metric("Refresh age", f"{age}d" if age is not None else "—")
    c4.metric("Files", f"{ec['n_files']:,}")
    st.caption(
        f"Last refresh {ec['last_refresh']} · "
        f"table {'present' if ec['table_present'] else 'not built yet'} · "
        f"newest file: {ec['newest_file']}")


def render_scheduled_tasks_table(states: dict, _TIER_DOT: dict, _color_text,
                                  _status_tier) -> None:
    """The full scheduled-task inventory table that used to live inline in
    app.py::render_health(). Call with the dict returned by
    scheduled_task_states() above."""
    st.markdown("#### Scheduled tasks (Windows)")
    if not states:
        st.caption("Task states unavailable on this host.")
        return
    cols = st.columns(3)
    for i, (label, state) in enumerate(states.items()):
        with cols[i % 3]:
            tier = _status_tier(state)
            st.markdown(
                f"{_TIER_DOT[tier]} **{label}** — {_color_text(state, tier)}",
                unsafe_allow_html=True)
