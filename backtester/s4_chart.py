"""
s4_chart.py — S4 SPX Volatility-Control Fund: equity-curve visualization.

PAPER / research only. Produces a self-contained interactive HTML chart that SHOWS
the smoothing + drawdown protection of the vol-control fund vs buy-and-hold SPY.

This script does NOT redefine any strategy or accounting logic. It imports the
validated runner (backtester/s4_vol_control.py) and reuses its EXACT data-loading
(build_returns) and strictly-causal daily TR simulation (simulate), so every series
plotted here is byte-identical to what the runner reports. It only adds presentation
(rebasing to $1, drawdown, panels, crisis shading). Nothing is modified on disk except
the new output HTML.

Curves (Total Return, gross of costs), rebased to 1.0 at the common start:
  * Buy-and-hold SPY  ............ the benchmark
  * S4  10% / 1.5x  .............. the retail-standard hero line
  * S4   5% / cap-irrelevant  .... the smoothest
  * S4  15% / 2.0x  ............. the most aggressive

Two stacked panels:
  1. Growth of $1 (log-scale y) so the early years are readable.
  2. Underwater / drawdown (% below running peak) — where vol-control visibly shines.

Crisis windows (2008-09 GFC, Feb-Mar 2020 COVID, 2022) are shaded on both panels.

Run (offline; no gateway; no network):
  C:/TradingDesk-Local/venv/Scripts/python.exe backtester/s4_chart.py
"""
from __future__ import annotations

import datetime as dt
import os

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# Reuse the VALIDATED runner's data loading + causal TR simulation verbatim.
from s4_vol_control import build_returns, simulate

# Estimator windows = runner defaults (max(20d, 60d) simple). Cash leg = BIL.
FAST, SLOW = 20, 60
ESTIMATOR = "simple"
OBS_LAG = 0
CASH = "BIL"

# The four lines: (label, target_vol, leverage_cap, color).
# S4 5% cap is "cap-irrelevant" (5% target almost never wants >1x), so we pin 1.5x.
SERIES = [
    ("S4  5% / 1.5x  (smoothest)",      0.05, 1.50, "#2ca02c"),
    ("S4 10% / 1.5x  (retail hero)",    0.10, 1.50, "#1f77b4"),
    ("S4 15% / 2.0x  (aggressive)",     0.15, 2.00, "#d62728"),
]
SPY_COLOR = "#7f7f7f"

# Crisis windows to shade (start, end, label).
CRISES = [
    ("2007-10-09", "2009-03-09", "GFC 2008-09"),
    ("2020-02-19", "2020-03-23", "COVID 2020"),
    ("2022-01-03", "2022-10-12", "2022 bear"),
]

OUT_NAME = "s4_equity_curves_20260628.html"


def nav_from_returns(r: pd.Series) -> pd.Series:
    """Growth-of-$1 NAV from a daily-return series (rebased to 1.0 at start)."""
    return (1.0 + r.fillna(0.0)).cumprod()


def underwater(nav: pd.Series) -> pd.Series:
    """Percent below running peak (<= 0)."""
    return nav / nav.cummax() - 1.0


def build_figure():
    # One simulate() call per series gives us identical dates/returns to the runner.
    # All four share the SAME common window: simulate() begins at the first warm date
    # over the full BIL+SPY overlap (no --start/--end), so the curves are aligned.
    rets, spx_price = build_returns("SPY", CASH)

    # Benchmark: buy-and-hold SPY (TR) over the exact sim window. We take the SPY
    # return series straight from a sim dict so the dates match the fund curves
    # day-for-day (same warm-up trim).
    ref_sim = simulate(
        rets, spx_price, 0.10, 1.50, FAST, SLOW, ESTIMATOR, OBS_LAG, None, None
    )
    dates = ref_sim["dates"]
    spy_nav = nav_from_returns(ref_sim["r_spx"])

    curves = []  # (label, color, nav, dd, maxdd)
    spy_dd = underwater(spy_nav)
    curves.append(("SPY buy & hold (TR)", SPY_COLOR, spy_nav, spy_dd, spy_dd.min()))

    for label, tv, cap, color in SERIES:
        sim = simulate(
            rets, spx_price, tv, cap, FAST, SLOW, ESTIMATOR, OBS_LAG, None, None
        )
        nav = nav_from_returns(sim["r_tr"])
        dd = underwater(nav)
        curves.append((label, color, nav, dd, dd.min()))

    win = (dates.min().strftime("%Y-%m-%d"), dates.max().strftime("%Y-%m-%d"))

    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.06,
        row_heights=[0.62, 0.38],
        subplot_titles=(
            "Growth of $1 — Total Return, log scale",
            "Underwater — % below running peak",
        ),
    )

    # --- Panel 1: growth of $1 (log y) ---
    for label, color, nav, dd, mdd in curves:
        is_spy = label.startswith("SPY")
        fig.add_trace(
            go.Scatter(
                x=dates, y=nav.values, name=label,
                line=dict(color=color, width=2.4 if not is_spy else 1.8,
                          dash="dot" if is_spy else "solid"),
                legendgroup=label, hovertemplate="%{x|%Y-%m-%d}<br>$%{y:.2f}<extra>" + label + "</extra>",
            ),
            row=1, col=1,
        )

    # --- Panel 2: underwater ---
    for label, color, nav, dd, mdd in curves:
        is_spy = label.startswith("SPY")
        fig.add_trace(
            go.Scatter(
                x=dates, y=(dd * 100).values, name=label,
                line=dict(color=color, width=2.0 if not is_spy else 1.6,
                          dash="dot" if is_spy else "solid"),
                legendgroup=label, showlegend=False,
                fill="tozeroy", fillcolor="rgba(0,0,0,0)",
                hovertemplate="%{x|%Y-%m-%d}<br>%{y:.1f}%<extra>" + label + "</extra>",
            ),
            row=2, col=1,
        )

    # --- Crisis shading on BOTH panels ---
    for cstart, cend, clabel in CRISES:
        x0 = pd.Timestamp(cstart)
        x1 = pd.Timestamp(cend)
        # clamp to window
        if x1 < dates.min() or x0 > dates.max():
            continue
        x0 = max(x0, dates.min())
        x1 = min(x1, dates.max())
        for rr in (1, 2):
            fig.add_vrect(
                x0=x0, x1=x1, fillcolor="rgba(120,120,120,0.12)",
                line_width=0, layer="below", row=rr, col=1,
            )
        # label only on the top panel
        fig.add_annotation(
            x=x0 + (x1 - x0) / 2, y=1.0, yref="y domain",
            text=clabel, showarrow=False, font=dict(size=10, color="#aaaaaa"),
            yanchor="bottom", row=1, col=1,
        )

    # --- Axes / layout ---
    fig.update_yaxes(type="log", row=1, col=1, title_text="Growth of $1 (log)",
                     gridcolor="rgba(128,128,128,0.18)")
    fig.update_yaxes(row=2, col=1, title_text="Drawdown (%)",
                     gridcolor="rgba(128,128,128,0.18)", ticksuffix="%")
    fig.update_xaxes(row=2, col=1, title_text="Date",
                     gridcolor="rgba(128,128,128,0.10)")
    fig.update_xaxes(row=1, col=1, gridcolor="rgba(128,128,128,0.10)")

    # Max-DD callouts in the subtitle text.
    dd_bits = " | ".join(
        f"{lbl.split('(')[0].strip()}: {mdd*100:.1f}%"
        for (lbl, _c, _n, _d, mdd) in curves
    )

    fig.update_layout(
        template="plotly_dark",
        title=dict(
            text=("<b>S4 — SPX Volatility-Control Fund</b> vs buy-and-hold SPY"
                  f"<br><span style='font-size:12px;color:#bbbbbb'>"
                  f"Total Return, gross of costs (no fees/slippage/borrow spread) | "
                  f"daily rebalance | cash leg = {CASH} | est = max({FAST}d,{SLOW}d) | "
                  f"window {win[0]} → {win[1]} | PAPER/research only</span>"
                  f"<br><span style='font-size:11px;color:#888888'>Max drawdown — {dd_bits}</span>"),
            x=0.01, xanchor="left",
        ),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1.0,
                    bgcolor="rgba(0,0,0,0)"),
        hovermode="x unified",
        margin=dict(l=70, r=30, t=130, b=50),
        height=860,
    )
    return fig, curves, win


def main():
    outdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, OUT_NAME)

    fig, curves, win = build_figure()
    fig.write_html(path, include_plotlyjs="cdn", full_html=True,
                   config={"displaylogo": False})

    size = os.path.getsize(path)
    print(f"window  : {win[0]} -> {win[1]}")
    print("max drawdowns (TR):")
    for lbl, _c, _n, _d, mdd in curves:
        print(f"  {lbl:<32} {mdd*100:7.1f}%   final $1 -> ${_n.iloc[-1]:.2f}")
    print(f"\nwrote   : {path}")
    print(f"size    : {size:,} bytes")
    if size < 50_000:
        print("WARNING: file smaller than expected — check the render.")


if __name__ == "__main__":
    main()
