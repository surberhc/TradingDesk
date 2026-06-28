"""
app.py — Trading Desk unified dashboard (Phase 1). READ-ONLY.

A single Streamlit app, phone- and desktop-friendly, that *shows* the state of the
whole desk by reusing the existing Python directly. It NEVER places, arms, or
transmits an order, never writes to the warehouse/config, never calls replaceFA.

Four sections (tabs):
  1. Health      — SPXW 1-min collector progress, EOD warehouse coverage, the
                   status JSONs (forward/tiingo/gex/eod_report), Windows task states.
  2. Gamma (GEX) — latest SPX/SPXW/SPY dealer-gamma snapshot + a history chart.
  3. Backtests   — latest CAGR/maxDD/Calmar/Sortino/down-capture for the 3 versions
                   (computed via the validated run_backtest, cached) + links to the
                   existing plotly HTML reports.
  4. Accounts    — the 5 DU paper subs read read-only through the gateway, with a
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

# --- Make the existing packages importable (reuse, don't rebuild) --------------
REPO = Path(__file__).resolve().parent.parent
for sub in ("paperbot", "backtester", "connections", "strategies"):
    p = REPO / sub
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
# connections is a namespace package one level deeper.
_conn = REPO / "connections"
if str(_conn) not in sys.path:
    sys.path.insert(0, str(_conn))

# --- Local data locations (off-Drive, on C:) -----------------------------------
WAREHOUSE = Path(r"C:\TradingDesk-Local\warehouse")
DERIVED = WAREHOUSE / "derived"
PROGRESS_JSON = WAREHOUSE / "spxw_1m_progress.json"
STATUS_DIR = Path(r"C:\TradingDesk-Local\state\dailyreport\status")
BACKTEST_OUTPUT = REPO / "backtester" / "output"

# Windows scheduled tasks that drive the desk (name -> friendly label).
SCHEDULED_TASKS = {
    "Spxw1mCollector": "SPXW 1-min collector",
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
        padding-top: 1.2rem !important;
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
    p = Path(path_str)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


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
    prog = load_json(str(PROGRESS_JSON))
    if prog:
        d = _parse_ts(prog.get("updated"))
        if d:
            cands.append(d)
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

    prog = load_json(str(PROGRESS_JSON)) or {}
    done = prog.get("days_done", 0)
    total = prog.get("days_total", 0) or 1
    pct = prog.get("pct", round(100 * done / total, 2))

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
        "collector_pct": pct, "collector_eta": prog.get("eta", "—"),
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
        st.metric("Collector", f"{s['collector_pct']:.1f}%", border=True)
        st.caption(str(s["collector_eta"]))
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

    # --- SPXW 1-min collector progress ---
    prog = load_json(str(PROGRESS_JSON))
    st.markdown("#### SPXW 1-minute collector")
    if not prog:
        st.warning("Collector progress file not found.")
    else:
        done = prog.get("days_done", 0)
        total = prog.get("days_total", 0) or 1
        pct = prog.get("pct", round(100 * done / total, 2))
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Days done", f"{done} / {total}")
        c2.metric("Percent", f"{pct:.1f}%")
        c3.metric("On disk", f"{prog.get('gb_on_disk_so_far', 0):.2f} GB")
        c4.metric("Errors", prog.get("errors_count", 0))
        st.progress(min(max(pct / 100.0, 0.0), 1.0))
        st.caption(
            f"Updated {_fmt_dt(prog.get('updated'))} · current day "
            f"{prog.get('current_day','—')} · ETA: {prog.get('eta','—')}")
        if prog.get("errors_count"):
            st.caption(f"Last error: {prog.get('last_error','')}")

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
            sl = state.lower()
            g_tier = ("good" if sl.startswith("pos") else
                      "bad" if sl.startswith("neg") else
                      "warn" if sl.startswith("neu") else "unknown")
            st.markdown(
                f"{_TIER_DOT[g_tier]} **{_color_text(state + ' gamma', g_tier)}**",
                unsafe_allow_html=True)
            net = snap.get("net_gex", 0) or 0
            net_tier = "good" if net > 0 else ("bad" if net < 0 else "unknown")
            st.metric("Spot", f"{snap.get('spot', float('nan')):,.2f}", border=True)
            st.metric("Net GEX", f"{net/1e9:,.2f} B",
                      delta=("positive" if net > 0 else "negative" if net < 0 else None),
                      delta_color=("normal" if net > 0 else "inverse" if net < 0 else "off"),
                      border=True)
            st.metric("Gamma flip", f"{snap.get('gamma_flip', float('nan')):,.2f}", border=True)
            st.metric("Dist to flip", f"{snap.get('dist_to_flip_pct', float('nan')):.2f}%", border=True)
            st.metric("Expected move", f"{snap.get('expected_move_pct', float('nan')):.3f}%", border=True)
            st.caption(f"as of {snap.get('date','—')}")

    st.divider()

    # --- History chart ---
    all_syms = sorted(p.name.replace("_gex_daily.parquet", "")
                      for p in DERIVED.glob("*_gex_daily.parquet"))
    default_idx = all_syms.index("SPX") if "SPX" in all_syms else 0
    c1, c2 = st.columns([1, 1])
    sym = c1.selectbox("Symbol", all_syms, index=default_idx)
    field = c2.selectbox("Series", ["net_gex", "spot", "dist_to_flip_pct",
                                    "expected_move_pct"], index=0)
    hist = gex_history(sym)
    if hist is None or hist.empty:
        st.caption("no history")
    else:
        chart_df = hist.set_index("date")[[field]]
        if field == "net_gex":
            chart_df = chart_df / 1e9
            chart_df.columns = ["net_gex (B)"]
        st.line_chart(chart_df, use_container_width=True)
        st.caption(f"{sym}: last {len(hist)} sessions")


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


# ================================= LAYOUT =====================================
def main() -> None:
    st.title("📊 Trading Desk")
    st.caption(f"As of {last_refreshed()} (newest available data) · auto-refreshes "
               "as caches expire (status/JSON 60s, GEX/coverage 120s, "
               "backtests cached 1h).")
    st.caption("Read-only dashboard · Phase 1 · paper account only · nothing here "
               "places, arms, or transmits any order.")

    tabs = st.tabs(["🩺 Health", "📈 Gamma (GEX)", "🧪 Backtests", "💼 Accounts"])
    with tabs[0]:
        render_health()
    with tabs[1]:
        render_gamma()
    with tabs[2]:
        render_backtests()
    with tabs[3]:
        render_accounts()


if __name__ == "__main__":
    main()
