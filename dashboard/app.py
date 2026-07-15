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
                   auto-close): today's 11-template entry schedule, s8_runner.py's
                   own ledger'd cycle log, a DISPLAY-ONLY live re-mark of today's
                   still-open hypothetical picks against current quotes, the live
                   account's own margin snapshot, and live-TRADING Gateway (port 4003)
                   connection health. See render_s8() below for the read-only/
                   never-feeds-back guarantees specific to this tab.

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
    ever calls ib.accountSummary(), ib.qualifyContracts(), ib.reqMktData()/
    cancelMktData(), and ib.reqTickers() (all reads) — no order object is ever built or
    placed from this tab.
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
for sub in ("paperbot", "backtester", "connections", "strategies", "dailyreport"):
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
# at import time — s8_config is data-only, s8_strategy/s8_chain's own IBKR imports are
# just class references (IB/Index/Option), never a connect() call, and ledger.py only
# defines a path constant). connections.ibkr_live_trade itself (the thing that actually
# opens a socket) stays a LAZY import inside _s8_connect_readonly_short(), same
# convention render_accounts() already uses for `from connections import ibkr_paper`.
import ledger
import s8_chain
import s8_config
import s8_strategy

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
# strategy, still in PILOT_MODE (paperbot/s8_runner.py hardcodes PILOT_MODE=True and
# never calls order_router.place()/ib.placeOrder() — see that module's own docstring).
# This tab is a pure MONITOR: it reads s8_runner.py's ledger records (paperbot/
# ledger.py -> C:\TradingDesk-Local\state\paperbot\runs.jsonl, filtered on
# mode == "s8_live_pilot" — the only tag ledger.record_run() actually carries; there
# is no separate strategy/runner field, confirmed by reading both ledger.py and every
# record_run() call site in s8_runner.py) and, for TODAY's still-open logged picks
# only, layers on a live re-mark against CURRENT IBKR quotes PURELY FOR DISPLAY.
#
# Until Andrew provides the S8 live-trading TEST account (s8_config.ACCOUNT is "TBD")
# and brings the live-TRADING Gateway up, s8_runner.py refuses to run, so every ledger
# read below WILL come back empty and every live read WILL degrade to "offline". That is
# the expected, correctly-handled state for this tab, not a bug.
CT_ZONE = ZoneInfo("America/Chicago")     # matches s8_config.ENTRY_GRID_CT's own convention
_ET_ZONE = ZoneInfo("America/New_York")   # SPX/SPXW PM settlement is stated in ET
_S8_SETTLEMENT_ET = dt_time(16, 0)        # 16:00 ET, same instant s5_harvest_engine.py uses

# Pilot default: mirrors s8_runner.py's own QTY_PER_ENTRY (=1) for the dollar math
# below. Duplicated as a small local constant rather than importing s8_runner.py itself
# into the dashboard process — s8_runner.py is a scheduled-task ENTRY POINT (it reads
# sys.argv-free module-level state and wires mailer/order_router/ledger at import time);
# a monitor page has no business executing that script's import side effects merely to
# read one integer. If S8's real position size ever becomes a genuine per-trade
# variable (not a pilot constant), this line must be revisited alongside that change.
S8_QTY_PER_ENTRY = 1
S8_CONTRACT_MULTIPLIER = 100.0  # standard SPX/SPXW index-option multiplier


@st.cache_data(ttl=20)
def _s8_read_today_records() -> list[dict]:
    """Today's S8 ledger records, read-only. ledger.py is append-only and this function
    never writes to it. Cached 20s (short — this feeds the fast-refresh fragment)."""
    path = ledger.RUNS_JSONL
    if not os.path.exists(path):
        return []
    today_str = datetime.now().strftime("%Y-%m-%d")
    out: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("mode") != "s8_live_pilot":
                    continue
                if not str(rec.get("ts", "")).startswith(today_str):
                    continue
                out.append(rec)
    except OSError:
        return []
    return out


@st.cache_data(ttl=300)
def _s8_read_all_records(max_rows: int = 2000) -> list[dict]:
    """Every S8 ledger record ever written, oldest-first, capped to the most recent
    max_rows so a long-lived ledger can't blow up dashboard memory. Cached 5 minutes —
    history changes far less often than the live cycle log; read-only, never writes."""
    path = ledger.RUNS_JSONL
    if not os.path.exists(path):
        return []
    out: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("mode") == "s8_live_pilot":
                    out.append(rec)
    except OSError:
        return []
    return out[-max_rows:]


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


def _s8_last_fired(records: list[dict], template: str) -> str:
    """Latest cycle ts where `template` appeared in that cycle's due_templates — the
    only per-template timestamp granularity ledger.py actually stores (there is no
    separate per-template log line, only a per-cycle record with a due_templates list
    and a results list); records are read in file order so the last match is the most
    recent."""
    latest = "—"
    for rec in records:
        if template in (rec.get("due_templates") or []):
            latest = rec.get("ts", latest)
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


def render_s8_schedule() -> None:
    st.markdown("#### Today's schedule — all 11 templates")
    st.caption("Times are CT (US/Central), matching s8_config.ENTRY_GRID_CT's own "
               "convention. Sorted by each template's own first scheduled entry time "
               "(fixed all day — not reordered by the clock); the 5 templates flagged "
               "NO SCHEDULE DATA are real gaps in the real-fills MATCHED sample (too "
               "few observations to name a grid, per s8_config.py's own comment) and "
               "are grouped at the bottom rather than interspersed.")
    now_ct = datetime.now(tz=CT_ZONE).time()
    records = _s8_read_today_records()
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


# --- 4.2 Live cycle log ---------------------------------------------------------
def _render_s8_cycle_log() -> None:
    st.markdown("#### Live cycle log (today)")
    records = _s8_read_today_records()
    if not records:
        st.info("No S8 runner cycles logged yet today — expected until the S8 "
                 "live-trading TEST account is set (s8_config.ACCOUNT is 'TBD') and the "
                 "scheduled task begins firing.")
        return
    lo, hi = s8_strategy._REAL_DELTA_BAND
    rows = []
    for rec in records:
        ts = rec.get("ts", "—")
        if rec.get("error"):
            rows.append({
                "cycle ts": ts, "template": ", ".join(rec.get("due_templates") or []),
                "chain snapshot": "FAIL", "short/long": "—", "width": "—", "credit": "—",
                "delta flag": "", "margin gate": "—", "would transmit": rec["error"],
            })
            continue
        for item in rec.get("results", []):
            pick = item.get("pick")
            delta_flag = ""
            if pick and pick.get("short_delta") is not None:
                d = pick["short_delta"]
                if not (lo <= d <= hi):
                    delta_flag = f"FLAG ({d:.3f} outside [{lo:.2f},{hi:.2f}])"
            preflight = item.get("preflight")
            margin_gate = ("OK" if preflight and preflight.get("ok")
                           else ("REFUSED: " + "; ".join(preflight.get("reasons", []))
                                 if preflight else "—"))
            rows.append({
                "cycle ts": ts, "template": item.get("template"),
                "chain snapshot": "ok",
                "short/long": (f"{pick['short_strike']:g}/{pick['long_strike']:g}"
                              if pick else "—"),
                "width": f"{pick['width']:.0f}" if pick else "—",
                "credit": f"{pick['realized_credit']:.2f}" if pick else "—",
                "delta flag": delta_flag,
                "margin gate": margin_gate,
                "would transmit": (item.get("would_transmit") or item.get("reason")
                                  or item.get("error") or "—"),
            })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


# --- 4.3 Estimated P&L on hypothetical picks (LIVE re-mark; DISPLAY ONLY) -------
def _s8_settlement_intrinsic(spot_settle: float, side: str, short_strike: float,
                             width: float) -> float:
    """Adapted from backtester/s5_harvest_engine.py::_settlement_intrinsic for S8's
    single-sided vertical (puts OR calls, never a condor's both sides at once) with its
    own PER-PICK width (S8's width varies pick to pick — NOT s5's fixed 5.0-point wing).
    Cash-settled European intrinsic, clipped to the defined-risk wing width."""
    if side == "PUT":
        return min(max(short_strike - spot_settle, 0.0), width)
    return min(max(spot_settle - short_strike, 0.0), width)   # CALL


def _s8_pick_id(pick_row: dict) -> str:
    return (f"{pick_row['cycle_ts']}|{pick_row['template']}|"
            f"{pick_row['short_strike']}|{pick_row['long_strike']}")


def _s8_reprice_pick(pick_row: dict, cfg: dict, short_q, long_q,
                     spot_settle: float | None, phase: str) -> dict:
    """PURE DISPLAY MATH for one hypothetical S8 pick.

    *** THIS FUNCTION CANNOT AFFECT ANY REAL DECISION. *** Its only caller is
    _render_s8_pnl() below, its only output is a dict rendered on this page, it never
    writes to ledger.py, and s8_runner.py / s8_strategy.py have no import of
    dashboard/app.py anywhere — there is no code path by which this math could reach
    the real PILOT_MODE decision loop. This whole function could be deleted with zero
    effect on the actual strategy.

    Stop check: reuses s8_strategy.stop_price(entry_credit, stop_multiple) (the frozen
    formula) and compares it to the SHORT leg's own current cost-to-close (its live
    ask) — the same honest-fill convention (sell short at bid, buy back at ask) already
    established in s8_strategy.py/s8_runner.py. Once stopped, the P&L is FROZEN at
    exactly (entry_credit - stop_price) — the constant-dollar stop's own definition —
    rather than kept re-marking a hypothetically-closed position.

    Open mark: a FULL SPREAD mark (both legs), not just the short leg — current cost to
    close the spread = short_ask_now - long_bid_now (buy back short at its ask, sell
    long at its bid); P&L = entry realized_credit - that cost. This is algebraically
    the same honest-fill convention applied to both legs
    ((short_bid_entry - short_ask_now) + (long_bid_now - long_ask_entry)
      == realized_credit - (short_ask_now - long_bid_now))
    without needing the individual per-leg entry prices ledger.py doesn't separately
    store (only the net realized_credit is logged).

    Settlement mark: past today's 16:00 ET settlement instant, marks to intrinsic at
    spot via _s8_settlement_intrinsic() above (adapted from
    backtester/s5_harvest_engine.py's proven _settlement_intrinsic), then freezes.
    """
    side = "PUT" if cfg["side"] == "Puts" else "CALL"
    stop_px = s8_strategy.stop_price(pick_row["realized_credit"], cfg["stop_multiple"])
    qty = S8_QTY_PER_ENTRY

    if phase == "past_settlement" and spot_settle is not None:
        debit = _s8_settlement_intrinsic(spot_settle, side, pick_row["short_strike"],
                                         pick_row["width"])
        pnl_pts = pick_row["realized_credit"] - debit
        return {"status": "settled", "pnl_points": pnl_pts,
                "pnl_dollars": pnl_pts * S8_CONTRACT_MULTIPLIER * qty,
                "note": f"marked to intrinsic @ spot {spot_settle:.2f}", "frozen": True}

    short_bid, short_ask = short_q if short_q else (None, None)
    if short_ask is not None and short_ask >= stop_px:
        pnl_pts = pick_row["realized_credit"] - stop_px
        return {"status": "stopped", "pnl_points": pnl_pts,
                "pnl_dollars": pnl_pts * S8_CONTRACT_MULTIPLIER * qty,
                "note": f"short ask {short_ask:.2f} >= stop {stop_px:.2f} (frozen)",
                "frozen": True}

    long_bid, long_ask = long_q if long_q else (None, None)
    if short_ask is not None and long_bid is not None:
        cost_to_close = short_ask - long_bid
        pnl_pts = pick_row["realized_credit"] - cost_to_close
        return {"status": "open", "pnl_points": pnl_pts,
                "pnl_dollars": pnl_pts * S8_CONTRACT_MULTIPLIER * qty,
                "note": f"live mark: short ask {short_ask:.2f}, long bid {long_bid:.2f}",
                "frozen": False}

    return {"status": "quote unavailable", "pnl_points": None, "pnl_dollars": None,
            "note": "no live two-sided quote for this strike this cycle", "frozen": False}


def _s8_targeted_quotes(ib, expiration: str, needs: list[tuple[str, float]]) -> dict:
    """Lighter targeted quote pull for exactly the strikes today's live P&L re-mark
    needs (2 legs per still-open pick), reusing s8_chain.py's own contract-construction
    pattern (SPX/SPXW 0DTE Option, SMART exchange, tradingClass SPXW) rather than
    inventing a new one. Deliberately NOT s8_chain.snapshot_0dte_chain()'s full
    near-money sweep (up to ~250 contracts, ~18s, meant for a pre-entry decision) — a
    30s auto-refresh fragment re-pricing a handful of open picks only needs the exact
    strikes involved. Settle window shortened to 3s (vs snapshot_0dte_chain's 6s)
    since this pulls a handful of lines, not up to 3 full LINE_LIMIT batches — a
    judgment call for this lighter, narrower ask, not a re-tuning of the proven
    snapshot's own pacing constants."""
    from ib_async import Option
    contracts = [
        Option("SPX", expiration, strike, right, "SMART",
              tradingClass=s8_chain._SPXW_TRADING_CLASS, currency="USD")
        for right, strike in needs
    ]
    contracts = [c for c in (ib.qualifyContracts(*contracts) or []) if c and c.conId]
    if not contracts:
        return {}
    tickers = [ib.reqMktData(c, "", False, False) for c in contracts]
    ib.sleep(3)
    out = {}
    for c, t in zip(contracts, tickers):
        out[(c.right, float(c.strike))] = (s8_chain._num(t.bid), s8_chain._num(t.ask))
    for c in contracts:
        ib.cancelMktData(c)
    return out


def _render_s8_pnl(ib) -> None:
    st.markdown("#### Estimated P&L on today's hypothetical picks (live re-mark)")
    st.caption("DISPLAY ONLY — re-marks today's still-open LOGGED picks against "
               "current quotes. This math lives entirely in dashboard/app.py and is "
               "never written to the ledger and never fed back into s8_runner.py's "
               "real PILOT_MODE decision loop — see _s8_reprice_pick()'s docstring for "
               "exactly why that coupling is structurally impossible here.")

    records = _s8_read_today_records()
    picks = [
        {"cycle_ts": rec.get("ts"), "template": item["template"], **item["pick"]}
        for rec in records
        for item in rec.get("results", [])
        if item.get("pick") and item.get("would_transmit")
    ]
    if not picks:
        st.info("No approved (would-have-transmitted) picks logged today yet.")
        return

    frozen: dict = st.session_state.setdefault("s8_frozen_pnl", {})
    now_et = datetime.now(tz=_ET_ZONE)
    phase = "past_settlement" if now_et.time() >= _S8_SETTLEMENT_ET else "intraday"

    to_query = [p for p in picks if _s8_pick_id(p) not in frozen]
    quotes: dict = {}
    spot_settle = None
    if ib is not None and to_query:
        try:
            expiration = to_query[0]["cycle_ts"][:10].replace("-", "")
            needs = set()
            for p in to_query:
                side = "PUT" if s8_config.TEMPLATES[p["template"]]["side"] == "Puts" else "CALL"
                needs.add((side, p["short_strike"]))
                needs.add((side, p["long_strike"]))
            quotes = _s8_targeted_quotes(ib, expiration, sorted(needs))
            if phase == "past_settlement":
                try:
                    _, spot_settle = s8_chain.get_underlying(ib)
                except Exception:
                    spot_settle = None
        except Exception as exc:
            st.caption(f"Live quote pull failed this cycle: {type(exc).__name__}: {exc}")

    rows = []
    total_dollars = 0.0
    any_unknown = False
    for p in picks:
        pid = _s8_pick_id(p)
        cfg = s8_config.TEMPLATES[p["template"]]
        if pid in frozen:
            result = frozen[pid]
        else:
            side = "PUT" if cfg["side"] == "Puts" else "CALL"
            short_q = quotes.get((side, p["short_strike"]))
            long_q = quotes.get((side, p["long_strike"]))
            result = _s8_reprice_pick(p, cfg, short_q, long_q, spot_settle, phase)
            if result.get("frozen"):
                frozen[pid] = result
        if result["pnl_dollars"] is None:
            any_unknown = True
        else:
            total_dollars += result["pnl_dollars"]
        rows.append({
            "template": p["template"], "entered": p["cycle_ts"],
            "short/long": f"{p['short_strike']:g}/{p['long_strike']:g}",
            "entry credit": f"{p['realized_credit']:.2f}",
            "status": result["status"],
            "est P&L $": (f"{result['pnl_dollars']:,.0f}"
                         if result["pnl_dollars"] is not None else "—"),
            "note": result["note"],
        })

    label = "Total estimated hypothetical P&L (today)"
    if any_unknown:
        label += " (partial — some picks unpriced this cycle)"
    st.metric(label, f"${total_dollars:,.0f}")
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    if ib is None:
        st.caption("Live-trading Gateway unreachable this cycle — showing logged entry "
                   "data only; live re-mark resumes once the Gateway is reachable.")


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
def _render_s8_connection_health(ib, connect_error: Exception | None) -> None:
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

    last_snapshot_ts = "—"
    for rec in reversed(_s8_read_all_records(max_rows=500)):
        if not rec.get("error"):
            last_snapshot_ts = rec.get("ts", "—")
            break
    st.caption(f"Last successful runner chain snapshot (from ledger): {last_snapshot_ts}")
    if connect_error is not None and ib is None:
        st.caption(f"(dashboard connect attempt this cycle failed: "
                  f"{type(connect_error).__name__}: {connect_error})")


# --- 4.6 History view ------------------------------------------------------------
def render_s8_history() -> None:
    st.markdown("#### History (all logged S8 cycles)")
    records = _s8_read_all_records()
    if not records:
        st.info("No S8 ledger history yet.")
        return
    dates = sorted({r.get("ts", "")[:10] for r in records if r.get("ts")}, reverse=True)
    chosen = st.selectbox("Date", ["(all)"] + dates, key="s8_hist_date")
    filtered = (records if chosen == "(all)"
               else [r for r in records if r.get("ts", "").startswith(chosen)])
    rows = []
    for rec in filtered:
        ts = rec.get("ts", "—")
        if rec.get("error"):
            rows.append({"cycle ts": ts, "template": ", ".join(rec.get("due_templates") or []),
                        "pick": "—", "outcome": rec["error"]})
            continue
        for item in rec.get("results", []):
            pick = item.get("pick")
            rows.append({
                "cycle ts": ts, "template": item.get("template"),
                "pick": (f"{pick['short_strike']:g}/{pick['long_strike']:g} @ "
                        f"{pick['realized_credit']:.2f}") if pick else "—",
                "outcome": (item.get("would_transmit") or item.get("reason")
                           or item.get("error") or "—"),
            })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True,
                height=400)


# --- Connection helper + the auto-refreshing fragment ---------------------------
def _s8_connect_readonly_short(timeout: int = 5):
    """Connect to the live-TRADING Gateway (port 4003), read-only and display-only, for
    quote-consistency with s8_runner.py (which reads the same connection). readonly=True
    is passed explicitly here — this account is transmit-capable at the broker level, but
    a monitor tab only ever reads. SHORT timeout, launch=False (an auto-refreshing 30s
    dashboard fragment must never be the thing that boots a Gateway). Mirrors
    render_accounts()'s own _connect_readonly_short pattern for the paper side."""
    from connections import ibkr_live_trade
    return ibkr_live_trade.connect("dashboard_s8", launch=False, readonly=True, timeout=timeout)


@st.fragment(run_every="30s")
def render_s8_live() -> None:
    """The fast-changing S8 sections (cycle log, live P&L re-mark, account snapshot,
    connection health) — wrapped in its own fragment so ONLY this part of the page
    reruns every 30s, not the whole app (and not the Health/Backtests/Accounts tabs).

    30s CHOSEN BECAUSE: (a) a live IBKR round trip here (qualify + reqMktData +
    a 3s settle) takes several real seconds, so 30s leaves comfortable headroom
    between round trips rather than queuing up overlapping connections; (b) it's the
    same order of magnitude as this desk's other live-polling cadences (e.g.
    GatewayWatchdog, ~1min) — fast enough to feel "live" for someone actively watching
    an intraday monitor, without hammering a connection s8_runner.py's own scheduled
    cycles are also using. A tighter interval (e.g. 5-10s) would risk stacking
    overlapping IBKR round trips if one settle window ever runs long; a much looser one
    (e.g. 2min) would feel stale for a same-day P&L watch. 30s is the sensible middle,
    not a tuned/curve-fit value (nothing here is a strategy parameter).

    ONE shared connection attempt per refresh cycle feeds all four subsections below
    (cycle log needs none; P&L, account, and connection-health all reuse the same `ib`)
    rather than reconnecting per-section — halves the round trips of a naive per-
    section connect.
    """
    st.caption("Auto-refreshes every 30s while this tab is open (see render_s8_live's "
               "docstring for why 30s). Schedule strip and History are cached longer — "
               "they change far less during the day.")

    ib = None
    connect_error: Exception | None = None
    try:
        ib = _s8_connect_readonly_short()
    except Exception as exc:
        connect_error = exc

    try:
        _render_s8_cycle_log()
        st.divider()
        _render_s8_pnl(ib)
        st.divider()
        _render_s8_account(ib)
        st.divider()
        _render_s8_connection_health(ib, connect_error)
    finally:
        if ib is not None:
            try:
                ib.disconnect()
            except Exception:
                pass


def render_s8() -> None:
    st.subheader("S8 — British IC + B2 (SPX/SPXW 0DTE)")
    st.caption("PILOT_MODE monitor. paperbot/s8_runner.py hardcodes PILOT_MODE=True "
               "and never transmits an order — this tab is a read-only window onto "
               "its ledger plus a live quote re-mark for display only. This tab never "
               "places, arms, or transmits anything, and never writes to the ledger.")

    render_s8_schedule()
    st.divider()
    render_s8_live()
    st.divider()
    render_s8_history()


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
