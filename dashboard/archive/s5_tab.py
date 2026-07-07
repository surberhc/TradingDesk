"""
s5_tab.py — ARCHIVED dashboard tab: S5 financed-convexity overlay (EOD ledger).

Archived 2026-07-07 as part of trimming dashboard/app.py to an S0-only focus
(Andrew's direction, post-review). S5 is a non-production research strategy
(spec-stage, not live-paper-tested) -- see project memory
s5-financed-convexity-overlay.md. Kept here, not deleted, for when S5 (or a
successor) is ready for its own dashboard presence.

Reversible-archive pattern: same as dailyreport/archive/rrg (commit d6c396f).

To reinstate:
  1. Restore the sys.path bootstrap block below (reaches backtester/ for
     s5_convexity_overlay.py -- app.py's own sys.path setup already includes
     "backtester", but this file adds it defensively in case it's used
     standalone).
  2. Copy render_s5()/s5_ledger()/s5_frontier()/_s5_metric_block() back into
     app.py (or import them from here), add the tab back to the st.tabs(...)
     list in main(), and restore the "S5 Convexity" entry in the module
     docstring.
  3. Restore the S5_LEDGER_CSV / S5_TAIL_SWEEP_MD path constants in app.py
     (BACKTEST_OUTPUT / "s5_ledger_experiment_20260630.csv" and
     BACKTEST_OUTPUT / "s5_tail_sweep_20260628.md").

Dependencies this file assumes are available from the caller's namespace when
reinstated: `st` (streamlit), `pd` (pandas), `go` (plotly.graph_objects),
`io`, `contextlib`, `sys`, `REPO` (repo root Path), `_TIER_DOT`, `_TIER_COLOR`,
`_color_text`, `S5_LEDGER_CSV` (Path to the pre-computed frontier CSV).
"""
from __future__ import annotations

import io
import sys
import contextlib
from pathlib import Path

import pandas as pd
import streamlit as st
import plotly.graph_objects as go


# --- sys.path bootstrap specific to reaching s5_convexity_overlay.py -----------
# (app.py's own sys.path setup already covers "backtester"; this is here so the
# archived module is self-sufficient if imported/run standalone later.)
def _ensure_backtester_on_path(repo: Path) -> None:
    s5dir = repo / "backtester"
    if str(s5dir) not in sys.path:
        sys.path.insert(0, str(s5dir))


# ============================ S5 CONVEXITY =================================
@st.cache_data(ttl=3600, show_spinner="Computing S5 convexity ledger (EOD prototype)...")
def s5_ledger(REPO) -> dict | None:
    """Recompute the S5 EOD convexity prototype ONCE (cached 1h) and return its
    per-day ledger/NAV time series + roll-up totals. Read-only research compute —
    imports the backtester's own s5_convexity_overlay.simulate_s5 unmodified and
    touches no broker/warehouse/config. The run is <1s; recomputing live avoids
    persisting a stale curve. Returns None if the module/data isn't importable."""
    try:
        _ensure_backtester_on_path(REPO)
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
def s5_frontier(S5_LEDGER_CSV: Path) -> pd.DataFrame | None:
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


def render_s5(REPO, S5_LEDGER_CSV, _TIER_DOT, _TIER_COLOR, _color_text) -> None:
    st.subheader("S5 — financed-convexity overlay (EOD ledger)")
    st.caption("Read-only research view. Recomputes the EOD convexity prototype live "
               "(cached 1h) from the backtester's own simulate_s5 — no broker, no "
               "writes. Numbers are a STRUCTURAL prototype on assumed harvest income "
               "and flat-skew BSM pricing (optimistic in absolutes; the honest reads "
               "are relative). Nothing here is adopted or wired into any strategy.")

    if st.button("↻ Recompute S5 ledger (cached 1h)"):
        s5_ledger.clear()

    led = s5_ledger(REPO)
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
    fr = s5_frontier(S5_LEDGER_CSV)
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
