"""
app.py — Trading Desk dashboard, scoped to S0 (Adaptive All-Weather Core). READ-ONLY.

Per Andrew's explicit direction (post-review session, 2026-07-07): this dashboard
focuses on S0, the ONE strategy actually live-paper-tested, not a whole-desk
monitor. Everything else (Gamma/GEX awareness panel, S5 Convexity research view,
EDGAR/CAN SLIM fundamentals coverage, the full scheduled-task inventory) has been
archived — physically moved, not deleted — to dashboard/archive/, ready to
reinstate the moment another strategy needs its own dashboard presence. See the
docstrings in dashboard/archive/*.py for what moved where and how to bring it back.

A single Streamlit app, phone- and desktop-friendly, that *shows* the state of S0
by reusing the existing Python directly. It NEVER places, arms, or transmits an
order, never writes to the warehouse/config, never calls replaceFA.

Four sections (tabs):
  1. Health      — S0's own data pipeline (Tiingo EOD feed status), the S0-only
                   nightly EOD email's own status, the account-monitor/rebalance
                   task, gateway up/down (cheap TCP probe, no live ib_async
                   connection), and disk free space.
  2. Backtests   — latest CAGR/maxDD/Calmar/Sortino/down-capture for S0's 3
                   versions (computed via the validated run_backtest, cached) +
                   links to the existing plotly HTML reports.
  3. Accounts    — the 5 DU paper subs (all enrolled in S0) read read-only through
                   the gateway, with a SHORT connect timeout so a weekend/feed-down
                   gateway degrades to "Gateway offline" instead of hanging. Drift
                   vs target + the rebalance PLAN preview (build-only). NO controls
                   anywhere.
  4. S8          — intraday PILOT_MODE monitor for S8 (British IC + B2 long-leg
                   auto-close), sourced from the S8 CAPTURE STORE (livebot/s8_store.py
                   -> trades.jsonl), not the retired runner ledger: today's 11-template
                   entry schedule, today's OPEN positions (entry snapshot), a live
                   exit-monitor (per-position distance-to-stop + running P&L computed
                   from the pilot's OWN RECORDED TICKS — zero Gateway contact), today's
                   CLOSED round-trips with exit
                   reasons + P&L + MAE + greeks-at-entry-vs-exit, the live account's own
                   margin snapshot, and live-TRADING Gateway (port 4003) connection
                   health. A date selector (default today) lets past sessions be viewed.
                   See render_s8() below for the read-only/never-feeds-back guarantees
                   specific to this tab.

Launch (see run_dashboard.bat): binds 0.0.0.0 so a phone on the LAN can reach it.

Read-only guarantees in this file:
  * The only broker call path is ibkr_paper.connect(readonly=True) with a short timeout,
    plus ib.managedAccounts / accountSummary / positions / reconcile (all reads) and
    rebalance_run.build_preview (a PURE build-only planner — no order objects, no send).
  * No order_router.place/arm, no ib.placeOrder, no replaceFA, no file writes.
  * The S8 tab's live reads go through connections.ibkr_live_trade.connect(readonly=True) —
    the live-TRADING Gateway (port 4003), connected read-only/display-only — called
    with launch=False (this dashboard never boots the Gateway) and a short timeout;
    every connection opened here is disconnected before the fragment returns. It only
    ever calls ib.accountSummary() (the account/margin snapshot) — no per-leg quote
    request; no order object is ever built or placed from this tab. The live exit-monitor
    overlay makes NO Gateway contact at all: it reads the pilot's own RECORDED tick
    parquet (livebot/s8_monitor.py's capture), never a fresh quote.
"""
from __future__ import annotations

import io
import json
import os
import sys
import contextlib
import time as _time
from datetime import datetime
from datetime import time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

# --- Make the existing packages importable (reuse, don't rebuild) --------------
REPO = Path(__file__).resolve().parent.parent
for sub in ("paperbot", "backtester", "connections", "strategies", "dailyreport", "livebot"):
    p = REPO / sub
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
# connections is a namespace package one level deeper.
_conn = REPO / "connections"
if str(_conn) not in sys.path:
    sys.path.insert(0, str(_conn))

# dailyreport is a flat sys.path addition (no __init__.py), same convention as
# the other folders above — import its modules directly. (desk_health.py is
# still the shared computation module for the archived Gamma/EDGAR panels —
# see dashboard/archive/ — but app.py itself no longer needs it directly.)
import status as dr_status

# S8 tab: pure/data modules only at module scope (no IBKR import triggers a connection
# at import time — s8_config is data-only and the capture-store modules below are
# pure/read-only over trades.jsonl + parquet). connections.ibkr_live_trade itself (the
# thing that actually opens a socket) stays a LAZY import inside
# _s8_connect_readonly_short(), same convention render_accounts() already uses for
# `from connections import ibkr_paper`. These all live in livebot/ (S8 was relocated out
# of paperbot/ into its own package, commit 321b5cf) — hence livebot on sys.path above.
import s8_config
import s8_monitor_core
import s8_report
import s8_schema
import s8_store

# --- Local data locations (off-Drive, on C:) -----------------------------------
# (WAREHOUSE/derived-GEX-parquet paths are no longer needed here — that's the
# archived Gamma tab's territory; see dashboard/archive/gamma_tab.py.)
STATUS_DIR = Path(r"C:\TradingDesk-Local\state\dailyreport\status")
BACKTEST_OUTPUT = REPO / "backtester" / "output"

# Windows scheduled tasks that actually drive S0 (name -> friendly label).
# The full whole-desk task inventory (ThetaData/GEX/EDGAR etc.) is archived in
# dashboard/archive/health_extras.py — this is deliberately the S0-only subset:
#   TiingoDailyUpdate  — S0's real data inputs (SPY/RSP/sectors/HYG/IEF EOD)
#   EodReport          — the nightly EOD email, now trimmed to S0-only sections
#   AccountMonitorDaily — daily drift check + propose-only rebalance for the
#                         S0-enrolled paper accounts
#   GatewayWatchdog    — keeps the paper gateway S0's Accounts tab reads through up
SCHEDULED_TASKS = {
    "TiingoDailyUpdate": "Tiingo equity EOD (S0 inputs)",
    "EodReport": "EOD status report (S0-only)",
    "AccountMonitorDaily": "Account monitor / rebalance check",
    "GatewayWatchdog": "Gateway watchdog",
}

BACKTEST_VERSIONS = ("Conservative", "Balanced", "Growth")

st.set_page_config(page_title="Trading Desk", page_icon="📊", layout="wide")

# --- Compact restyle (STYLING ONLY — no data/logic change) ---------------------
# Smaller base font + tighter vertical spacing so far more fits per screen on
# this read-only monitor. Nothing here touches data, queries, or behavior.
st.markdown(
    """
    <style>
      /* Base font: ~13.5px vs Streamlit's 16px default, across body & markdown. */
      html, body, [class*="css"], .stMarkdown, .stMarkdown p,
      .stText, .stCaption, [data-testid="stMarkdownContainer"] {
        font-size: 13.5px !important;
        line-height: 1.35 !important;
      }
      /* Pull the top of the main block up and tighten its horizontal padding. */
      .block-container {
        padding-top: 4rem !important;
        padding-bottom: 1.2rem !important;
        padding-left: 1.6rem !important;
        padding-right: 1.6rem !important;
        max-width: 1500px !important;
      }
      /* Headers: smaller sizes + tighter margins. */
      h1, .stTitle { font-size: 1.55rem !important; margin: 0 0 .35rem 0 !important; }
      h2 { font-size: 1.20rem !important; margin: .4rem 0 .3rem 0 !important; }
      h3 { font-size: 1.02rem !important; margin: .35rem 0 .25rem 0 !important; }
      h4 { font-size: .92rem !important; margin: .3rem 0 .2rem 0 !important; }
      /* Shrink the gap Streamlit puts between vertical elements. */
      [data-testid="stVerticalBlock"] { gap: .45rem !important; }
      [data-testid="stHorizontalBlock"] { gap: .55rem !important; }
      /* Captions a touch smaller. */
      .stCaption, [data-testid="stCaptionContainer"] { font-size: 11.5px !important; }
      /* Metrics: denser label + value. */
      [data-testid="stMetric"] { padding: .15rem 0 !important; }
      [data-testid="stMetricLabel"] p { font-size: 11.5px !important; }
      [data-testid="stMetricValue"] { font-size: 1.25rem !important; line-height: 1.2 !important; }
      /* Tabs: tighter padding, smaller label. */
      .stTabs [data-baseweb="tab"] { padding: .35rem .7rem !important; font-size: 13px !important; }
      .stTabs [data-baseweb="tab-list"] { gap: .3rem !important; }
      /* Dividers: collapse the big default vertical margin. */
      hr { margin: .5rem 0 !important; }
      /* Dataframe/table: shorter rows, smaller cell text. */
      [data-testid="stDataFrame"] { font-size: 12px !important; }
      [data-testid="stTable"] td, [data-testid="stTable"] th { padding: .2rem .4rem !important; }
      /* Expanders & buttons: trim padding. */
      .streamlit-expanderHeader { font-size: 13px !important; padding: .3rem .5rem !important; }
      .stButton button { padding: .25rem .7rem !important; font-size: 13px !important; }
    </style>
    """,
    unsafe_allow_html=True,
)


# ============================ small shared helpers =============================
def _fmt_dt(ts: str | None) -> str:
    if not ts:
        return "—"
    return str(ts).replace("T", " ")


def _status_tier(status: str) -> str:
    """Map any free-text status onto one of: good / warn / bad / unknown."""
    s = (status or "").lower()
    if any(k in s for k in ("ok", "ready", "green", "running", "aligned", "clean")):
        return "good"
    if any(k in s for k in ("warn", "partial", "stale", "yellow", "queued", "drift")):
        return "warn"
    if any(k in s for k in ("fail", "error", "disabled", "red", "offline", "not found")):
        return "bad"
    return "unknown"


# One source of truth for the dot + accent colour used everywhere.
_TIER_DOT = {"good": "🟢", "warn": "🟡", "bad": "🔴", "unknown": "⚪"}
_TIER_COLOR = {"good": "#3ddc84", "warn": "#f5c451", "bad": "#ff5c5c", "unknown": "#9aa0a6"}


def _status_badge(status: str) -> str:
    """The single 🟢/🟡/🔴/⚪ renderer used by Health tiles, gamma state and tasks."""
    return _TIER_DOT[_status_tier(status)]


def _badge_legend() -> None:
    st.caption("🟢 ok / ready  ·  🟡 warning / partial / stale  ·  "
               "🔴 fail / error / offline  ·  ⚪ unknown / no data")


def _color_text(text: str, tier: str) -> str:
    """Inline coloured markdown span for a given good/warn/bad/unknown tier."""
    return f"<span style='color:{_TIER_COLOR.get(tier, _TIER_COLOR['unknown'])}'>{text}</span>"


@st.cache_data(ttl=60)
def load_json(path_str: str) -> dict | None:
    """Read one status JSON. Delegates to dailyreport.status.read() (the
    already-correct shared reader) — this wrapper keeps the existing path-based
    call sites unchanged and preserves the exact None-on-failure contract."""
    p = Path(path_str)
    job = p.stem  # STATUS_DIR / f"{job}.json" -> job name status.read() expects
    return dr_status.read(job)


@st.cache_data(ttl=60)
def scheduled_task_states() -> dict:
    """Read Windows Task Scheduler states for S0's own jobs (read-only). Best-effort:
    returns {label: state} and degrades to {} if PowerShell/schtasks isn't reachable.
    (The full whole-desk task inventory is archived — see dashboard/archive/health_extras.py.)"""
    import subprocess
    states: dict[str, str] = {}
    try:
        names = list(SCHEDULED_TASKS.keys())
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
        for tn, label in SCHEDULED_TASKS.items():
            v = raw.get(tn)
            if isinstance(v, int):
                v = enum.get(v, str(v))
            states[label] = v or "not found"
    except Exception:
        return {}
    return states


# ================================ 1. HEALTH ===================================
def _port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    """Cheap 'is something listening?' TCP probe — milliseconds, no asyncio, no
    trading session. Same pattern as dailyreport/eod_report.py::_port_open — a
    live ib_async connect just to print up/down previously crashed a
    non-interactive scheduled job (silent for 5 nights, 2026-06-27..07-01). A
    port-open check is a safe proxy for 'up' and safe to call on every page load."""
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@st.cache_data(ttl=30)
def gateway_status() -> dict:
    """S0's paper gateway (port 4002) up/down + C: free disk space — the two
    cheapest 'will things break tonight' checks. No live trading session opened."""
    up = _port_open("127.0.0.1", 4002)
    try:
        import shutil as _shutil
        _t, _u, free = _shutil.disk_usage("C:\\")
        free_gb = free / 1e9
    except Exception:
        free_gb = None
    return {"gateway_up": up, "free_gb": free_gb}


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    s = str(s).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y%m%d"):
        try:
            return datetime.strptime(s[:19] if len(s) >= 19 else s, fmt)
        except Exception:
            continue
    return None


@st.cache_data(ttl=60)
def last_refreshed() -> str:
    """Newest of every available S0-relevant data timestamp: the tiingo/eod_report
    status JSON `ts` values, and the newest backtest report mtime."""
    cands: list[datetime] = []
    for name in ("tiingo", "eod_report"):
        js = load_json(str(STATUS_DIR / f"{name}.json"))
        if js:
            d = _parse_ts(js.get("ts")) or _parse_ts(js.get("date"))
            if d:
                cands.append(d)
    for v in BACKTEST_VERSIONS:
        f = BACKTEST_OUTPUT / f"backtest_report_{v}.html"
        if f.exists():
            cands.append(datetime.fromtimestamp(f.stat().st_mtime))
    if not cands:
        return "—"
    return max(cands).strftime("%Y-%m-%d %H:%M")


@st.cache_data(ttl=60)
def desk_status() -> dict:
    """Roll S0's pipeline up into one home-screen summary. Read-only; reuses the
    same JSONs the detail section shows, plus the gateway/disk probe."""
    # Overall pipeline health = worst tier across S0's own status JSONs.
    order = {"good": 0, "unknown": 1, "warn": 2, "bad": 3}
    worst, worst_name = "good", "—"
    any_status = False
    latest_day = "—"
    for name in ("tiingo", "eod_report"):
        js = load_json(str(STATUS_DIR / f"{name}.json"))
        if not js:
            continue
        any_status = True
        shown = js.get("metrics", {}).get("overall") or js.get("status", "")
        t = _status_tier(shown)
        if order[t] > order[worst]:
            worst, worst_name = t, name
        if name == "tiingo" and js.get("date"):
            latest_day = js["date"]
    if not any_status:
        worst = "unknown"

    gw = gateway_status()

    states = scheduled_task_states()
    not_ready = sum(1 for s in states.values() if str(s).lower() != "ready")
    n_tasks = len(states)

    return {
        "overall_tier": worst, "overall_name": worst_name,
        "latest_day": latest_day,
        "gateway_up": gw["gateway_up"], "free_gb": gw["free_gb"],
        "tasks_not_ready": not_ready, "tasks_total": n_tasks,
    }


def render_health() -> None:
    st.subheader("S0 health")
    st.caption("Scoped to S0 (Adaptive All-Weather Core): its data pipeline, its "
               "own scheduled tasks, and the gateway/disk checks. Whole-desk "
               "sections (EDGAR, GEX, full task inventory) are archived — see "
               "dashboard/archive/.")

    # --- At-a-glance S0 status (the "is anything broken?" row) ---
    s = desk_status()
    cols = st.columns(4)
    with cols[0]:
        tier = s["overall_tier"]
        label = {"good": "Healthy", "warn": "Warning",
                 "bad": "Problem", "unknown": "Unknown"}[tier]
        st.metric("S0 pipeline health", f"{_TIER_DOT[tier]} {label}", border=True)
        if tier in ("warn", "bad") and s["overall_name"] != "—":
            st.caption(f"worst: {s['overall_name']}")
    with cols[1]:
        st.metric("Latest Tiingo data day", s["latest_day"], border=True)
    with cols[2]:
        up = s["gateway_up"]
        tier_g = "good" if up else "warn"
        st.metric("Paper gateway (4002)", f"{_TIER_DOT[tier_g]} {'UP' if up else 'DOWN'}", border=True)
        is_weekend = datetime.now().weekday() >= 5
        st.caption("Weekend — offline is expected"
                   if is_weekend else "Weekday — should be up")
    with cols[3]:
        fg = s["free_gb"]
        val = f"{fg:.0f} GB" if fg is not None else "—"
        tier_d = "good" if (fg is None or fg > 5) else "warn"
        st.metric("C: free space", f"{_TIER_DOT[tier_d]} {val}", border=True)
    _badge_legend()

    st.divider()

    # --- Status JSONs (S0-relevant only) ---
    st.markdown("#### Pipeline status")
    cols = st.columns(2)
    for col, name in zip(cols, ("tiingo", "eod_report")):
        js = load_json(str(STATUS_DIR / f"{name}.json"))
        with col:
            if not js:
                st.markdown(f"**{name}**  ⚪")
                st.caption("no status file")
                continue
            status = js.get("status", "?")
            # eod_report carries a nested 'overall' that's the real health.
            overall = js.get("metrics", {}).get("overall")
            shown = overall or status
            tier = _status_tier(shown)
            st.markdown(
                f"**{name}**  {_TIER_DOT[tier]} {_color_text(shown, tier)}",
                unsafe_allow_html=True)
            st.caption(f"date {js.get('date','—')} · {_fmt_dt(js.get('ts'))}")
            if js.get("message"):
                st.caption(js["message"])

    st.divider()

    # --- S0's own scheduled tasks ---
    st.markdown("#### S0 scheduled tasks (Windows)")
    states = scheduled_task_states()
    if not states:
        st.caption("Task states unavailable on this host.")
    else:
        cols = st.columns(2)
        for i, (label, state) in enumerate(states.items()):
            with cols[i % 2]:
                tier = _status_tier(state)
                st.markdown(
                    f"{_TIER_DOT[tier]} **{label}** — {_color_text(state, tier)}",
                    unsafe_allow_html=True)


# ============================== 2. BACKTESTS ==================================
@st.cache_data(ttl=3600, show_spinner="Running backtest curve for performance comparison...")
def _backtest_strategy_curve(version_str: str) -> pd.Series:
    """The validated run_backtest()'s own "strategy" NAV series for one version —
    reused as-is (no re-derived performance math) to compare against the live
    paper NAV. Cached 1h, same convention as backtest_metrics() below (re-running
    the full backtest is comparatively expensive; the NAV history read that pairs
    with this is NOT cached, since it changes daily and should show promptly)."""
    from src import backtest as bt
    with contextlib.redirect_stdout(io.StringIO()):
        res = bt.run_backtest(version=version_str, end=None)
    # run_backtest() names its own NAV series "strategy" (res["nav"], also carried
    # as benchmark_navs["strategy"]) — pull it directly, no re-derived math.
    return res["nav"]


@st.cache_data(ttl=3600, show_spinner="Computing backtest metrics (validated engine)...")
def backtest_metrics() -> pd.DataFrame:
    """Run the VALIDATED run_backtest once per version (cached 1h) and pull the
    headline metrics via the backtester's own metrics.compute_metrics. Read-only
    compute — same code path the strategy/paperbot use; touches no broker."""
    from src import backtest as bt
    from src import metrics as mt

    headline = ["CAGR", "Max drawdown", "Calmar", "Sortino", "Down capture vs SPY"]
    rows = {}
    for v in BACKTEST_VERSIONS:
        # Silence the engine's own prints so they don't pollute the UI.
        with contextlib.redirect_stdout(io.StringIO()):
            res = bt.run_backtest(version=v, end=None)
            table = mt.compute_metrics(res["benchmark_navs"])
        col = "strategy"
        rows[v] = {m: (table.loc[m, col] if m in table.index else float("nan"))
                   for m in headline}
    df = pd.DataFrame(rows).T  # versions as rows
    return df


def render_backtests() -> None:
    st.subheader("Backtest results (3 versions)")

    if st.button("↻ Recompute metrics (cached 1h)"):
        backtest_metrics.clear()

    try:
        df = backtest_metrics()

        # Per-version metric cards. The VALUE is colour-coded by meaning:
        # green for favourable CAGR/Calmar/Sortino, red for the drawdown &
        # down-capture risk stats (which are inherently "bad" magnitudes).
        risk_metrics = {"Max drawdown", "Down capture vs SPY"}
        for v in BACKTEST_VERSIONS:
            if v not in df.index:
                continue
            st.markdown(f"#### {v}")
            row = df.loc[v]
            mcols = st.columns(len(df.columns))
            for mc, metric in zip(mcols, df.columns):
                raw = row[metric]
                if metric in ("CAGR", "Max drawdown", "Down capture vs SPY"):
                    txt = f"{raw * 100:.1f}%" if pd.notna(raw) else "—"
                else:
                    txt = f"{raw:.2f}" if pd.notna(raw) else "—"
                if pd.isna(raw):
                    tier = "unknown"
                elif metric in risk_metrics:
                    tier = "bad"          # drawdown / down-capture read red
                else:
                    tier = "good" if raw >= 0 else "bad"
                with mc:
                    st.caption(metric)
                    st.markdown(
                        f"<div style='font-size:1.25rem;font-weight:600'>"
                        f"{_color_text(txt, tier)}</div>",
                        unsafe_allow_html=True)
        st.caption("Computed live from the validated run_backtest "
                   "(strategy column vs SPY). Cached 1 hour. Green = favourable, "
                   "red = drawdown / down-capture (deeper is worse).")
    except Exception as exc:
        st.error(f"Could not compute metrics: {exc}")

    st.divider()
    st.markdown("#### Full HTML reports (plotly)")
    st.caption("Generated by the backtester. Click to open / download the rich report.")
    for v in BACKTEST_VERSIONS:
        f = BACKTEST_OUTPUT / f"backtest_report_{v}.html"
        if f.exists():
            mtime = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            with open(f, "rb") as fh:
                st.download_button(
                    f"⬇ {v} report  ({mtime})", fh.read(),
                    file_name=f.name, mime="text/html", key=f"dl_{v}")
        else:
            st.caption(f"{v}: report not found")


def render_performance() -> None:
    """S0 Performance vs Model — live paper NAV (per version, accounts summed by
    version since that's what's directly comparable to one backtest curve) vs the
    backtest's own run_backtest() curve for the same version/window, both rebased
    to 100 at the first tracked date. Purely additive reporting; no broker call —
    reads the local nav_history.csv that account_monitor_run.py appends to daily.

    The live paper test started 2026-07-07 with no backfill possible before that
    date, so this gracefully shows a "tracking started" message until >=2 distinct
    dates of history exist (true today, the day this feature ships)."""
    st.markdown("#### S0 Performance vs Model")
    st.caption("Live paper NAV (summed per version) vs the validated backtest's own "
               "NAV curve for the same version and window, both rebased to 100 at "
               "the first tracked date. No backfill exists before the live paper "
               "test started (2026-07-07) — tracking accumulates forward only.")

    import nav_history
    hist = nav_history.load_history()

    if hist.empty or hist["date"].nunique() < 2:
        first_date = hist["date"].min() if not hist.empty else None
        if first_date:
            st.info(f"Performance tracking started {first_date} — check back after "
                    "a few sessions accumulate.")
        else:
            st.info("Performance tracking has not recorded any sessions yet — "
                    "check back after the account monitor's next cycle.")
        return

    start_date = hist["date"].min()
    end_date = hist["date"].max()

    import plotly.graph_objects as go

    for v in BACKTEST_VERSIONS:
        v_hist = hist[hist["version"] == v]
        if v_hist.empty:
            continue
        paper_nav = v_hist.groupby("date")["net_liq"].sum().sort_index()
        if len(paper_nav) < 2 or paper_nav.iloc[0] == 0:
            continue
        paper_rebased = paper_nav / paper_nav.iloc[0] * 100.0

        try:
            bt_curve = _backtest_strategy_curve(v)
            bt_window = bt_curve.loc[start_date:end_date]
        except Exception as exc:
            st.caption(f"{v}: could not load backtest curve ({type(exc).__name__})")
            continue
        if bt_window.empty:
            st.caption(f"{v}: backtest curve has no data in the tracked window yet.")
            continue
        bt_rebased = bt_window / bt_window.iloc[0] * 100.0

        fig = go.Figure()
        fig.add_trace(go.Scatter(x=paper_rebased.index, y=paper_rebased.values,
                                  mode="lines", name="Paper (live)"))
        fig.add_trace(go.Scatter(x=bt_rebased.index, y=bt_rebased.values,
                                  mode="lines", name="Backtest (model)"))
        fig.update_layout(
            title=f"{v} — rebased to 100 at {start_date}",
            height=280, margin=dict(l=10, r=10, t=40, b=10),
            legend=dict(orientation="h", yanchor="bottom", y=1.02),
        )
        st.plotly_chart(fig, use_container_width=True, key=f"perf_{v}")


# ============================== 3. ACCOUNTS ===================================
def _connect_readonly_short(timeout: int = 6):
    """Connect read-only with a SHORT timeout. Never launches the gateway (weekend
    safety) so a down gateway fails fast instead of trying to boot it. Returns the IB
    handle or raises."""
    from connections import ibkr_paper
    # readonly=True -> the session physically cannot transmit. launch=False -> no boot.
    return ibkr_paper.connect("paperbot_accounts", readonly=True, launch=False, timeout=timeout)


def render_accounts() -> None:
    st.subheader("Live paper accounts (read-only)")
    st.caption("Display only. No controls. The gateway is offline on weekends — this "
               "panel degrades gracefully and lights up Monday.")

    # Performance tracking reads a local CSV (no broker call), so it renders
    # regardless of whether the gateway read below succeeds.
    render_performance()
    st.divider()

    go = st.button("🔌 Read live accounts (read-only)")
    if not go:
        st.info("Press the button to attempt a short, read-only gateway read. "
                "If the gateway is down (weekend/feed down) you'll see an offline "
                "notice rather than a hang.")
        return

    import accounts as acc_mod
    import config as pb_config
    import strategy_target
    import rebalance_run

    ib = None
    try:
        with st.spinner("Connecting to paper gateway (read-only, short timeout)..."):
            ib = _connect_readonly_short(timeout=6)
    except Exception as exc:
        st.error("**Gateway offline — live account data unavailable "
                 "(weekend / feed down).** It will light up Monday.")
        st.caption(f"(connect failed fast: {type(exc).__name__})")
        return

    try:
        with st.spinner("Reading account structure (read-only)..."):
            infos = acc_mod.discover(ib)
        if not infos:
            st.warning("Gateway connected but reported no managed accounts.")
            return

        # --- Account table ---
        rows = []
        for i in sorted(infos, key=lambda x: (not x.is_master, x.number)):
            rows.append({
                "account": i.number,
                "kind": i.kind,
                "NetLiq": f"{i.net_liq:,.0f}",
                "cash": f"{i.total_cash:,.0f}",
                "positions": i.n_positions,
                "funded": "yes" if i.funded else "no",
                "version": i.version or ("(advisor)" if i.is_master else "NOT ENROLLED"),
            })
        st.markdown("#### Accounts")
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        warns = acc_mod.reconcile_enrollment(infos)
        if warns:
            for w in warns:
                st.warning(w)
        else:
            st.success("Enrollment clean — every enrolled account visible, valid, funded.")

        # --- Drift vs target, per enrolled+funded client ---
        st.divider()
        st.markdown("#### Drift vs target (per enrolled account)")
        clients = [i for i in infos if i.enrolled and i.funded and not i.is_master]
        if not clients:
            st.caption("No enrolled + funded client accounts.")
        else:
            import reconcile as recon
            targets_cache: dict = {}
            for info in sorted(clients, key=lambda x: x.number):
                if info.version not in targets_cache:
                    with st.spinner(f"Computing {info.version} target..."):
                        targets_cache[info.version] = strategy_target.current_target(
                            version=info.version)
                tgt = targets_cache[info.version]
                positions = {p.contract.symbol: p.position
                             for p in ib.positions(info.number) if p.position != 0}
                lines = recon.reconcile(tgt, info.net_liq, positions)
                drift_rows = [{
                    "symbol": ln.symbol, "status": ln.status,
                    "tgt_w": f"{ln.target_weight*100:.1f}%",
                    "act_w": f"{ln.actual_weight*100:.1f}%",
                    "drift": f"{ln.drift_weight*100:+.1f}%",
                } for ln in lines if ln.target_weight > 0 or ln.actual_shares != 0]
                aligned = all(ln.status == "MATCHED" for ln in lines)
                dot = _TIER_DOT["good"] if aligned else _TIER_DOT["bad"]
                with st.expander(
                        f"{dot} {info.number} [{info.version}] — "
                        f"{'ALIGNED' if aligned else 'drift present'}"):
                    if aligned:
                        st.markdown(_color_text("Aligned with target — no drift.",
                                                "good"), unsafe_allow_html=True)
                    else:
                        st.markdown(_color_text("Drifting from target.", "bad"),
                                    unsafe_allow_html=True)
                    st.dataframe(pd.DataFrame(drift_rows),
                                 use_container_width=True, hide_index=True)

            # --- Rebalance PLAN preview (build-only; transmits nothing) ---
            st.divider()
            st.markdown("#### Rebalance plan (review only — nothing is built or sent)")
            account_inputs = []
            for info in sorted(clients, key=lambda x: x.number):
                positions = {p.contract.symbol: p.position
                             for p in ib.positions(info.number) if p.position != 0}
                tgt = targets_cache[info.version]
                prices = {s: float(tgt.prices.get(s, float("nan")))
                          for s in tgt.prices.index}
                account_inputs.append({
                    "account": info.number, "version": info.version,
                    "net_liq": info.net_liq, "positions": positions, "prices": prices})
            # build_preview prints its report; capture it for display. PURE/build-only.
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rebalance_run.build_preview(account_inputs, targets_cache)
            st.code(buf.getvalue() or "(no plan output)", language="text")
            st.caption("This is the SHAPE of a rebalance only. No order objects are "
                       "created, nothing is armed, nothing is transmitted.")
    except Exception as exc:
        st.error(f"Read failed: {exc}")
    finally:
        if ib is not None:
            try:
                ib.disconnect()
            except Exception:
                pass


# ================================== 4. S8 =====================================
# S8 (British IC + B2 long-leg auto-close): SPX/SPXW 0DTE scheduled credit-spread pair
# strategy, still in PILOT_MODE (livebot/s8_service.py hardcodes PILOT_MODE=True and
# never calls order_router.place()/ib.placeOrder() — see that module's own docstring).
# This tab is a pure MONITOR over the S8 CAPTURE STORE: it reads the durable trade
# records via livebot/s8_store.read_trade_records() (append-only trades.jsonl under
# C:\TradingDesk-Local\s8_pilot\, latest-wins by trade_id), NOT the retired runner
# ledger. The live all-day service (s8_service.py) writes ONLY to this store — it no
# longer writes ledger records — so the store is the single source of truth here.
# For TODAY's still-open captured positions it layers on a live exit-monitor overlay
# (per-position distance-to-stop + running P&L) computed from the pilot's OWN RECORDED
# TICKS (livebot/s8_monitor.py's capture parquet — read-only, in-memory DuckDB, ZERO
# Gateway contact) PURELY FOR DISPLAY, using the canonical frozen semantics in
# livebot/s8_monitor_core.py (spread_close_value = short_ask - long_bid;
# distance_to_stop = stop_price - spread_close_value).
#
# When the store is empty (no session captured yet), a past session is selected, or no
# ticks have been recorded for a position yet, the overlay's live columns simply show
# "—" (and a tick older than the freshness threshold is flagged stale rather than shown
# as live). That is the expected, correctly-handled state for this tab, not a bug.
CT_ZONE = ZoneInfo("America/Chicago")     # matches s8_config.ENTRY_GRID_CT's own convention

# Pilot default: mirrors the service's own QTY_PER_ENTRY (=1) — used only as a fallback
# when a captured record has no qty. The store records carry their own qty, so the live
# P&L math prefers record.qty and falls back to this constant.
S8_QTY_PER_ENTRY = 1


@st.cache_data(ttl=20)
def _s8_store_records() -> list:
    """All S8 capture-store trade records, read-only, latest-wins by trade_id.

    Delegates to livebot/s8_store.read_trade_records() — the SAME read livebot/
    s8_report.py uses — and never writes to the store. Returns a list of
    s8_schema.TradeRecord (dataclasses; picklable, so st.cache_data can hold them).
    Cached 20s (short — this feeds the fast-refresh fragment). Any read failure
    degrades to an empty list rather than taking the tab down."""
    try:
        return s8_store.read_trade_records()
    except Exception:
        return []


def _s8_available_dates(records: list) -> list[str]:
    """Distinct captured session dates (bare YYYYMMDD), most-recent first. Pure."""
    return sorted({r.date for r in records if r.date}, reverse=True)


def _s8_records_for_date(records: list, date: str | None) -> list:
    """Records for one session date, reusing s8_report.select_records (pure). A None
    date means 'all captured records'."""
    if date is None:
        return list(records)
    return s8_report.select_records(records, date=date)


# --- 4.0b PURE distance-to-stop math (offline-testable; no st, no IBKR) ---------
def _s8_monitor_position(rec):
    """Build a livebot/s8_monitor_core.MonitorPosition from a stored TradeRecord.

    PURE (no I/O). realized_credit and stop_price are taken VERBATIM from the frozen
    entry (never recomputed here) — the monitor core only compares live prices against
    that already-frozen level (rule #1 stays clean)."""
    e = rec.entry
    return s8_monitor_core.MonitorPosition(
        trade_id=rec.trade_id,
        side=rec.side,
        short_strike=(e.short_strike if e else None),
        long_strike=(e.long_strike if e else None),
        qty=(rec.qty if rec.qty else S8_QTY_PER_ENTRY),
        realized_credit=(e.realized_credit if e and e.realized_credit is not None else 0.0),
        stop_price=(e.stop_price if e and e.stop_price is not None else 0.0),
    )


def s8_distance_to_stop(rec, short_ask, long_bid) -> dict:
    """PURE. Given an open TradeRecord + the CURRENT short-leg ask and long-leg bid,
    return the live exit-monitor row values using the CANONICAL frozen semantics from
    livebot/s8_monitor_core (the corrected net-spread stop, commit e08568e):

        spread_cost      = short_ask - long_bid                 (cost to close now)
        distance_to_stop = stop_price - spread_cost             (points; <=0 == stopped)
        running_pnl      = (realized_credit - spread_cost) * 100 * qty   (dollars)

    Computed by delegating to s8_monitor_core.spread_close_value / pnl_at so this tab can
    never drift from the monitor's own stop/P&L math. None-safe: a missing quote yields
    None for whatever needs it (never a guess, never a crash). NO Streamlit or IBKR
    dependency — this is the single computation the offline tests pin."""
    pos = _s8_monitor_position(rec)
    sample = s8_monitor_core.Sample(short_ask=short_ask, long_bid=long_bid)
    spread_cost = s8_monitor_core.spread_close_value(sample)
    running_pnl = s8_monitor_core.pnl_at(pos, sample)
    stop_price = rec.entry.stop_price if rec.entry else None
    distance = None
    if spread_cost is not None and stop_price is not None:
        distance = float(stop_price) - spread_cost
    stopped = distance is not None and distance <= 0
    return {
        "spread_cost": spread_cost,
        "stop_price": stop_price,
        "distance_to_stop": distance,
        "running_pnl": running_pnl,
        "stopped": stopped,
    }


# --- 4.1 Today's schedule strip ------------------------------------------------
def _s8_next_slot_today(grid: list[str] | None, now_ct: dt_time) -> str:
    if not grid:
        return "—"
    now_min = now_ct.hour * 60 + now_ct.minute
    for slot in grid:
        h, m = (int(x) for x in slot.split(":"))
        if h * 60 + m >= now_min:
            return slot
    return "done for today"


def _s8_last_fired(records: list, template: str) -> str:
    """Latest captured entry timestamp for `template` among the given (today's) store
    records — the store now IS the per-entry log (one TradeRecord per would-be trade),
    so a template 'fired today' iff it has a captured record today. Records are in
    first-seen order, so the last match is the most recent. '—' if none captured."""
    latest = "—"
    for rec in records:
        if rec.template == template and rec.entry and rec.entry.entry_ts:
            latest = rec.entry.entry_ts
    return latest


def _s8_first_slot_minutes(grid: list[str] | None) -> int | None:
    """Minutes-since-midnight of a template's own FIRST (earliest) scheduled entry —
    the template's fixed, day-independent sort key. Deliberately NOT
    _s8_next_slot_today()'s "next slot from right now" value: that changes as the day
    progresses and would reorder the whole table hour by hour, which defeats its use
    as a stable reference schedule. None (no ENTRY_GRID_CT data) sorts last."""
    if not grid:
        return None
    h, m = (int(x) for x in grid[0].split(":"))
    return h * 60 + m


def render_s8_schedule(records: list) -> None:
    st.markdown("#### Today's schedule — all 11 templates")
    st.caption("Times are CT (US/Central), matching s8_config.ENTRY_GRID_CT's own "
               "convention. Sorted by each template's own first scheduled entry time "
               "(fixed all day — not reordered by the clock); the 5 templates flagged "
               "NO SCHEDULE DATA are real gaps in the real-fills MATCHED sample (too "
               "few observations to name a grid, per s8_config.py's own comment) and "
               "are grouped at the bottom rather than interspersed. 'last fired' is now "
               "store-sourced (a captured trade record for the template today).")
    now_ct = datetime.now(tz=CT_ZONE).time()
    today = datetime.now(tz=CT_ZONE).strftime("%Y%m%d")
    records = _s8_records_for_date(records, today)
    templates_sorted = sorted(
        s8_config.TEMPLATES.items(),
        key=lambda kv: (
            (1, 0, kv[0]) if _s8_first_slot_minutes(s8_config.ENTRY_GRID_CT.get(kv[0])) is None
            else (0, _s8_first_slot_minutes(s8_config.ENTRY_GRID_CT.get(kv[0])), kv[0])
        ),
    )
    rows = []
    for name, cfg in templates_sorted:
        grid = s8_config.ENTRY_GRID_CT.get(name)
        last_fired = _s8_last_fired(records, name)
        if grid is None:
            next_slot, status = "n/a", "⚪ NO SCHEDULE DATA"
        else:
            next_slot = _s8_next_slot_today(grid, now_ct)
            if last_fired != "—":
                status = "🟢 fired today"
            elif next_slot == "done for today":
                status = "⚪ done for today (never fired)"
            else:
                status = "🟡 pending"
        rows.append({
            "template": name, "side": cfg["side"], "width label": cfg["width_label"],
            "target credit": f"${cfg['target_credit']:.0f}",
            "next entry (CT)": next_slot, "last fired": last_fired, "status": status,
        })
    # st.dataframe (kept for styling consistency with every other table in this file —
    # see the Accounts/cycle-log/history tables above, none of which use st.table) but
    # with EXPLICIT per-column width config: this is an 11-row static schedule strip
    # where a squeezed/auto-fit column is exactly the reported bug (time values
    # rendering invisible), so every column — the two time columns especially — gets a
    # forced minimum width rather than trusting auto-fit.
    st.dataframe(
        pd.DataFrame(rows), use_container_width=True, hide_index=True,
        column_config={
            "template": st.column_config.TextColumn("template", width="medium"),
            "side": st.column_config.TextColumn("side", width="small"),
            "width label": st.column_config.TextColumn("width label", width="small"),
            "target credit": st.column_config.TextColumn("target credit", width="small"),
            "next entry (CT)": st.column_config.TextColumn("next entry (CT)", width="medium"),
            "last fired": st.column_config.TextColumn("last fired", width="medium"),
            "status": st.column_config.TextColumn("status", width="medium"),
        },
    )
    _badge_legend()


# --- 4.2 Today's open positions (STORE-sourced; works offline) ------------------
def _render_s8_open_positions(open_recs: list) -> None:
    st.markdown("#### Open positions — entry snapshot (store)")
    st.caption("Store-sourced (livebot/s8_store.py -> trades.jsonl); works offline with "
               "no Gateway. One row per still-open captured S8 credit spread for the "
               "selected session: strikes, entry credit, frozen stop_price, short-leg "
               "entry greeks, and entry spot/VIX.")
    if not open_recs:
        st.info("No open positions for the selected session.")
        return
    fmt_n, fmt_k = s8_report._fmt_num, s8_report._fmt_strike
    rows = []
    for r in open_recs:
        e = r.entry
        sl = e.short_leg if e else None
        rows.append({
            "template": r.template or "—", "slot": r.slot or "—", "side": r.side or "—",
            "short/long": (f"{fmt_k(e.short_strike)}/{fmt_k(e.long_strike)}" if e else "—"),
            "credit": fmt_n(e.realized_credit if e else None),
            "stop px": fmt_n(e.stop_price if e else None),
            "short Δ": fmt_n(sl.delta if sl else None, 3),
            "short Γ": fmt_n(sl.gamma if sl else None, 4),
            "short Θ": fmt_n(sl.theta if sl else None),
            "short IV": fmt_n(sl.iv if sl else None, 3),
            "entry spot": fmt_n(e.entry_spot if e else None),
            "entry VIX": fmt_n(e.entry_vix if e else None),
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


# --- 4.3 Live exit-monitor / distance-to-stop (RECORDED-TICK overlay; DISPLAY ONLY) ---
# The overlay reads the pilot's OWN recorded ticks — it makes ZERO Gateway contact. This
# removes the market-data-line perturbation the old per-leg quote pull risked and drops the
# wait-for-close constraint: it works DURING the session and AFTER close (once capture
# stops, the last recorded tick simply ages past the freshness threshold and is flagged
# stale rather than presented as live).

# A tick older than this (seconds) is shown as "stale" rather than as a live price. The
# pilot samples every few seconds, so a gap this large means capture is not currently live
# for that position (after the close, or a dropped subscription).
S8_TICK_STALE_SECS = 90

# The tick store writes a NEW ~50-row parquet part per buffer flush — tens of thousands
# of tiny files per session (~82k / 1GB by mid-afternoon). Scanning the whole day every
# 5 minutes takes long enough, so the overlay bounds its read to the recent TAIL of part files: the
# latest tick per position always lives in the newest parts. The window is anchored to the
# NEWEST part's mtime (not wall-clock) so it still captures each position's final tick
# after the close (shown stale) instead of an empty set. Chosen comfortably above
# S8_TICK_STALE_SECS so a position in the "stale but recent" band (90–240s) still appears
# with its age; a position with no tick in this window shows "—". NOT a strategy knob.
S8_TICK_SCAN_WINDOW_SECS = 240


def _s8_num(value):
    """None for None/NaN/blank; float otherwise. Local mirror of s8_chain._num so the
    tick reader carries no IBKR dependency. PURE."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _s8_ticks_dataframe(session_date: str, trade_ids=None):
    """Read the LATEST recorded tick per (trade_id, leg) for one session date into a
    DataFrame, via an EPHEMERAL in-memory DuckDB view — the SAME read-only pattern
    livebot/s8_report.tick_counts uses: the on-disk catalog.duckdb is NEVER opened, and
    the running collector's parquet parts are only READ. Optionally restrict to
    ``trade_ids``.

    ZERO Gateway contact — that is the whole point of the recorded-tick overlay. Any
    failure (no partition yet, missing duckdb, unreadable parts) degrades to an empty
    frame, so the overlay fail-softs to '—' exactly as the old gateway-offline path did.

    PERFORMANCE: the parts are tens of thousands of tiny files (see
    S8_TICK_SCAN_WINDOW_SECS), and ~all rows belong to the day's open positions, so
    neither a whole-day scan nor a plain ``WHERE trade_id IN (...)`` is cheap enough for a
    5-minute fragment refresh. Two bounds are applied: (1) only the recent TAIL of part files (mtime
    within S8_TICK_SCAN_WINDOW_SECS of the newest part) is handed to DuckDB; (2) the
    latest-per-leg reduction is pushed DOWN into SQL (``QUALIFY row_number() … = 1``) so
    DuckDB MATERIALIZES only ~2 rows per open position, not millions. The ``ORDER BY ts
    DESC`` is lexical on the ISO-8601 ts, matching _s8_latest_tick_per_leg's own string
    sort, so the SQL pre-trim returns exactly the row that pure selection would keep (it
    stays the canonical semantics; SQL only avoids shipping the full session to pandas).
    Empty ``trade_ids`` list -> empty frame."""
    import os

    ids = None if trade_ids is None else list(trade_ids)
    if ids is not None and not ids:
        return pd.DataFrame(columns=s8_schema.TICK_COLUMNS)
    ticks_dir = s8_store.get_root() / "ticks" / f"date={session_date}"
    try:
        parts = [(e.path, e.stat().st_mtime) for e in os.scandir(ticks_dir)
                 if e.name.endswith(".parquet")]
    except (FileNotFoundError, NotADirectoryError, OSError):
        return pd.DataFrame(columns=s8_schema.TICK_COLUMNS)
    if not parts:
        return pd.DataFrame(columns=s8_schema.TICK_COLUMNS)
    # Recent tail only — bound the scan to the newest window of parts (the latest tick per
    # position lives here), anchored to the newest part so it still works after the close.
    cutoff = max(m for _, m in parts) - S8_TICK_SCAN_WINDOW_SECS
    recent = [p for p, m in parts if m >= cutoff]
    try:
        import duckdb

        con = duckdb.connect(":memory:")
        try:
            files_sql = ",".join(
                "'" + p.replace("\\", "/").replace("'", "''") + "'" for p in recent
            )
            con.execute(
                "CREATE VIEW mon_ticks AS "
                f"SELECT * FROM read_parquet([{files_sql}], union_by_name=true)"
            )
            where = ""
            params: list = []
            if ids is not None:
                where = " WHERE trade_id IN (" + ",".join(["?"] * len(ids)) + ")"
                params = ids
            df = con.execute(
                "SELECT trade_id, ts, leg, bid, ask, last, delta, iv FROM mon_ticks"
                + where
                + " QUALIFY row_number() OVER "
                "(PARTITION BY trade_id, leg ORDER BY ts DESC) = 1",
                params,
            ).fetchdf()
        finally:
            con.close()
    except Exception:
        return pd.DataFrame(columns=s8_schema.TICK_COLUMNS)
    return df


def _s8_latest_tick_per_leg(df) -> dict:
    """PURE. Collapse a tick DataFrame to the LATEST tick per (trade_id, leg) by ts,
    returning {trade_id: {"short": {...}, "long": {...}}} where each inner dict carries
    bid/ask/last/delta/iv/ts (numbers coerced, NaN -> None). Empty / None input -> {}.
    No I/O — this is the selection the offline tests pin, mirroring the store's
    latest-wins convention but on the tick series (recorded ts are ISO-8601 with a single
    tz offset per session, so lexical order == chronological order)."""
    out: dict = {}
    if df is None or len(df) == 0:
        return out
    ordered = df.sort_values("ts")
    for (tid, leg), grp in ordered.groupby(["trade_id", "leg"], sort=False):
        row = grp.iloc[-1]
        out.setdefault(str(tid), {})[str(leg)] = {
            "bid": _s8_num(row.get("bid")),
            "ask": _s8_num(row.get("ask")),
            "last": _s8_num(row.get("last")),
            "delta": _s8_num(row.get("delta")),
            "iv": _s8_num(row.get("iv")),
            "ts": row.get("ts"),
        }
    return out


def _s8_tick_age_secs(ts_iso, now=None):
    """Seconds between ``now`` and an ISO-8601 tick timestamp; None if unparseable/blank.
    PURE (now defaults to the tick's own tz so age is well-defined offline)."""
    if not ts_iso:
        return None
    try:
        t = datetime.fromisoformat(str(ts_iso))
    except (TypeError, ValueError):
        return None
    ref = now or datetime.now(tz=t.tzinfo)
    if t.tzinfo is not None and ref.tzinfo is None:
        ref = ref.replace(tzinfo=t.tzinfo)
    return (ref - t).total_seconds()


def _s8_fmt_age(secs) -> str:
    """Compact '4s ago' / '2m03s ago' / '1h05m ago' freshness label; '—' if unknown."""
    if secs is None:
        return "—"
    s = int(round(secs))
    if s < 0:
        s = 0
    if s < 60:
        return f"{s}s ago"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m{s:02d}s ago"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m ago"


def _render_s8_live_monitor(open_recs: list, is_today: bool, session_date=None) -> None:
    st.markdown("#### Live exit-monitor — distance to stop (recorded ticks)")
    st.caption("The 'watch it happen' view. For each open position: current cost to "
               "close the spread (short ask − long bid) vs its frozen stop_price → "
               "distance to stop + running P&L. The distance/P&L math is the CANONICAL "
               "livebot/s8_monitor_core computation (s8_distance_to_stop); the prices come "
               "from the pilot's OWN RECORDED TICKS (read-only parquet, in-memory DuckDB, "
               "ZERO Gateway contact) — the latest tick per leg. Each row shows that "
               f"tick's age; a tick older than {S8_TICK_STALE_SECS}s (e.g. after the "
               "close, when capture stops) is flagged 'stale' and its live columns blanked "
               "rather than presenting old data as live. Past session / no ticks yet → "
               "'—'.")
    if not open_recs:
        st.info("No open positions to monitor for the selected session.")
        return

    fmt_n, fmt_k, fmt_s = s8_report._fmt_num, s8_report._fmt_strike, s8_report._fmt_signed
    latest: dict = {}
    if is_today and session_date:
        ids = [r.trade_id for r in open_recs]
        latest = _s8_latest_tick_per_leg(_s8_ticks_dataframe(session_date, ids))

    rows = []
    total_pnl = 0.0
    any_live = False
    any_unpriced = False
    for r in open_recs:
        e = r.entry
        legs = latest.get(r.trade_id, {})
        sq = legs.get("short")
        lq = legs.get("long")
        # Freshness: the position is only as live as its most recent leg sample; take the
        # newer of the two legs' tick ages. Stale -> do not present old prices as live.
        age = None
        for q in (sq, lq):
            if q and q.get("ts"):
                a = _s8_tick_age_secs(q["ts"])
                if a is not None and (age is None or a < age):
                    age = a
        stale = age is not None and age > S8_TICK_STALE_SECS
        short_ask = (sq.get("ask") if sq else None) if not stale else None
        long_bid = (lq.get("bid") if lq else None) if not stale else None
        d = s8_distance_to_stop(r, short_ask, long_bid)
        if d["running_pnl"] is not None:
            any_live = True
            total_pnl += d["running_pnl"]
        else:
            any_unpriced = True
        if stale:
            state = "stale"
        elif d["stopped"]:
            state = "STOPPED"
        elif d["running_pnl"] is not None:
            state = "live"
        else:
            state = "no tick"
        rows.append({
            "template": r.template or "—", "slot": r.slot or "—", "side": r.side or "—",
            "short/long": (f"{fmt_k(e.short_strike)}/{fmt_k(e.long_strike)}" if e else "—"),
            "stop px": fmt_n(d["stop_price"]),
            "cost to close": fmt_n(d["spread_cost"]),
            "dist to stop": fmt_s(d["distance_to_stop"]),
            "running P&L $": (f"{d['running_pnl']:,.0f}" if d["running_pnl"] is not None else "—"),
            "cur short Δ": fmt_n((sq.get("delta") if sq else None) if not stale else None, 3),
            "cur short IV": fmt_n((sq.get("iv") if sq else None) if not stale else None, 3),
            "tick age": _s8_fmt_age(age),
            "state": state,
        })
    if any_live:
        label = "Running P&L on open positions (live from recorded ticks)"
        if any_unpriced:
            label += " (partial — some positions unpriced/stale this cycle)"
        st.metric(label, f"${total_pnl:,.0f}")
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    if not is_today:
        st.caption("Past session selected — positions shown without a live overlay "
                   "(the recorded-tick overlay applies to today's session only).")
    elif not any_live:
        st.caption("No fresh recorded ticks for the open positions this cycle — live "
                   "columns blank; they populate as the pilot records ticks (and go stale "
                   "after the close, which is expected).")


# --- 4.4 Account / margin snapshot ----------------------------------------------
def _s8_account_summary(ib) -> dict:
    """AccountType/BuyingPower/ExcessLiquidity from the live-trading connection's own
    accountSummary() — the exact same read s8_risk.py's margin_preflight() consumes.
    These are the live-trading account's own numbers (the connected login serves its own
    summary), not any paper DU sub-account."""
    rows = ib.accountSummary()
    m = {r.tag: r.value for r in rows}
    return {"AccountType": m.get("AccountType"), "BuyingPower": m.get("BuyingPower"),
            "ExcessLiquidity": m.get("ExcessLiquidity")}


def _render_s8_account(ib) -> None:
    st.markdown("#### Account / margin snapshot")
    st.caption("The live-trading account's own numbers (live-TRADING Gateway, port 4003 "
               "— a funded, transmit-capable test account; S8's pilot only reads it, "
               "never trades). Informational only.")
    if ib is None:
        st.info("Live-trading Gateway unreachable this cycle.")
        return
    try:
        summary = _s8_account_summary(ib)
    except Exception as exc:
        st.error(f"accountSummary() read failed: {type(exc).__name__}: {exc}")
        return
    cols = st.columns(3)
    cols[0].metric("AccountType", summary.get("AccountType") or "—")
    bp, el = summary.get("BuyingPower"), summary.get("ExcessLiquidity")
    cols[1].metric("BuyingPower", f"{float(bp):,.0f}" if bp not in (None, "") else "—")
    cols[2].metric("ExcessLiquidity", f"{float(el):,.0f}" if el not in (None, "") else "—")


# --- 4.5 Connection health -------------------------------------------------------
def _render_s8_connection_health(ib, connect_error: Exception | None, records: list) -> None:
    st.markdown("#### Connection health")
    up = _port_open("127.0.0.1", 4003)   # same cheap TCP-probe pattern as the Health
                                          # tab's paper-gateway (4002) check above
    tier = "good" if up else "bad"
    st.markdown(f"{_TIER_DOT[tier]} Live-trading Gateway (port 4003) — "
               f"{'UP' if up else 'DOWN'}")
    if ib is not None:
        st.session_state["s8_last_live_probe"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    st.caption("Last successful dashboard live probe (this session): "
              f"{st.session_state.get('s8_last_live_probe', '—')}")

    last_capture = "—"
    for rec in records:            # records are in first-seen order; last wins
        if rec.entry and rec.entry.entry_ts:
            last_capture = rec.entry.entry_ts
    st.caption(f"Last captured trade entry (from store): {last_capture}")
    if connect_error is not None and ib is None:
        st.caption(f"(dashboard connect attempt this cycle failed: "
                  f"{type(connect_error).__name__}: {connect_error})")


# --- 4.6 Closed round-trips (STORE-sourced; works offline) ----------------------
def render_s8_closed(records: list, date: str | None) -> None:
    st.markdown("#### Closed round-trips (store)")
    st.caption("Store-sourced round-trips for the selected session (works offline): "
               "entry→exit spot move, exit reason, P&L, max adverse excursion (MAE), "
               "duration, and short-leg greeks at entry vs exit. The aggregate row "
               "reuses livebot/s8_report.compute_aggregates (win rate / P&L / MAE over "
               "CLOSED trades only).")
    day_recs = _s8_records_for_date(records, date)
    closed = [r for r in day_recs if not s8_report.is_open(r)]
    if not closed:
        st.info("No closed round-trips for the selected session yet.")
        return

    agg = s8_report.compute_aggregates(day_recs)
    cols = st.columns(5)
    cols[0].metric("Closed", agg["closed_count"])
    wr = agg["win_rate"]
    cols[1].metric("Win rate", f"{wr * 100:.0f}%" if wr is not None else "—")
    cols[2].metric("Total P&L", f"${agg['total_pnl']:,.0f}"
                   if agg["total_pnl"] is not None else "—")
    cols[3].metric("Avg P&L", f"${agg['avg_pnl']:,.0f}"
                   if agg["avg_pnl"] is not None else "—")
    cols[4].metric("Avg MAE", f"${agg['avg_mae']:,.0f}"
                   if agg["avg_mae"] is not None else "—")

    fmt_n, fmt_k, fmt_s = s8_report._fmt_num, s8_report._fmt_strike, s8_report._fmt_signed
    rows = []
    for r in closed:
        e, x = r.entry, r.exit
        spot_move = None
        if e and x and e.entry_spot is not None and x.exit_spot is not None:
            spot_move = x.exit_spot - e.entry_spot
        sl = e.short_leg if e else None
        se = x.short_leg_exit if x else None
        rows.append({
            "template": r.template or "—", "slot": r.slot or "—", "side": r.side or "—",
            "short/long": (f"{fmt_k(e.short_strike)}/{fmt_k(e.long_strike)}" if e else "—"),
            "credit": fmt_n(e.realized_credit if e else None),
            "reason": (x.exit_reason if x else "—"),
            "spot move": fmt_s(spot_move),
            "P&L $": (f"{x.pnl:,.0f}" if x and x.pnl is not None else "—"),
            "MAE $": (f"{x.max_adverse_excursion:,.0f}"
                      if x and x.max_adverse_excursion is not None else "—"),
            "duration": s8_report._fmt_duration(x.duration_secs if x else None),
            "short Δ entry→exit": (f"{fmt_n(sl.delta if sl else None, 3)} → "
                                   f"{fmt_n(se.delta if se else None, 3)}"),
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True,
                height=400)


# --- Connection helper + the auto-refreshing fragment ---------------------------
def _s8_connect_readonly_short(timeout: int = 5):
    """Connect to the live-TRADING Gateway (port 4003), read-only and display-only, for
    quote-consistency with s8_runner.py (which reads the same connection). readonly=True
    is passed explicitly here — this account is transmit-capable at the broker level, but
    a monitor tab only ever reads. SHORT timeout, launch=False (an auto-refreshing 5-minute
    dashboard fragment must never be the thing that boots a Gateway). Mirrors
    render_accounts()'s own _connect_readonly_short pattern for the paper side."""
    from connections import ibkr_live_trade
    return ibkr_live_trade.connect("dashboard_s8", launch=False, readonly=True, timeout=timeout)


@st.fragment(run_every="300s")
def render_s8_live() -> None:
    """The fast-changing S8 sections (cycle log, live P&L re-mark, account snapshot,
    connection health) — wrapped in its own fragment so ONLY this part of the page
    reruns every 5 minutes, not the whole app (and not the Health/Backtests/Accounts tabs).

    5min CHOSEN BECAUSE: each refresh reconnects to the live-trading Gateway (port 4003)
    for the account/margin snapshot, so the cadence directly governs how often the tab
    churns that connection during a live session. The live exit-monitor data now comes
    from the pilot's RECORDED tick files (not a Gateway stream), so a tight 30s refresh
    bought nothing but needless reconnects; a 5-minute cadence keeps the margin panel
    fresh enough for an intraday watch while leaving the live Gateway alone between
    round trips. 5min is an operational cadence, not a tuned/curve-fit value (nothing
    here is a strategy parameter).

    The exit-monitor overlay no longer needs the Gateway at all — it reads the pilot's
    RECORDED ticks (files only). The single shared connection attempt per refresh cycle
    now feeds only the account snapshot + connection-health subsections (both reuse the
    same `ib`) rather than reconnecting per-section.
    """
    st.caption("Auto-refreshes every 5 minutes while this tab is open (see render_s8_live's "
               "docstring for why 5min). Schedule strip and Closed round-trips are read "
               "less often; the exit-monitor reads the latest RECORDED ticks for the "
               "selected session (today, live).")

    # Read the selected session (default today) from the outer date selector. The
    # open-position tables are store-sourced (offline); the exit-monitor overlay reads the
    # recorded ticks (offline); a Gateway connection is only attempted for TODAY's
    # account/health panels, and only when there is something open to monitor.
    date = st.session_state.get("s8_view_date")
    today = datetime.now(tz=CT_ZONE).strftime("%Y%m%d")
    is_today = (date is None) or (date == today)
    session_date = date or today
    records = _s8_store_records()
    day_recs = _s8_records_for_date(records, date)
    open_recs = [r for r in day_recs if s8_report.is_open(r)]

    _render_s8_open_positions(open_recs)
    st.divider()

    # The exit-monitor overlay reads the pilot's RECORDED ticks (files only) — it takes
    # no Gateway connection at all, so it renders before (and independent of) the live
    # connect below.
    _render_s8_live_monitor(open_recs, is_today, session_date=session_date)
    st.divider()

    # The account/margin snapshot + connection health still read the live-trading Gateway
    # (a light accountSummary + a cheap TCP probe) — UNCHANGED. Only the exit-monitor
    # overlay above was repointed off the Gateway onto the recorded ticks.
    ib = None
    connect_error: Exception | None = None
    if is_today and open_recs:
        try:
            ib = _s8_connect_readonly_short()
        except Exception as exc:
            connect_error = exc

    try:
        _render_s8_account(ib)
        st.divider()
        _render_s8_connection_health(ib, connect_error, records)
    finally:
        if ib is not None:
            try:
                ib.disconnect()
            except Exception:
                pass


def render_s8() -> None:
    st.subheader("S8 — British IC + B2 (SPX/SPXW 0DTE)")
    st.caption("PILOT_MODE monitor over the S8 capture store (livebot/s8_store.py -> "
               "trades.jsonl). livebot/s8_service.py hardcodes PILOT_MODE=True and never "
               "transmits an order — this tab is a read-only window onto the captured "
               "trade records plus a live quote overlay for display only. It never "
               "places, arms, or transmits anything, and never writes to the store.")

    records = _s8_store_records()
    render_s8_schedule(records)
    st.divider()

    # Date selector (default = most recent captured session, i.e. today when live). It
    # lives OUTSIDE the 5-minute fragment (so it is not reset every refresh); the fragment and
    # the closed-round-trips panel both read the selection back via session_state.
    dates = _s8_available_dates(records)
    chosen: str | None = None
    if dates:
        chosen = st.selectbox(
            "Session date", dates, index=0, key="s8_view_date",
            format_func=lambda d: f"{d[:4]}-{d[4:6]}-{d[6:]}",
        )
    else:
        st.info("No S8 trades captured yet — the store (trades.jsonl) is empty. This is "
                "the expected state before the first session; panels populate as the "
                "live service captures trades.")
    st.divider()

    render_s8_live()
    st.divider()
    render_s8_closed(records, chosen)


# ================================= LAYOUT =====================================
def main() -> None:
    st.title("📊 Trading Desk — S0")
    st.caption(f"As of {last_refreshed()} (newest available data) · auto-refreshes "
               "as caches expire (status/JSON 60s, backtests cached 1h).")
    st.caption("Read-only dashboard · scoped to S0 (Adaptive All-Weather Core) · "
               "paper account only · nothing here places, arms, or transmits any order.")

    tabs = st.tabs(["🩺 Health", "🧪 Backtests", "💼 Accounts", "📈 S8"])
    with tabs[0]:
        render_health()
    with tabs[1]:
        render_backtests()
    with tabs[2]:
        render_accounts()
    with tabs[3]:
        render_s8()


if __name__ == "__main__":
    main()
