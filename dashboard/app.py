"""
app.py — Trading Desk unified dashboard (Phase 1). READ-ONLY.

A single Streamlit app, phone- and desktop-friendly, that *shows* the state of the
whole desk by reusing the existing Python directly. It NEVER places, arms, or
transmits an order, never writes to the warehouse/config, never calls replaceFA.

Five sections (tabs):
  1. Health      — EDGAR fundamentals freshness/coverage (periodic refresh), EOD
                   warehouse coverage, the status JSONs (forward/tiingo/gex/
                   eod_report), Windows task states. (The SPXW 1-min one-time-grab
                   panel is retired — backfill complete 2026-07.)
  2. Gamma (GEX) — latest SPX/SPXW/SPY dealer-gamma snapshot + a history chart.
  3. Backtests   — latest CAGR/maxDD/Calmar/Sortino/down-capture for the 3 versions
                   (computed via the validated run_backtest, cached) + links to the
                   existing plotly HTML reports.
  4. S5 Convexity — read-only research view of the S5 financed-convexity overlay
                   EOD prototype ledger (defensive half only; recomputed live).
  5. Accounts    — the 5 DU paper subs read read-only through the gateway, with a
                   SHORT connect timeout so a weekend/feed-down gateway degrades to
                   "Gateway offline" instead of hanging. Drift vs target + the
                   rebalance PLAN preview (build-only). NO controls anywhere.

Launch (see run_dashboard.bat): binds 0.0.0.0 so a phone on the LAN can reach it.

Read-only guarantees in this file:
  * The only broker call path is ibkr.connect(readonly=True) with a short timeout,
    plus ib.managedAccounts / accountSummary / positions / reconcile (all reads) and
    rebalance_run.build_preview (a PURE build-only planner — no order objects, no send).
  * No order_router.place/arm, no ib.placeOrder, no replaceFA, no file writes.
"""
from __future__ import annotations

import io
import json
import os
import sys
import contextlib
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st
import plotly.graph_objects as go

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
# the other folders above — import its modules directly.
import desk_health
import status as dr_status

# --- Local data locations (off-Drive, on C:) -----------------------------------
WAREHOUSE = Path(r"C:\TradingDesk-Local\warehouse")
DERIVED = WAREHOUSE / "derived"
PROGRESS_JSON = WAREHOUSE / "spxw_1m_progress.json"  # retired panel (backfill done 2026-07); kept for reversibility
STATUS_DIR = Path(r"C:\TradingDesk-Local\state\dailyreport\status")
BACKTEST_OUTPUT = REPO / "backtester" / "output"
# S5 convexity-ledger experiment summary (head-to-head + tail-sizing frontier tables).
# The full per-day NAV/ledger time series is recomputed live via simulate_s5 (fast, cached).
S5_LEDGER_CSV = BACKTEST_OUTPUT / "s5_ledger_experiment_20260630.csv"
S5_TAIL_SWEEP_MD = BACKTEST_OUTPUT / "s5_tail_sweep_20260628.md"

# EDGAR point-in-time fundamentals warehouse (off-Drive, on C:). A PERIODIC
# (monthly-ish) refresh — monitored here as freshness/coverage, not a daily feed.
EDGAR = Path(r"C:\TradingDesk-Local\canslim\edgar")
EDGAR_FUNDAMENTALS = EDGAR / "quarterly_fundamentals.parquet"
EDGAR_STALE_DAYS = 45   # periodic-refresh threshold

# Windows scheduled tasks that drive the desk (name -> friendly label).
# (Spxw1mCollector retired — the SPXW 1-min one-time backfill is complete 2026-07.)
SCHEDULED_TASKS = {
    "ThetaEodDaily": "EOD options (ThetaData)",
    "ThetaForwardDaily": "Forward EOD grab",
    "TiingoDailyUpdate": "Tiingo equity EOD",
    "GexDailyBuild": "GEX daily build",
    "EodReport": "EOD status report",
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


def _fmt_big(x: float, nd: int = 2) -> str:
    """One shared magnitude formatter: raw dollars/notional -> a compact B/M/K string.
    Used so GEX numbers read consistently across the snapshot tiles, chart and history.
    Thin wrapper over the shared formatter — see desk_health.fmt_magnitude."""
    return desk_health.fmt_magnitude(x, nd=nd)


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
    """Read Windows Task Scheduler states for the desk's jobs (read-only). Best-effort:
    returns {label: state} and degrades to {} if PowerShell/schtasks isn't reachable."""
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
@st.cache_data(ttl=120)
def eod_coverage() -> tuple[pd.DataFrame, dict]:
    """EOD warehouse coverage straight from the derived GEX parquet files (no DB lock
    contention with the live collector). Returns (per-symbol table, summary dict)."""
    files = sorted(DERIVED.glob("*_gex_daily.parquet"))
    rows = []
    for f in files:
        sym = f.name.replace("_gex_daily.parquet", "")
        try:
            d = pd.read_parquet(f, columns=["date"])
            rows.append({
                "symbol": sym,
                "days": len(d),
                "first": str(d["date"].min()),
                "last": str(d["date"].max()),
            })
        except Exception:
            rows.append({"symbol": sym, "days": 0, "first": "—", "last": "—"})
    df = pd.DataFrame(rows)
    summary = {
        "n_symbols": len(df),
        "total_day_rows": int(df["days"].sum()) if not df.empty else 0,
        "latest_day": str(df["last"].max()) if not df.empty else "—",
    }
    return df, summary


@st.cache_data(ttl=120)
def edgar_coverage() -> dict:
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
    """Newest of every available data timestamp: collector `updated`, the status
    JSON `ts` values, latest GEX `date`, and the newest backtest report mtime."""
    cands: list[datetime] = []
    try:
        ec = edgar_coverage()
        d = _parse_ts(ec.get("last_refresh"))
        if d:
            cands.append(d)
    except Exception:
        pass
    for name in ("forward", "tiingo", "gex", "eod_report"):
        js = load_json(str(STATUS_DIR / f"{name}.json"))
        if js:
            d = _parse_ts(js.get("ts")) or _parse_ts(js.get("date"))
            if d:
                cands.append(d)
    try:
        _, summ = eod_coverage()
        d = _parse_ts(summ.get("latest_day"))
        if d:
            cands.append(d)
    except Exception:
        pass
    for v in BACKTEST_VERSIONS:
        f = BACKTEST_OUTPUT / f"backtest_report_{v}.html"
        if f.exists():
            cands.append(datetime.fromtimestamp(f.stat().st_mtime))
    if not cands:
        return "—"
    return max(cands).strftime("%Y-%m-%d %H:%M")


@st.cache_data(ttl=60)
def desk_status() -> dict:
    """Roll the pipeline up into one home-screen summary. Read-only; reuses the
    same JSONs/parquet the detail sections show."""
    # Overall pipeline health = worst tier across the status JSONs.
    order = {"good": 0, "unknown": 1, "warn": 2, "bad": 3}
    worst, worst_name = "good", "—"
    any_status = False
    for name in ("forward", "tiingo", "gex", "eod_report"):
        js = load_json(str(STATUS_DIR / f"{name}.json"))
        if not js:
            continue
        any_status = True
        shown = js.get("metrics", {}).get("overall") or js.get("status", "")
        t = _status_tier(shown)
        if order[t] > order[worst]:
            worst, worst_name = t, name
    if not any_status:
        worst = "unknown"

    try:
        ec = edgar_coverage()
    except Exception:
        ec = {"tier": "unknown", "state": "—", "companies": None, "age_days": None}

    try:
        _, summ = eod_coverage()
        latest_day = summ.get("latest_day", "—")
    except Exception:
        latest_day = "—"

    states = scheduled_task_states()
    not_ready = sum(1 for s in states.values() if str(s).lower() != "ready")
    n_tasks = len(states)

    return {
        "overall_tier": worst, "overall_name": worst_name,
        "edgar_tier": ec.get("tier", "unknown"), "edgar_state": ec.get("state", "—"),
        "edgar_companies": ec.get("companies"), "edgar_age_days": ec.get("age_days"),
        "latest_day": latest_day,
        "tasks_not_ready": not_ready, "tasks_total": n_tasks,
    }


def render_health() -> None:
    st.subheader("Desk health")

    # --- At-a-glance desk status (the "is anything broken?" row) ---
    s = desk_status()
    cols = st.columns(4)
    with cols[0]:
        tier = s["overall_tier"]
        label = {"good": "Healthy", "warn": "Warning",
                 "bad": "Problem", "unknown": "Unknown"}[tier]
        st.metric("Pipeline health", f"{_TIER_DOT[tier]} {label}", border=True)
        if tier in ("warn", "bad") and s["overall_name"] != "—":
            st.caption(f"worst: {s['overall_name']}")
    with cols[1]:
        et = s["edgar_tier"]
        companies = s.get("edgar_companies")
        val = f"{companies:,}" if companies is not None else "—"
        st.metric("EDGAR companies", f"{_TIER_DOT[et]} {val}", border=True)
        age = s.get("edgar_age_days")
        st.caption(f"{s['edgar_state']}"
                   + (f" · {age}d old" if age is not None else ""))
    with cols[2]:
        st.metric("Latest warehouse day", s["latest_day"], border=True)
    with cols[3]:
        nr = s["tasks_not_ready"]
        st.metric("Tasks not Ready", f"{nr} / {s['tasks_total']}",
                  delta=("all Ready" if nr == 0 and s["tasks_total"] else None),
                  delta_color="off", border=True)
        is_weekend = datetime.now().weekday() >= 5
        st.caption("Weekend — gateway offline is expected"
                   if is_weekend else "Weekday — gateway should be up")
    _badge_legend()

    st.divider()

    # --- EDGAR fundamentals freshness / coverage ---
    # (The SPXW 1-min one-time-grab panel is retired: backfill complete 2026-07.)
    # EDGAR is a PERIODIC (monthly-ish) refresh — shown as freshness/coverage, not a
    # nightly download. Handles a partially-built (mid-refresh) warehouse gracefully.
    ec = edgar_coverage()
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

    st.divider()

    # --- EOD warehouse coverage ---
    st.markdown("#### EOD warehouse coverage (derived GEX tables)")
    df, summ = eod_coverage()
    c1, c2, c3 = st.columns(3)
    c1.metric("Symbols", summ["n_symbols"])
    c2.metric("Total day-rows", f"{summ['total_day_rows']:,}")
    c3.metric("Latest day", summ["latest_day"])
    with st.expander("Per-symbol coverage"):
        st.dataframe(df, use_container_width=True, hide_index=True)

    st.divider()

    # --- Status JSONs ---
    st.markdown("#### Pipeline status")
    cols = st.columns(4)
    for col, name in zip(cols, ("forward", "tiingo", "gex", "eod_report")):
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

    # --- Windows scheduled tasks ---
    st.markdown("#### Scheduled tasks (Windows)")
    states = scheduled_task_states()
    if not states:
        st.caption("Task states unavailable on this host.")
    else:
        cols = st.columns(3)
        for i, (label, state) in enumerate(states.items()):
            with cols[i % 3]:
                tier = _status_tier(state)
                st.markdown(
                    f"{_TIER_DOT[tier]} **{label}** — {_color_text(state, tier)}",
                    unsafe_allow_html=True)


# ================================ 2. GAMMA ====================================
@st.cache_data(ttl=120)
def gex_latest(symbol: str) -> dict | None:
    f = DERIVED / f"{symbol}_gex_daily.parquet"
    if not f.exists():
        return None
    try:
        df = pd.read_parquet(f)
        last = df.iloc[-1]
        return {col: last[col] for col in df.columns}
    except Exception:
        return None


@st.cache_data(ttl=120)
def gex_history(symbol: str, n: int = 250) -> pd.DataFrame | None:
    f = DERIVED / f"{symbol}_gex_daily.parquet"
    if not f.exists():
        return None
    try:
        df = pd.read_parquet(f).tail(n).copy()
        df["date"] = pd.to_datetime(df["date"].astype(str), format="%Y%m%d")
        return df
    except Exception:
        return None


def render_gamma() -> None:
    st.subheader("Dealer gamma (GEX)")

    snap_syms = ["SPX", "SPXW", "SPY"]
    cols = st.columns(len(snap_syms))
    for col, sym in zip(cols, snap_syms):
        snap = gex_latest(sym)
        with col:
            st.markdown(f"### {sym}")
            if not snap:
                st.caption("no data")
                continue
            state = str(snap.get("gamma_state", "—"))
            # Shared with dailyreport/eod_report.py — see desk_health.GAMMA_STATE_TIER.
            # Negative gamma is a market-condition/awareness signal, not a pipeline
            # failure: maps to "warn" (amber), not "bad" (red).
            dh_tier = desk_health.GAMMA_STATE_TIER.get(state, "info")
            g_tier = {"ok": "good", "warn": "warn", "info": "unknown"}.get(dh_tier, "unknown")
            st.markdown(
                f"{_TIER_DOT[g_tier]} **{_color_text(state + ' gamma', g_tier)}**",
                unsafe_allow_html=True)
            net = snap.get("net_gex", 0) or 0
            net_tier = "good" if net > 0 else ("bad" if net < 0 else "unknown")
            st.metric("Spot", f"{snap.get('spot', float('nan')):,.2f}", border=True)
            st.metric("Net GEX", f"{_fmt_big(net)}",
                      delta=("positive" if net > 0 else "negative" if net < 0 else None),
                      delta_color=("normal" if net > 0 else "inverse" if net < 0 else "off"),
                      border=True)
            st.metric("Gamma flip", f"{snap.get('gamma_flip', float('nan')):,.2f}", border=True)
            st.metric("Dist to flip", f"{snap.get('dist_to_flip_pct', float('nan')):.2f}%", border=True)
            st.metric("Expected move", f"{snap.get('expected_move_pct', float('nan')):.3f}%", border=True)
            st.caption(f"as of {snap.get('date','—')}")

    st.divider()

    # --- L1: GEX zero-line / flip chart -------------------------------------
    # The headline viz. Replaces the bare line chart with a proper plotly chart:
    #   * net GEX drawn as a signed area with a ZERO LINE marked (green above /
    #     red below) — the "are we long or short gamma" read at a glance;
    #   * spot vs gamma_flip on a secondary y-axis, so you see how far price is
    #     from the flip level (positive- vs negative-gamma regime boundary).
    # Pure frontend on the existing *_gex_daily.parquet (net_gex, spot,
    # gamma_flip already present) — no new data, no writes.
    st.markdown("#### GEX zero-line / flip chart")
    all_syms = sorted(p.name.replace("_gex_daily.parquet", "")
                      for p in DERIVED.glob("*_gex_daily.parquet"))
    default_idx = all_syms.index("SPX") if "SPX" in all_syms else 0
    c1, c2 = st.columns([1, 1])
    sym = c1.selectbox("Symbol", all_syms, index=default_idx)
    lookback_label = c2.selectbox("Lookback", ["30", "90", "250", "All"], index=2)
    n = 100_000 if lookback_label == "All" else int(lookback_label)

    hist = gex_history(sym, n=n)
    if hist is None or hist.empty:
        st.caption("no history")
    else:
        have_flip = "gamma_flip" in hist.columns and hist["gamma_flip"].notna().any()
        have_spot = "spot" in hist.columns and hist["spot"].notna().any()

        # --- Panel 1: net GEX with a zero line (sign = gamma regime) ---
        gb = hist["net_gex"] / 1e9
        pos = gb.where(gb >= 0)
        neg = gb.where(gb < 0)
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=hist["date"], y=pos, name="net GEX ≥ 0 (long gamma)",
            fill="tozeroy", mode="lines", line=dict(color=_TIER_COLOR["good"], width=1),
            connectgaps=False))
        fig.add_trace(go.Scatter(
            x=hist["date"], y=neg, name="net GEX < 0 (short gamma)",
            fill="tozeroy", mode="lines", line=dict(color=_TIER_COLOR["bad"], width=1),
            connectgaps=False))
        fig.add_hline(y=0, line_width=1.4, line_color="#c9ccd1")
        last_g = gb.iloc[-1]
        fig.update_layout(
            height=300, margin=dict(l=10, r=10, t=28, b=10),
            template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            title=dict(text=f"{sym} net GEX ($B) — last {len(hist)} sessions "
                            f"(latest {_fmt_big(hist['net_gex'].iloc[-1])})", font=dict(size=13)),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, font=dict(size=10)),
            yaxis=dict(title="net GEX ($B)"))
        st.plotly_chart(fig, use_container_width=True)
        regime = ("positive gamma (mean-reverting)" if last_g > 0 else
                  "negative gamma (trend-amplifying)" if last_g < 0 else "flat")
        st.caption(f"Latest net GEX {_fmt_big(hist['net_gex'].iloc[-1])} → **{regime}**. "
                   "Above the zero line = dealers long gamma (dampen moves); "
                   "below = short gamma (amplify moves).")

        # --- Panel 2: spot vs the gamma-flip level ---
        if have_spot and have_flip:
            f2 = go.Figure()
            f2.add_trace(go.Scatter(
                x=hist["date"], y=hist["spot"], name="spot",
                mode="lines", line=dict(color="#5b9dff", width=1.4)))
            f2.add_trace(go.Scatter(
                x=hist["date"], y=hist["gamma_flip"], name="gamma flip",
                mode="lines", line=dict(color="#f5c451", width=1.2, dash="dot")))
            sp, fl = hist["spot"].iloc[-1], hist["gamma_flip"].iloc[-1]
            above = sp >= fl
            f2.update_layout(
                height=260, margin=dict(l=10, r=10, t=28, b=10),
                template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                title=dict(text=f"{sym} spot vs gamma-flip level", font=dict(size=13)),
                legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, font=dict(size=10)),
                yaxis=dict(title="index level"))
            st.plotly_chart(f2, use_container_width=True)
            dist = hist["dist_to_flip_pct"].iloc[-1] if "dist_to_flip_pct" in hist.columns else float("nan")
            st.caption(
                f"Spot {sp:,.2f} is **{'ABOVE' if above else 'BELOW'}** the flip "
                f"{fl:,.2f}"
                + (f" ({dist:+.2f}% away)" if dist == dist else "")
                + " — spot above flip ≈ positive-gamma regime; below ≈ negative-gamma.")
        else:
            st.caption("spot / gamma_flip not available for this symbol — "
                       "showing net-GEX zero-line only.")

    # --- L2 (gamma-by-strike grid): blocked. The derived *_gex_daily.parquet
    # tables are daily AGGREGATES (net/call/put GEX + a single focal_strike) —
    # there is no per-strike GEX profile persisted, so the strike-ladder heat
    # strip cannot be built from data on disk yet. It needs a small build in
    # datacollector features to persist the strike-level profile first.
    st.caption("⚠ Gamma-by-strike grid (roadmap L2) is blocked: the derived tables "
               "are daily aggregates only — no per-strike GEX profile is persisted yet.")


# ============================== 3. BACKTESTS ==================================
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


# ============================== 4. ACCOUNTS ===================================
def _connect_readonly_short(timeout: int = 6):
    """Connect read-only with a SHORT timeout. Never launches the gateway (weekend
    safety) so a down gateway fails fast instead of trying to boot it. Returns the IB
    handle or raises."""
    from connections import ibkr
    # readonly=True -> the session physically cannot transmit. launch=False -> no boot.
    return ibkr.connect("paperbot_accounts", readonly=True, launch=False, timeout=timeout)


def render_accounts() -> None:
    st.subheader("Live paper accounts (read-only)")
    st.caption("Display only. No controls. The gateway is offline on weekends — this "
               "panel degrades gracefully and lights up Monday.")

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


# ============================ 5. S5 CONVEXITY =================================
@st.cache_data(ttl=3600, show_spinner="Computing S5 convexity ledger (EOD prototype)...")
def s5_ledger() -> dict | None:
    """Recompute the S5 EOD convexity prototype ONCE (cached 1h) and return its
    per-day ledger/NAV time series + roll-up totals. Read-only research compute —
    imports the backtester's own s5_convexity_overlay.simulate_s5 unmodified and
    touches no broker/warehouse/config. The run is <1s; recomputing live avoids
    persisting a stale curve. Returns None if the module/data isn't importable."""
    try:
        s5dir = REPO / "backtester"
        if str(s5dir) not in sys.path:
            sys.path.insert(0, str(s5dir))
        import s5_convexity_overlay as s5
        with contextlib.redirect_stdout(io.StringIO()):
            panel = s5.build_panel()
            res = s5.simulate_s5(panel)
        out = res["df"].copy()
        # SPY total-return buy&hold NAV on the same index (the honest benchmark).
        spy_nav = (1.0 + panel["r_spy"].reindex(out.index).fillna(0.0)).cumprod()
        return {
            "df": out,
            "spy_nav": spy_nav,
            "reserve_target": res.get("reserve_target"),
            "total_harvest": res.get("total_harvest"),
            "total_tail_carry": res.get("total_tail_carry"),
            "total_upside_spent": res.get("total_upside_spent"),
            "total_upside_payoff": res.get("total_upside_payoff"),
            "upside_fund_count": res.get("upside_fund_count"),
        }
    except Exception as exc:  # pragma: no cover - defensive display path
        return {"error": f"{type(exc).__name__}: {exc}"}


@st.cache_data(ttl=3600)
def s5_frontier() -> pd.DataFrame | None:
    """The pre-computed head-to-head summary (ENDOGENOUS vs FIXED vs S4 vs SPY)
    from the S5 ledger-experiment CSV. Display-only read."""
    if not S5_LEDGER_CSV.exists():
        return None
    try:
        return pd.read_csv(S5_LEDGER_CSV)
    except Exception:
        return None


def _s5_metric_block(r: pd.Series, spy_nav: pd.Series) -> dict:
    """Headline CAGR/maxDD/Calmar off the S5 fund-return series (self-contained so
    the panel doesn't depend on the backtester's metrics wiring)."""
    import numpy as np
    rr = r.dropna()
    nav = (1.0 + rr.fillna(0.0)).cumprod()
    yrs = len(rr) / 252.0
    cagr = float(nav.iloc[-1] ** (1.0 / yrs) - 1.0) if yrs > 0 and len(nav) else float("nan")
    dd = float((nav / nav.cummax() - 1.0).min()) if len(nav) else float("nan")
    calmar = float(cagr / abs(dd)) if dd < 0 else float("nan")
    vol = float(rr.std(ddof=0) * np.sqrt(252.0)) if len(rr) else float("nan")
    return {"cagr": cagr, "maxdd": dd, "calmar": calmar, "vol": vol}


def render_s5() -> None:
    st.subheader("S5 — financed-convexity overlay (EOD ledger)")
    st.caption("Read-only research view. Recomputes the EOD convexity prototype live "
               "(cached 1h) from the backtester's own simulate_s5 — no broker, no "
               "writes. Numbers are a STRUCTURAL prototype on assumed harvest income "
               "and flat-skew BSM pricing (optimistic in absolutes; the honest reads "
               "are relative). Nothing here is adopted or wired into any strategy.")

    if st.button("↻ Recompute S5 ledger (cached 1h)"):
        s5_ledger.clear()

    led = s5_ledger()
    if led is None:
        st.warning("S5 prototype module not importable on this host.")
        return
    if "error" in led:
        st.error(f"Could not compute S5 ledger: {led['error']}")
        return

    out = led["df"]
    spy_nav = led["spy_nav"]
    m = _s5_metric_block(out["r_fund"], spy_nav)

    # --- Headline metric row ---
    c = st.columns(5)
    c[0].metric("CAGR", f"{m['cagr']*100:.2f}%", border=True)
    c[1].metric("Max drawdown", f"{m['maxdd']*100:.1f}%", border=True)
    c[2].metric("Calmar", f"{m['calmar']:.2f}", border=True)
    c[3].metric("Ann vol", f"{m['vol']*100:.1f}%", border=True)
    c[4].metric("Cum. harvest (assumed)",
                f"{(led.get('total_harvest') or 0)*100:.0f}%", border=True)
    st.caption(f"EOD window {out.index.min().date()} → {out.index.max().date()} "
               f"({len(out):,} trading days). CAGR is optimistic (flat-skew BSM + "
               "assumed harvest); read it against SPY below, not in isolation.")

    st.divider()

    # --- Equity curve: S5 vs SPY buy&hold (TR) ---
    st.markdown("#### Equity curve — S5 vs SPY buy & hold (TR)")
    s5_nav = (1.0 + out["r_fund"].fillna(0.0)).cumprod()
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=out.index, y=s5_nav, name="S5 fund",
                             mode="lines", line=dict(color=_TIER_COLOR["good"], width=1.4)))
    fig.add_trace(go.Scatter(x=spy_nav.index, y=spy_nav, name="SPY buy&hold (TR)",
                             mode="lines", line=dict(color="#5b9dff", width=1.1)))
    fig.update_layout(
        height=320, margin=dict(l=10, r=10, t=24, b=10),
        template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, font=dict(size=10)),
        yaxis=dict(title="growth of $1", type="log"))
    st.plotly_chart(fig, use_container_width=True)
    st.caption("Log-scale growth of $1. The S5 story is a smoother path (shallower "
               "crash drawdowns via the always-on tail), not beating SPY on raw return.")

    st.divider()

    # --- Convexity ledger: core vs tail + the self-funding ledger/reserve ---
    st.markdown("#### Convexity ledger — core vs tail, and the self-funding buffer")
    lc, rc = st.columns(2)
    with lc:
        # Net book delta = the passive de-risk engine (1.0 = fully invested core,
        # ~0 = fully hedged at a crash bottom). This is the "convexity" dial.
        fdel = go.Figure()
        fdel.add_trace(go.Scatter(x=out.index, y=out["net_delta"], name="net delta",
                                  mode="lines", line=dict(color="#f5c451", width=1)))
        fdel.add_hline(y=1.0, line_width=1, line_color="#5b9dff", line_dash="dot")
        fdel.add_hline(y=0.0, line_width=1, line_color="#c9ccd1")
        fdel.update_layout(
            height=240, margin=dict(l=10, r=10, t=26, b=10), template="plotly_dark",
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            title=dict(text="Net book delta (1.0 = full core · ~0 = hedged at bottom)",
                       font=dict(size=12)),
            showlegend=False, yaxis=dict(title="net delta"))
        st.plotly_chart(fdel, use_container_width=True)
        st.caption("The passive convexity engine: net delta falls toward ~0 as the "
                   "uncapped tail goes ITM in a crash, then re-rises on recovery — "
                   "no signal, no timing.")
    with rc:
        flg = go.Figure()
        flg.add_trace(go.Scatter(x=out.index, y=out["ledger"] * 100, name="ledger",
                                 mode="lines", line=dict(color=_TIER_COLOR["good"], width=1)))
        flg.add_trace(go.Scatter(x=out.index, y=out["reserve"] * 100, name="reserve",
                                 mode="lines", line=dict(color="#5b9dff", width=1)))
        rt = led.get("reserve_target")
        if rt is not None:
            flg.add_hline(y=rt * 100, line_width=1, line_color="#c9ccd1",
                          line_dash="dot")
        flg.update_layout(
            height=240, margin=dict(l=10, r=10, t=26, b=10), template="plotly_dark",
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            title=dict(text="Self-funding ledger & reserve (% of NAV)", font=dict(size=12)),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, font=dict(size=10)),
            yaxis=dict(title="% of NAV"))
        st.plotly_chart(flg, use_container_width=True)
        st.caption("Harvested-premium ledger funds the discretionary hedge spend via "
                   "the priority waterfall; the reserve is the senior buffer (dotted "
                   "= target).")

    # --- Ledger roll-up totals ---
    tc = st.columns(4)
    tc[0].metric("Cum. tail carry paid",
                 f"{(led.get('total_tail_carry') or 0)*100:.1f}%")
    tc[1].metric("Upside fundings", f"{led.get('upside_fund_count') or 0}")
    tc[2].metric("Upside premium spent",
                 f"{(led.get('total_upside_spent') or 0)*100:.2f}%")
    tc[3].metric("Upside payoff",
                 f"{(led.get('total_upside_payoff') or 0)*100:.2f}%")

    st.divider()

    # --- Tail-sizing frontier / head-to-head (pre-computed) ---
    st.markdown("#### Head-to-head (pre-registered ledger experiment)")
    fr = s5_frontier()
    if fr is None or fr.empty:
        st.caption("Frontier summary CSV not found.")
    else:
        disp = fr.copy()
        for col, pct in (("cagr", True), ("maxdd", True), ("calmar", False),
                         ("sharpe", False), ("vol", True)):
            if col in disp.columns:
                disp[col] = disp[col].map(
                    (lambda v: f"{v*100:.2f}%") if pct else (lambda v: f"{v:.2f}"))
        st.dataframe(disp, use_container_width=True, hide_index=True)
        st.caption("ENDOGENOUS waterfall ledger vs a FIXED flat budget (2%/yr spec "
                   "seed), S4 vol-control and SPY. Verdict: endogenous wins on "
                   "twitchy-market bleed; ~tie on the full cycle at a like budget. "
                   "See s5_ledger_experiment / s5_tail_sweep reports for the full study.")

    # --- Blocked note: the offensive/harvest half ---
    st.info("⚠ **Offensive / harvest half is blocked-for-real-data.** The harvest "
            "income above is an ASSUMED knob and the tail is flat-skew-BSM priced "
            "(optimistic). The active-monetization 0DTE harvest engine needs the "
            "1-min SPXW feed (now backfilled) wired through before those numbers are "
            "real P&L. This panel shows the EOD / defensive half only.")


# ================================= LAYOUT =====================================
def main() -> None:
    st.title("📊 Trading Desk")
    st.caption(f"As of {last_refreshed()} (newest available data) · auto-refreshes "
               "as caches expire (status/JSON 60s, GEX/coverage 120s, "
               "backtests cached 1h).")
    st.caption("Read-only dashboard · Phase 1 · paper account only · nothing here "
               "places, arms, or transmits any order.")

    tabs = st.tabs(["🩺 Health", "📈 Gamma (GEX)", "🧪 Backtests",
                    "🛡 S5 Convexity", "💼 Accounts"])
    with tabs[0]:
        render_health()
    with tabs[1]:
        render_gamma()
    with tabs[2]:
        render_backtests()
    with tabs[3]:
        render_s5()
    with tabs[4]:
        render_accounts()


if __name__ == "__main__":
    main()
