"""
gamma_tab.py — ARCHIVED dashboard tab: dealer gamma (GEX) awareness panel.

Archived 2026-07-07 as part of trimming dashboard/app.py to an S0-only focus
(Andrew's direction, post-review). GEX is explicitly NOT part of any strategy's
alpha per project memory (gamma-signal-awareness-not-alpha.md) -- it's a
situational-awareness instrument (fair range + fragility flag), not a trading
signal for S0 or anything else currently live. Kept here, not deleted, for
when this feature needs its own dashboard presence again (e.g. if a future
strategy consumes GEX directly).

Reversible-archive pattern: same as dailyreport/archive/rrg (commit d6c396f).

To reinstate: copy render_gamma()/gex_latest()/gex_history() back into app.py
(or import them from here), add the tab back to the st.tabs(...) list in
main(), and restore the "Gamma (GEX)" entry in the module docstring.

Dependencies this file assumes are available from the caller's namespace when
reinstated: `st` (streamlit), `pd` (pandas), `go` (plotly.graph_objects),
`desk_health` (dailyreport/desk_health.py -- GAMMA_STATE_TIER + fmt_magnitude,
UNCHANGED, still shared with the nightly EOD email -- do not duplicate it),
`DERIVED` (warehouse derived-parquet dir), `_TIER_DOT`, `_TIER_COLOR`,
`_color_text`, `_fmt_big`.
"""
from __future__ import annotations

import pandas as pd
import streamlit as st
import plotly.graph_objects as go


# ================================ GAMMA (GEX) ====================================
@st.cache_data(ttl=120)
def gex_latest(symbol: str, DERIVED) -> dict | None:
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
def gex_history(symbol: str, DERIVED, n: int = 250) -> pd.DataFrame | None:
    f = DERIVED / f"{symbol}_gex_daily.parquet"
    if not f.exists():
        return None
    try:
        df = pd.read_parquet(f).tail(n).copy()
        df["date"] = pd.to_datetime(df["date"].astype(str), format="%Y%m%d")
        return df
    except Exception:
        return None


def render_gamma(desk_health, DERIVED, _TIER_DOT, _TIER_COLOR, _color_text, _fmt_big) -> None:
    st.subheader("Dealer gamma (GEX)")

    snap_syms = ["SPX", "SPXW", "SPY"]
    cols = st.columns(len(snap_syms))
    for col, sym in zip(cols, snap_syms):
        snap = gex_latest(sym, DERIVED)
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

    hist = gex_history(sym, DERIVED, n=n)
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
