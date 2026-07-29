"""page_s0.py — the rebuilt desk's Strategy 0 page. READ-ONLY.

Strategy 0 (Adaptive All-Weather Core) is the ONE strategy that is actually
live-paper-tested. This page PORTS the correct, read-only implementations from the
old dashboard (dashboard/app.py) — the paper-account read + drift vs target +
rebalance PLAN preview, the validated backtest metrics for the 3 versions, and the
live-paper NAV vs backtest-model performance curve — and re-dresses them in the new
theme with full plain-English labels (the owner is a non-coder).

WHAT THIS NEVER DOES (the read-only contract, identical to app.py):
  * The only broker path is ibkr_paper.connect(readonly=True, launch=False) with a
    SHORT timeout — a session that physically cannot transmit, that never boots the
    gateway. It only reads (managedAccounts / accountSummary / positions / reconcile)
    and calls rebalance_run.build_preview, which is a PURE build-only planner: no order
    objects are created, nothing is armed, nothing is transmitted. Every connection is
    disconnected in a finally block.
  * No order_router.place/arm, no ib.placeOrder, no replaceFA, no file writes.
  * The backtest is the VALIDATED run_backtest, reused as-is (rule #1: the paperbot
    never re-implements strategy or performance math — one shared, validated code
    path, never curve-fit, never drifted).

IMPORT DISCIPLINE (critical): module-top imports are CHEAP only (stdlib, pandas,
streamlit, theme, and nav_history — which is pure: it only reads a local CSV via its
pure `config` data module). Every heavy/broker import — accounts, config,
strategy_target, rebalance_run, reconcile, connections.ibkr_paper, and the backtester
`bt`/`mt` — is LAZY (inside the function that needs it), exactly as app.py does, so
importing this module never opens a socket and never runs a backtest.
"""
from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

import theme

# --- Make the existing packages importable (reuse, don't rebuild) --------------
# Same sys.path bootstrap app.py uses. This module lives at
# dashboard/desk/page_s0.py, so the repo root is parents[2].
REPO = Path(__file__).resolve().parents[2]
for _sub in ("paperbot", "backtester", "connections", "strategies", "dailyreport",
             "livebot"):
    _p = REPO / _sub
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
# connections is a namespace package one level deeper (matches app.py).
_conn = REPO / "connections"
if str(_conn) not in sys.path:
    sys.path.insert(0, str(_conn))

# nav_history is PURE — it only reads a local CSV through the pure `config` data
# module; importing it opens no socket and runs no backtest. Safe at module top.
import nav_history  # noqa: E402

BACKTEST_OUTPUT = REPO / "backtester" / "output"
BACKTEST_VERSIONS = ("Conservative", "Balanced", "Growth")

# Plain-English gloss for each headline backtest metric (the owner is a non-coder).
METRIC_LABELS = {
    "CAGR": "Compound annual growth rate (CAGR)",
    "Max drawdown": "Maximum drawdown (worst peak-to-trough drop)",
    "Calmar": "Calmar ratio (annual growth ÷ worst drawdown — higher is better)",
    "Sortino": "Sortino ratio (return per unit of downside risk — higher is better)",
    "Down capture vs SPY":
        "Down-capture vs the S&P 500 (how much of the market's losses it takes)",
}
_HEADLINE_METRICS = list(METRIC_LABELS.keys())
_PERCENT_METRICS = {"CAGR", "Max drawdown", "Down capture vs SPY"}
_RISK_METRICS = {"Max drawdown", "Down capture vs SPY"}


# =========================================================================== #
# Home P&L tile summary — SLOW / monthly-style (S0 barely moves in a month).  #
# =========================================================================== #
def s0_pnl_summary() -> dict:
    """A slow, monthly-style P&L summary of the live paper Strategy 0 accounts for
    the home P&L tile — S0 is an end-of-day strategy, so value moves gradually.

    Sums the live paper net-liquidation value across all accounts per tracked date
    (nav_history.csv, written by the read-only account monitor) and returns the
    month-to-date percent change (or since-tracking-start when only one month exists).

    Returns {"change_pct": float|None, "since": str|None, "as_of": str|None,
    "note": str}. Degrades to an honest "not enough history yet" note when fewer
    than two tracked dates exist (live paper tracking started 2026-07-07). No broker
    call — reads a local CSV only."""
    slow = ("Strategy 0 is a slow, end-of-day strategy — value moves gradually, "
            "so small month-to-date moves are normal.")
    try:
        hist = nav_history.load_history()
    except Exception:
        return {"change_pct": None, "since": None, "as_of": None,
                "note": "Performance history could not be read right now. " + slow}

    if hist is None or hist.empty:
        return {"change_pct": None, "since": None, "as_of": None,
                "note": ("Not enough history yet — Strategy 0 performance tracking "
                         "started 2026-07-07; check back after a few sessions. " + slow)}

    hist = hist.dropna(subset=["net_liq"])
    # Sum the live paper net-liquidation value across every account, per date.
    totals = hist.groupby("date")["net_liq"].sum().sort_index()

    if totals.shape[0] < 2:
        only = str(totals.index[0]) if totals.shape[0] else None
        return {"change_pct": None, "since": only, "as_of": only,
                "note": ("Not enough history yet — need at least two tracked days to "
                         "show a change. " + slow)}

    as_of = str(totals.index[-1])
    as_of_month = as_of[:7]  # "YYYY-MM"
    # Month-to-date: the first tracked date in the same calendar month as the latest
    # date. Fall back to since-tracking-start when the current month has only one date.
    month_dates = [str(d) for d in totals.index if str(d)[:7] == as_of_month]
    if len(month_dates) >= 2:
        since = month_dates[0]
        scope = "month to date"
    else:
        since = str(totals.index[0])
        scope = "since tracking started"

    start_val = float(totals.loc[since])
    end_val = float(totals.loc[as_of])
    change_pct = (end_val / start_val - 1.0) * 100.0 if start_val else None
    return {"change_pct": change_pct, "since": since, "as_of": as_of,
            "note": f"Change in total paper value {scope}. " + slow}


# =========================================================================== #
# Real-money gate card — the sacred review -> arm -> transmit wall, plain.     #
# =========================================================================== #
def _render_gate_card() -> None:
    st.markdown(
        theme.status_card(
            "Real-money transmission",
            "bad",  # colour only: red = the wall is deliberately closed
            "OFF — deliberately gated",
            "Strategy 0 runs on the paper account. A real order only ever transmits "
            "behind an explicit, armed, human decision — the sacred "
            "review -> arm -> transmit gate. Nothing on this page places, arms, or "
            "transmits anything.",
        ),
        unsafe_allow_html=True,
    )


# =========================================================================== #
# Backtests — the VALIDATED run_backtest, reused as-is (never re-derived).     #
# =========================================================================== #
@st.cache_data(ttl=3600, show_spinner="Computing backtest metrics (validated engine)...")
def _backtest_metrics() -> pd.DataFrame:
    """Run the VALIDATED run_backtest once per version (cached 1h) and pull the
    headline metrics via the backtester's own metrics.compute_metrics. Read-only
    compute — the exact code path the strategy/paperbot use; touches no broker.
    Ported verbatim from app.py::backtest_metrics."""
    from src import backtest as bt
    from src import metrics as mt

    rows = {}
    for v in BACKTEST_VERSIONS:
        # Silence the engine's own prints so they don't pollute the UI.
        with contextlib.redirect_stdout(io.StringIO()):
            res = bt.run_backtest(version=v, end=None)
            table = mt.compute_metrics(res["benchmark_navs"])
        col = "strategy"
        rows[v] = {m: (table.loc[m, col] if m in table.index else float("nan"))
                   for m in _HEADLINE_METRICS}
    return pd.DataFrame(rows).T  # versions as rows


@st.cache_data(ttl=3600, show_spinner="Running backtest curve for performance comparison...")
def _backtest_strategy_curve(version_str: str) -> pd.Series:
    """The validated run_backtest()'s own 'strategy' NAV series for one version —
    reused as-is (no re-derived performance math) to compare against the live paper
    NAV. Ported verbatim from app.py::_backtest_strategy_curve."""
    from src import backtest as bt
    with contextlib.redirect_stdout(io.StringIO()):
        res = bt.run_backtest(version=version_str, end=None)
    return res["nav"]


def _fmt_metric(metric: str, raw) -> str:
    if pd.isna(raw):
        return "—"
    if metric in _PERCENT_METRICS:
        return f"{raw * 100:.1f}%"
    return f"{raw:.2f}"


def _metric_tier(metric: str, raw) -> str:
    if pd.isna(raw):
        return "unknown"
    if metric in _RISK_METRICS:
        return "bad"  # drawdown / down-capture are inherently "bad" magnitudes
    return "good" if raw >= 0 else "bad"


def _render_backtests() -> None:
    st.markdown(theme.section("Backtest results — the 3 Strategy 0 versions"), unsafe_allow_html=True)
    st.caption("Computed live from the validated backtest engine (the same code the "
               "strategy itself uses — never a separate, curve-fit copy). The three "
               "versions are progressively more aggressive: Conservative, Balanced, "
               "Growth. Cached for one hour.")

    if st.button("Recompute metrics (cached 1 hour)"):
        _backtest_metrics.clear()

    try:
        df = _backtest_metrics()
    except Exception as exc:
        st.error(f"Could not compute backtest metrics: {exc}")
        df = None

    if df is not None:
        for v in BACKTEST_VERSIONS:
            if v not in df.index:
                continue
            st.markdown(f"**{v}**")
            row = df.loc[v]
            mcols = st.columns(len(_HEADLINE_METRICS))
            for mc, metric in zip(mcols, _HEADLINE_METRICS):
                raw = row[metric]
                with mc:
                    st.markdown(
                        theme.status_card(
                            METRIC_LABELS[metric],
                            _metric_tier(metric, raw),
                            _fmt_metric(metric, raw),
                        ),
                        unsafe_allow_html=True,
                    )
        st.caption("Green = favourable. Red = drawdown / down-capture, where a deeper "
                   "(more negative) number is worse.")

    st.markdown(theme.section("Full backtest reports (rich, interactive charts)"), unsafe_allow_html=True)
    st.caption("Generated by the backtester. Download to open the full interactive report.")
    from datetime import datetime as _dt
    for v in BACKTEST_VERSIONS:
        f = BACKTEST_OUTPUT / f"backtest_report_{v}.html"
        if f.exists():
            mtime = _dt.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            with open(f, "rb") as fh:
                st.download_button(
                    f"Download the {v} report  (built {mtime})", fh.read(),
                    file_name=f.name, mime="text/html", key=f"s0_dl_{v}")
        else:
            st.caption(f"{v}: report not found")


# =========================================================================== #
# Performance vs model — live paper NAV vs the validated backtest curve.       #
# =========================================================================== #
def _render_performance() -> None:
    """Live paper NAV (summed per version) vs the validated backtest's own NAV curve
    for the same version and window, both rebased to 100 at the first tracked date.
    Ported from app.py::render_performance. Reads the local nav_history.csv only — no
    broker call — and degrades to a plain message until >=2 tracked dates exist (live
    paper tracking started 2026-07-07, no backfill possible before that)."""
    st.markdown(theme.section("How the live paper account is tracking vs the model"), unsafe_allow_html=True)
    st.caption("The live paper net-liquidation value (added up across accounts of the "
               "same version) vs the validated backtest's own value for the same "
               "version and window — both rebased to 100 at the first tracked day, so "
               "the lines start together and you can watch them diverge. There is no "
               "history before the live paper test started on 2026-07-07.")

    hist = nav_history.load_history()

    if hist.empty or hist["date"].nunique() < 2:
        first_date = hist["date"].min() if not hist.empty else None
        if first_date:
            st.info(f"Performance tracking started {first_date} — check back after a "
                    "few sessions accumulate.")
        else:
            st.info("Performance tracking has not recorded any sessions yet — check "
                    "back after the account monitor's next cycle.")
        return

    start_date = hist["date"].min()
    end_date = hist["date"].max()

    import plotly.graph_objects as go

    plotted_any = False
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
            st.caption(f"{v}: the backtest curve has no data in the tracked window yet.")
            continue
        bt_rebased = bt_window / bt_window.iloc[0] * 100.0

        fig = go.Figure()
        fig.add_trace(go.Scatter(x=paper_rebased.index, y=paper_rebased.values,
                                  mode="lines", name="Live paper account"))
        fig.add_trace(go.Scatter(x=bt_rebased.index, y=bt_rebased.values,
                                  mode="lines", name="Backtest (model)"))
        fig.update_layout(
            title=f"{v} — rebased to 100 at {start_date}",
            height=280, margin=dict(l=10, r=10, t=40, b=10),
            legend=dict(orientation="h", yanchor="bottom", y=1.02),
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            font=dict(color=theme.TEXT),
        )
        st.plotly_chart(fig, use_container_width=True, key=f"s0_perf_{v}")
        plotted_any = True

    if not plotted_any:
        st.info("Not enough per-version history yet to draw the comparison.")


# =========================================================================== #
# Accounts — read-only paper read + drift vs target + rebalance PLAN preview.  #
# =========================================================================== #
def _connect_readonly_short(timeout: int = 6):
    """Connect read-only with a SHORT timeout. Never launches the gateway (weekend
    safety) so a down gateway fails fast instead of trying to boot it. Returns the IB
    handle or raises. Ported verbatim from app.py."""
    from connections import ibkr_paper
    # readonly=True -> the session physically cannot transmit. launch=False -> no boot.
    return ibkr_paper.connect("paperbot_accounts", readonly=True, launch=False,
                              timeout=timeout)


def _render_accounts() -> None:
    st.markdown(theme.section("Live paper accounts (read-only)"), unsafe_allow_html=True)
    st.caption("Display only — there are no controls anywhere on this page. The gateway "
               "is offline on weekends and outside market hours; this panel degrades "
               "gracefully with an offline notice rather than hanging.")

    go_read = st.button("Read live paper accounts (read-only)")
    if not go_read:
        st.info("Press the button for a short, read-only gateway read. If the gateway "
                "is down (weekend / feed down) you'll see an offline notice rather than "
                "a hang. Nothing is ever placed, armed, or transmitted.")
        return

    import accounts as acc_mod
    import strategy_target
    import rebalance_run

    ib = None
    try:
        with st.spinner("Connecting to the paper gateway (read-only, short timeout)..."):
            ib = _connect_readonly_short(timeout=6)
    except Exception as exc:
        st.error("Gateway offline — live account data unavailable (weekend / feed "
                 "down). It will light up during market hours.")
        st.caption(f"(connect failed fast: {type(exc).__name__})")
        return

    try:
        with st.spinner("Reading account structure (read-only)..."):
            infos = acc_mod.discover(ib)
        if not infos:
            st.warning("Gateway connected but reported no managed accounts.")
            return

        # --- Account table (columns spelled out in full) ---
        rows = []
        for i in sorted(infos, key=lambda x: (not x.is_master, x.number)):
            rows.append({
                "Account number": i.number,
                "Account type": i.kind,
                "Net liquidation value (total account value)": f"${i.net_liq:,.0f}",
                "Cash balance": f"${i.total_cash:,.0f}",
                "Number of positions held": i.n_positions,
                "Funded?": "yes" if i.funded else "no",
                "Strategy version":
                    i.version or ("(advisor / master account)" if i.is_master
                                  else "NOT ENROLLED"),
            })
        st.markdown("**Accounts**")
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        warns = acc_mod.reconcile_enrollment(infos)
        if warns:
            for w in warns:
                st.warning(w)
        else:
            st.success("Enrollment clean — every enrolled account is visible, valid, "
                       "and funded.")

        # --- Drift vs target, per enrolled + funded client ---
        st.markdown("**Drift from target — per enrolled account**")
        clients = [i for i in infos if i.enrolled and i.funded and not i.is_master]
        if not clients:
            st.caption("No enrolled + funded client accounts.")
        else:
            import reconcile as recon
            targets_cache: dict = {}
            for info in sorted(clients, key=lambda x: x.number):
                if info.version not in targets_cache:
                    with st.spinner(f"Computing the {info.version} target..."):
                        targets_cache[info.version] = strategy_target.current_target(
                            version=info.version)
                tgt = targets_cache[info.version]
                positions = {p.contract.symbol: p.position
                             for p in ib.positions(info.number) if p.position != 0}
                lines = recon.reconcile(tgt, info.net_liq, positions)
                drift_rows = [{
                    "Holding (symbol)": ln.symbol,
                    "Alignment status": ln.status,
                    "Target weight": f"{ln.target_weight * 100:.1f}%",
                    "Actual weight": f"{ln.actual_weight * 100:.1f}%",
                    "Drift from target (percentage points)":
                        f"{ln.drift_weight * 100:+.1f}%",
                } for ln in lines if ln.target_weight > 0 or ln.actual_shares != 0]
                aligned = all(ln.status == "MATCHED" for ln in lines)
                pill = theme.pill(
                    "Aligned with target" if aligned else "Drifting from target",
                    "good" if aligned else "warn")
                with st.expander(f"{info.number}  [{info.version}] — "
                                 f"{'aligned' if aligned else 'drift present'}"):
                    st.markdown(pill, unsafe_allow_html=True)
                    st.dataframe(pd.DataFrame(drift_rows),
                                 use_container_width=True, hide_index=True)

            # --- Rebalance PLAN preview (build-only; transmits nothing) ---
            st.markdown("**Rebalance plan — review only (nothing is built, armed, or sent)**")
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
            # build_preview PRINTS its report; capture it for display. PURE/build-only —
            # no order objects, nothing armed, nothing transmitted.
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


# =========================================================================== #
# Page entry point.                                                            #
# =========================================================================== #
def render_s0_full() -> None:
    """Render the full Strategy 0 page: the real-money gate card, the validated
    backtest metrics + reports, the live-paper-vs-model performance curve, and the
    read-only paper-account read with drift + rebalance plan preview."""
    st.subheader("Strategy 0 — Adaptive All-Weather Core")
    st.caption("The one strategy that is actually live-paper-tested. Everything here "
               "is read-only.")

    _render_gate_card()
    _render_backtests()
    _render_performance()
    _render_accounts()
