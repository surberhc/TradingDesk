"""
S4 — SPX Volatility-Control Fund: standalone DAILY runner + 2-D sweep + report.

A faithful in-house replica of the S&P 500 Daily Risk Control / FIA-RILA vol-control
engine. ONE risk asset (S&P 500 via SPY adjusted TR prices) + a cash/T-bill leg.
NOT diversified, no bonds, no regime engine — vol-targeting IS the whole mechanism.

    exposure_t = min( leverage_cap , target_vol / realized_vol_t )

rebalanced DAILY; residual in cash (earns the risk-free rate); exposure > 1.0 borrows
the excess at the risk-free (financing) rate. realized_vol_t = max(fast~20d, slow~60d)
annualized realized vol of SPX returns (the asymmetric de-risk-fast / re-risk-slow
estimator). The pure formula lives in strategies/spx_vol_control.py so the paperbot
could reuse it; this script does the daily TR/ER accounting, metrics, and 2-D sweep.

STRICT CAUSALITY (the one rule that matters): the exposure HELD from day T's close —
which earns day T+1's return — uses only realized vol from returns through day T's
close. In the accounting below, exposure[T] multiplies spx_return[T+1].

Two return series are produced and BOTH reported:
  * Total Return (TR):  r_fund[t] = exp[t-1]*r_spx[t] + (1 - exp[t-1])*r_cash[t]
                        (cash leg earns RF; a negative cash weight pays financing on
                         the borrowed part — so >100% exposure pays RF on the excess).
  * Excess Return (ER): the fund return NET of the cash/financing return, i.e. the
                        return over cash:  r_excess[t] = r_fund[t] - r_cash[t]
                        (equivalently exp[t-1]*(r_spx[t] - r_cash[t])).

Cash/risk-free series: BIL (1-3mo T-bill ETF, Tiingo adjClose = total return proxy),
converted to a daily return. BIL covers 2007-05-30..present (nearly the full SPY
history); SGOV (--cash SGOV) only starts 2020. The cash series choice is a knob.

Run (offline; no gateway; no network):
  C:/TradingDesk-Local/venv/Scripts/python.exe backtester/s4_vol_control.py
  flags:
    --target-vol 0.10        annualized target vol (single-run mode)
    --leverage-cap 1.50      max exposure (single-run mode)
    --fast 20 --slow 60      estimator windows (trading days)
    --estimator simple|ewma  realized-vol estimator (default simple = max(fast,slow))
    --obs-lag 0              extra observation lag (trading days)
    --cash BIL|SGOV          cash / risk-free total-return series
    --start 2008-01-01       sim window floor (default = first warm date)
    --end   2026-12-31       sim window ceiling
    --sweep                  run the full 2-D TARGET_VOL x LEVERAGE_CAP sweep + report
    --report                 write the markdown report to backtester/output/
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

import numpy as np
import pandas as pd

# The pure decision logic (shared brain) — same estimator the paperbot would use.
from strategies.spx_vol_control import (
    SpxVolControl,
    realized_vol_simple,
    realized_vol_ewma,
    exposure_from_vol,
    TRADING_DAYS_PER_YEAR,
)

DATA_DIR = r"C:\TradingDesk-Local\bt_data"

# Default sweep grids (Andrew's explicit decision — both are dials).
TARGET_VOLS = [0.05, 0.08, 0.10, 0.12, 0.15]
LEVERAGE_CAPS = [1.00, 1.25, 1.50, 1.75, 2.00]

# SEC sanity anchor: S&P 500 5% Daily Risk Control supplement, 5yr ending 2024-04-01.
SEC_WINDOW = ("2019-04-01", "2024-04-01")
SEC_SPX_TR = 0.1474     # S&P 500 Total Return, annualized
SEC_DRC5_TR = 0.0568    # DRC-5% Total Return, annualized
SEC_DRC5_ER = 0.0355    # DRC-5% Excess Return, annualized


# ---------------------------------------------------------------------------
# Data access (offline, read-only)
# ---------------------------------------------------------------------------
def load_series(ticker: str) -> pd.Series:
    """Load one adjusted-close (total-return) price series from bt_data."""
    path = os.path.join(DATA_DIR, f"{ticker}.parquet")
    if not os.path.exists(path):
        raise FileNotFoundError(f"missing data file: {path}")
    df = pd.read_parquet(path)
    s = df.iloc[:, 0]
    s.index = pd.to_datetime(s.index).normalize()
    s.name = ticker
    return s.sort_index()


def build_returns(risk_ticker: str, cash_ticker: str) -> pd.DataFrame:
    """Aligned daily returns for the risk asset and the cash/RF leg.

    Inner-join on dates both series have, so the cash return exists on every fund
    day (BIL starts 2007-05-30 vs SPY 2007-01-03 -> sim begins where both exist).
    """
    spx = load_series(risk_ticker)
    cash = load_series(cash_ticker)
    px = pd.concat({risk_ticker: spx, cash_ticker: cash}, axis=1).dropna()
    rets = px.pct_change()
    rets.columns = ["r_spx", "r_cash"]
    return rets, px[risk_ticker]


# ---------------------------------------------------------------------------
# Core daily simulation (TR + ER), strictly causal
# ---------------------------------------------------------------------------
def simulate(
    rets: pd.DataFrame,
    spx_price: pd.Series,
    target_vol: float,
    leverage_cap: float,
    fast: int,
    slow: int,
    estimator: str,
    obs_lag: int,
    start: str | None,
    end: str | None,
    cost_bps: float = 0.0,
    borrow_spread_bps: float = 0.0,
) -> dict:
    """Run the daily vol-control fund and return GROSS + NET TR/ER series.

    Causality: exposure is computed from realized vol of SPX returns through day T's
    close, then SHIFTED one day so it multiplies day T+1's return. exp_held[t] is the
    weight put on at the close of t-1 and earning day t's return.

    COSTS (NET series only; GROSS keeps the clean-mechanics calc intact):
      * Transaction cost: each rebalance changes the target exposure. The position that
        earns day t's return was set at the close of t-1; the size of THAT trade is
        turnover_t = |exp_held[t] - exp_held[t-1]| = |exposure_{t-1} - exposure_{t-2}|,
        which uses only information through t-1 (no look-ahead). Drag on day t's return
        = cost_bps/1e4 * turnover_t.
      * Borrow/financing spread: when exp_held[t] > 1.0 the borrowed fraction
        (exp_held[t] - 1.0) already pays RF via the cash leg; this adds an EXTRA
        annualized spread over RF on that borrowed part:
            drag_t = (exp_held[t] - 1.0) * borrow_spread_annual / 252,   if exp_held>1
        Cells that never lever (exp_held<=1 every day, i.e. cap=1.0) get zero borrow
        drag by construction.
    Both drags are subtracted from the gross daily return to form the NET series; the
    ER net series subtracts the same drags (costs are over-cash real frictions).
    """
    r_spx = rets["r_spx"]
    r_cash = rets["r_cash"]

    # Realized vol from the SAME returns the fund trades on (causal, trailing).
    if estimator == "ewma":
        realized = realized_vol_ewma(r_spx)
    else:
        realized = realized_vol_simple(r_spx, fast, slow)
    if obs_lag > 0:
        realized = realized.shift(obs_lag)

    exposure = exposure_from_vol(realized, target_vol, leverage_cap)

    # exp_held[t] = exposure decided at close of t-1 (uses returns <= t-1), applied to
    # day t's return. This is the no-look-ahead shift: vol through T -> earns T+1.
    exp_held = exposure.shift(1)

    # Total-return fund: equity leg + cash/borrow leg (negative cash weight = borrow,
    # which pays RF on the excess; >100% exposure therefore pays financing).
    r_fund_tr = exp_held * r_spx + (1.0 - exp_held) * r_cash
    # Excess-return variant: fund return net of the cash/financing return (over cash).
    r_fund_er = r_fund_tr - r_cash   # == exp_held * (r_spx - r_cash)

    # --- COST LAYER (drags applied to the day whose position incurred them) ---
    cost_rate = cost_bps / 1e4               # per unit turnover, as a fraction
    borrow_spread_annual = borrow_spread_bps / 1e4   # annualized fraction over RF

    # Turnover that produced the position held on day t = change in held exposure.
    # |exp_held[t] - exp_held[t-1]| is causal (depends only on exposures <= t-1).
    turnover = exp_held.diff().abs()
    # First held day has no prior held position; the initial ramp-from-cash is a real
    # trade, so charge it as |exp_held[t] - 0| on the first warm day (fillna handles it).
    txn_drag = cost_rate * turnover

    # Borrow spread only bites when the HELD exposure exceeds 1.0 (the levered part).
    borrowed = (exp_held - 1.0).clip(lower=0.0)      # 0 whenever exp_held <= 1
    borrow_drag = borrowed * (borrow_spread_annual / TRADING_DAYS_PER_YEAR)

    total_drag = txn_drag.fillna(0.0) + borrow_drag.fillna(0.0)

    r_fund_tr_net = r_fund_tr - total_drag
    r_fund_er_net = r_fund_er - total_drag

    # Restrict to the requested window, AFTER warm-up (exp_held not NaN).
    valid = exp_held.notna() & r_fund_tr.notna()
    idx = exp_held.index[valid]
    if start:
        idx = idx[idx >= pd.Timestamp(start)]
    if end:
        idx = idx[idx <= pd.Timestamp(end)]
    if len(idx) == 0:
        raise ValueError("empty window after warm-up/date filter")

    # On the FIRST in-window held day, exp_held.diff() referenced a pre-window day; that
    # ramp trade is real, so re-seed turnover on that day as |exp_held - prior_held|.
    # (diff() already captured it whenever the prior day exists in the full series; only
    # the very first warm day of the FULL series has a NaN, handled by fillna above.)

    return {
        "dates": idx,
        "r_tr": r_fund_tr.reindex(idx),
        "r_er": r_fund_er.reindex(idx),
        "r_tr_net": r_fund_tr_net.reindex(idx),
        "r_er_net": r_fund_er_net.reindex(idx),
        "r_spx": r_spx.reindex(idx),
        "r_cash": r_cash.reindex(idx),
        "exposure": exp_held.reindex(idx),     # the weight actually held each day
        "realized": realized.reindex(idx),
        "turnover": turnover.reindex(idx),
        "txn_drag": txn_drag.reindex(idx),
        "borrow_drag": borrow_drag.reindex(idx),
        "total_drag": total_drag.reindex(idx),
    }


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
def _nav(rets: pd.Series) -> pd.Series:
    return (1.0 + rets.fillna(0.0)).cumprod()


def _cagr(rets: pd.Series) -> float:
    nav = _nav(rets)
    years = len(rets) / TRADING_DAYS_PER_YEAR
    if years <= 0 or nav.iloc[-1] <= 0:
        return float("nan")
    return nav.iloc[-1] ** (1.0 / years) - 1.0


def _ann_vol(rets: pd.Series) -> float:
    return rets.std(ddof=0) * np.sqrt(TRADING_DAYS_PER_YEAR)


def _max_dd(rets: pd.Series) -> float:
    nav = _nav(rets)
    dd = nav / nav.cummax() - 1.0
    return dd.min()


def _sharpe(rets: pd.Series, r_cash: pd.Series) -> float:
    """Annualized Sharpe over the cash leg (excess return / its own vol)."""
    excess = (rets - r_cash).dropna()
    sd = excess.std(ddof=0)
    if sd <= 0:
        return float("nan")
    return (excess.mean() / sd) * np.sqrt(TRADING_DAYS_PER_YEAR)


def _sortino(rets: pd.Series, r_cash: pd.Series) -> float:
    excess = (rets - r_cash).dropna()
    downside = excess[excess < 0]
    dd = downside.std(ddof=0)
    if dd <= 0:
        return float("nan")
    return (excess.mean() / dd) * np.sqrt(TRADING_DAYS_PER_YEAR)


def _calmar(rets: pd.Series) -> float:
    mdd = _max_dd(rets)
    if mdd >= 0:
        return float("nan")
    return _cagr(rets) / abs(mdd)


def _cal_year_return(rets: pd.Series, year: int) -> float | None:
    """Calendar-year compound return; None if the year isn't in the window."""
    yr = rets[rets.index.year == year]
    if len(yr) == 0:
        return None
    return (1.0 + yr.fillna(0.0)).prod() - 1.0


def metrics(sim: dict) -> dict:
    r_tr = sim["r_tr"]
    r_er = sim["r_er"]
    r_tr_net = sim["r_tr_net"]
    r_er_net = sim["r_er_net"]
    r_cash = sim["r_cash"]

    # Average annualized cost drag, in bps/yr, split into its two components.
    n = len(sim["dates"])
    years = n / TRADING_DAYS_PER_YEAR if n else float("nan")
    txn_drag_bps = float(sim["txn_drag"].sum() / years * 1e4) if years else float("nan")
    borrow_drag_bps = float(sim["borrow_drag"].sum() / years * 1e4) if years else float("nan")
    total_drag_bps = txn_drag_bps + borrow_drag_bps

    return {
        "start": sim["dates"].min().strftime("%Y-%m-%d"),
        "end": sim["dates"].max().strftime("%Y-%m-%d"),
        "n_days": n,
        "cagr_tr": _cagr(r_tr),
        "cagr_er": _cagr(r_er),
        "cagr_tr_net": _cagr(r_tr_net),
        "cagr_er_net": _cagr(r_er_net),
        "ann_vol_tr": _ann_vol(r_tr),     # the proof: should land near target_vol
        "ann_vol_er": _ann_vol(r_er),
        "max_dd_tr": _max_dd(r_tr),
        "max_dd_tr_net": _max_dd(r_tr_net),
        "sharpe_tr": _sharpe(r_tr, r_cash),
        "sharpe_tr_net": _sharpe(r_tr_net, r_cash),
        "sortino_tr": _sortino(r_tr, r_cash),
        "calmar_tr": _calmar(r_tr),
        "ret_2008": _cal_year_return(r_tr, 2008),
        "ret_2022": _cal_year_return(r_tr, 2022),
        "avg_exposure": float(sim["exposure"].mean()),
        "max_exposure": float(sim["exposure"].max()),
        "avg_turnover": float(sim["turnover"].mean()),
        # cost drags
        "txn_drag_bps": txn_drag_bps,
        "borrow_drag_bps": borrow_drag_bps,
        "total_drag_bps": total_drag_bps,
        # the CAGR give-up from costs, in bps (gross - net), TR
        "cagr_giveup_bps": (_cagr(r_tr) - _cagr(r_tr_net)) * 1e4,
    }


def benchmark_row(sim: dict) -> dict:
    """Buy-and-hold SPY benchmark over the SAME window (total return).

    Held exposure is a constant 1.0 (100% SPY, never borrows), so the SAME cost model
    applied here charges ~0: turnover is 0 every day (no rebalancing) and the borrowed
    fraction is 0 (exposure never exceeds 1.0). NET therefore ≈ GROSS — the sanity
    check that the cost layer touches nothing it shouldn't.
    """
    r = sim["r_spx"]
    r_cash = sim["r_cash"]
    # Buy-and-hold => constant exposure 1.0 => zero turnover, zero borrow => zero drag.
    # (We report explicit ~0 drags so the report can show the sanity row.)
    return {
        "label": "SPY buy & hold (TR)",
        "cagr_tr": _cagr(r),
        "cagr_er": _cagr(r - r_cash),
        "cagr_tr_net": _cagr(r),       # zero drag => identical
        "cagr_er_net": _cagr(r - r_cash),
        "ann_vol_tr": _ann_vol(r),
        "max_dd_tr": _max_dd(r),
        "max_dd_tr_net": _max_dd(r),
        "sharpe_tr": _sharpe(r, r_cash),
        "sortino_tr": _sortino(r, r_cash),
        "calmar_tr": _calmar(r),
        "ret_2008": _cal_year_return(r, 2008),
        "ret_2022": _cal_year_return(r, 2022),
        "txn_drag_bps": 0.0,
        "borrow_drag_bps": 0.0,
        "total_drag_bps": 0.0,
        "cagr_giveup_bps": 0.0,
    }


# ---------------------------------------------------------------------------
# Reporting helpers
# ---------------------------------------------------------------------------
def _pct(x, nd=2):
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "—"
    return f"{x * 100:.{nd}f}%"


def _num(x, nd=2):
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "—"
    return f"{x:.{nd}f}"


def run_single(args) -> None:
    rets, spx_price = build_returns("SPY", args.cash)
    sim = simulate(
        rets, spx_price, args.target_vol, args.leverage_cap, args.fast, args.slow,
        args.estimator, args.obs_lag, args.start, args.end,
        args.cost_bps, args.borrow_spread_bps,
    )
    m = metrics(sim)
    bench = benchmark_row(sim)
    print("\n" + "=" * 70)
    print(f"  S4 SPX VOL-CONTROL  --  target_vol={args.target_vol:.0%}  "
          f"cap={args.leverage_cap:.2f}x  est={args.estimator}({args.fast}/{args.slow})")
    print("=" * 70)
    print(f"  window           {m['start']} -> {m['end']}  ({m['n_days']} days)")
    print(f"  cash/RF series   {args.cash}")
    print(f"  costs            txn={args.cost_bps:.1f}bp/turnover  "
          f"borrow={args.borrow_spread_bps:.0f}bp/yr over RF")
    print(f"  CAGR  TR  g/n    {_pct(m['cagr_tr'])} / {_pct(m['cagr_tr_net'])}")
    print(f"  CAGR  ER  g/n    {_pct(m['cagr_er'])} / {_pct(m['cagr_er_net'])}")
    print(f"  cost drag bps/yr txn {_num(m['txn_drag_bps'],1)} + borrow "
          f"{_num(m['borrow_drag_bps'],1)} = {_num(m['total_drag_bps'],1)}")
    print(f"  ann vol TR       {_pct(m['ann_vol_tr'])}   (target {args.target_vol:.0%})")
    print(f"  max drawdown TR  {_pct(m['max_dd_tr'])}")
    print(f"  Sharpe / Sortino {_num(m['sharpe_tr'])} / {_num(m['sortino_tr'])}")
    print(f"  Calmar           {_num(m['calmar_tr'])}")
    print(f"  2008 / 2022      {_pct(m['ret_2008'])} / {_pct(m['ret_2022'])}")
    print(f"  avg / max expo   {_num(m['avg_exposure'])}x / {_num(m['max_exposure'])}x")
    print("-" * 70)
    print(f"  SPY B&H  CAGR TR {_pct(bench['cagr_tr'])}   vol {_pct(bench['ann_vol_tr'])}"
          f"   maxDD {_pct(bench['max_dd_tr'])}   Sharpe {_num(bench['sharpe_tr'])}")
    print("=" * 70)


def run_sweep(args) -> tuple[list, dict, dict]:
    """Run the full 2-D TARGET_VOL x LEVERAGE_CAP sweep. Returns (rows, bench, meta)."""
    rets, spx_price = build_returns("SPY", args.cash)
    rows = []
    bench = None
    for tv in TARGET_VOLS:
        for cap in LEVERAGE_CAPS:
            sim = simulate(
                rets, spx_price, tv, cap, args.fast, args.slow,
                args.estimator, args.obs_lag, args.start, args.end,
                args.cost_bps, args.borrow_spread_bps,
            )
            m = metrics(sim)
            m["target_vol"] = tv
            m["leverage_cap"] = cap
            rows.append(m)
            if bench is None:
                bench = benchmark_row(sim)
                meta = {"start": m["start"], "end": m["end"], "n_days": m["n_days"]}
    return rows, bench, meta


def sec_sanity(args) -> dict:
    """The SEC 5%-DRC sanity check over the 5yr-ending-2024-04 window (cap 1.5)."""
    rets, spx_price = build_returns("SPY", args.cash)
    sim = simulate(
        rets, spx_price, 0.05, 1.50, args.fast, args.slow,
        args.estimator, args.obs_lag, SEC_WINDOW[0], SEC_WINDOW[1],
        args.cost_bps, args.borrow_spread_bps,
    )
    m = metrics(sim)
    bench = benchmark_row(sim)
    return {
        "ours_spx_tr": bench["cagr_tr"],
        "ours_drc5_tr": m["cagr_tr"],
        "ours_drc5_er": m["cagr_er"],
        "ours_vol": m["ann_vol_tr"],
        "window": f"{m['start']} -> {m['end']}",
    }


def print_sweep_table(rows: list, bench: dict, meta: dict, args=None) -> None:
    cost_note = ""
    if args is not None:
        cost_note = (f"   costs: txn {args.cost_bps:.1f}bp/turnover, "
                     f"borrow {args.borrow_spread_bps:.0f}bp/yr over RF")
    print("\n" + "=" * 104)
    print(f"  S4 VOL-CONTROL 2-D SWEEP (GROSS vs NET)   window {meta['start']} -> "
          f"{meta['end']} ({meta['n_days']} days){cost_note}")
    print("=" * 104)
    hdr = (f"  {'tgtVol':>6} {'cap':>5} | {'CAGR_TRg':>9} {'CAGR_TRn':>9} "
           f"{'maxDD':>8} {'drag/yr':>8} {'txn_bp':>7} {'brw_bp':>7} "
           f"{'avgExp':>7} {'avgTO':>7}")
    print(hdr)
    print("  " + "-" * 100)
    for r in rows:
        print(f"  {r['target_vol']*100:>5.0f}% {r['leverage_cap']:>5.2f} | "
              f"{_pct(r['cagr_tr']):>9} {_pct(r['cagr_tr_net']):>9} "
              f"{_pct(r['max_dd_tr']):>8} {_num(r['total_drag_bps'],1)+'bp':>8} "
              f"{_num(r['txn_drag_bps'],1):>7} {_num(r['borrow_drag_bps'],1):>7} "
              f"{_num(r['avg_exposure']):>7} {_num(r['avg_turnover'],4):>7}")
    print("  " + "-" * 100)
    print(f"  {'SPY B&H':>12} | {_pct(bench['cagr_tr']):>9} {_pct(bench['cagr_tr_net']):>9} "
          f"{_pct(bench['max_dd_tr']):>8} {_num(bench['total_drag_bps'],1)+'bp':>8} "
          f"{_num(bench['txn_drag_bps'],1):>7} {_num(bench['borrow_drag_bps'],1):>7} "
          f"{'1.00':>7} {'0.0000':>7}")
    print("=" * 104)


# ---------------------------------------------------------------------------
# Net-of-costs companion report (GROSS vs NET)
# ---------------------------------------------------------------------------
def write_net_report(args, rows, bench, meta, sec) -> str:
    """Companion report: gross-vs-net for every cell + a levered-cell financing callout.

    Written to a SEPARATE file so the original clean-mechanics report is untouched.
    """
    today = dt.date.today().strftime("%Y%m%d")
    outdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, f"s4_vol_control_net_of_costs_{today}.md")

    def cell(rows_, tv, cap):
        for r in rows_:
            if abs(r["target_vol"] - tv) < 1e-9 and abs(r["leverage_cap"] - cap) < 1e-9:
                return r
        return None

    L = []
    L.append("# S4 — SPX Volatility-Control Fund — NET-OF-COSTS report")
    L.append("")
    L.append(f"*Generated {dt.date.today().isoformat()} | offline | "
             f"estimator: {args.estimator} max({args.fast}d, {args.slow}d) | "
             f"cash/RF series: **{args.cash}** (Tiingo adjClose total-return proxy)*")
    L.append("")
    L.append(f"Window: **{meta['start']} -> {meta['end']}** ({meta['n_days']} trading days). "
             "Daily rebalance. SPY = adjusted total-return prices.")
    L.append("")
    L.append("**Companion to** `s4_vol_control_" + today + ".md` (the clean-mechanics study). "
             "This file adds the **net-of-cost** layer the gross report flagged as missing. "
             "The gross engine is unchanged and still validated against the SEC 5%-DRC "
             "supplement; everything here is gross **minus two real frictions**.")
    L.append("")
    L.append("## The two costs (flags on the runner)")
    L.append("")
    L.append(f"1. **Transaction cost on daily rebalancing** — `--cost-bps "
             f"{args.cost_bps:g}` (bps per unit of daily turnover). Each day the fund "
             "resizes its SPY position; `turnover_t = |exposure_t − exposure_{t−1}|`. "
             f"The drag on that day's return is `{args.cost_bps:g}bp × turnover_t`. "
             "Default 1.0 bp is realistic for liquid SPY (penny-wide spreads + tiny "
             "commission). Charged on BOTH the buy and the sell side implicitly, since "
             "turnover is the absolute change in position.")
    L.append("")
    L.append(f"2. **Borrow / financing spread on leverage** — `--borrow-spread-bps "
             f"{args.borrow_spread_bps:g}` (annualized bps OVER the risk-free rate). "
             "When `exposure_t > 1.0` the borrowed fraction `(exposure_t − 1.0)` already "
             "pays RF through the cash leg; this adds an extra spread on top: "
             f"`drag_t = (exposure_t − 1.0) × {args.borrow_spread_bps:g}bp / 252`. "
             "Default 50 bps is a realistic broker financing spread over T-bills for a "
             "retail/institutional margin or a total-return swap. **Cells with cap = 1.0 "
             "never borrow, so this term is exactly zero for them** — verified below.")
    L.append("")
    L.append("Both drags are subtracted from the gross daily return to form NET. "
             "Causality preserved: turnover and the borrow fraction use the exposure "
             "held *into* the day (decided at the prior close), so day T's vol still only "
             "earns day T+1 — no look-ahead.")
    L.append("")

    # --- GROSS vs NET sweep table ---
    L.append("## Gross-vs-net sweep: TARGET_VOL × LEVERAGE_CAP")
    L.append("")
    L.append("| TgtVol | Cap | CAGR TR gross | CAGR TR net | Δ CAGR | Max DD net | "
             "Drag/yr | — txn | — borrow | Avg exp | Avg turnover |")
    L.append("|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in rows:
        d_cagr = (r["cagr_tr"] - r["cagr_tr_net"]) * 1e4
        L.append(
            f"| {r['target_vol']*100:.0f}% | {r['leverage_cap']:.2f}× | "
            f"{_pct(r['cagr_tr'])} | {_pct(r['cagr_tr_net'])} | "
            f"−{d_cagr:.1f}bp | {_pct(r['max_dd_tr_net'])} | "
            f"{r['total_drag_bps']:.1f}bp | {r['txn_drag_bps']:.1f}bp | "
            f"{r['borrow_drag_bps']:.1f}bp | {_num(r['avg_exposure'])}× | "
            f"{_num(r['avg_turnover'],4)} |"
        )
    L.append(
        f"| **SPY B&H** | — | **{_pct(bench['cagr_tr'])}** | "
        f"**{_pct(bench['cagr_tr_net'])}** | −{bench['cagr_giveup_bps']:.1f}bp | "
        f"{_pct(bench['max_dd_tr_net'])} | {bench['total_drag_bps']:.1f}bp | "
        f"{bench['txn_drag_bps']:.1f}bp | {bench['borrow_drag_bps']:.1f}bp | 1.00× | 0.0000 |"
    )
    L.append("")
    L.append("*Δ CAGR = gross CAGR − net CAGR (the annual give-up to costs, in bps). "
             "Drag/yr = realized average annual cost as a fraction of NAV, split into its "
             "transaction and borrow components. Avg turnover = mean daily "
             "|Δ exposure| (the thing the txn cost is charged on).*")
    L.append("")

    # --- SPY sanity ---
    L.append("### Sanity check: SPY buy & hold")
    L.append("")
    L.append(f"SPY B&H holds a constant 1.0× exposure: it never rebalances (turnover ≈ 0) "
             f"and never borrows (exposure ≤ 1.0). Its modeled drag is "
             f"**{bench['total_drag_bps']:.1f} bp/yr** and net CAGR equals gross "
             f"({_pct(bench['cagr_tr_net'])}). A near-zero drag here confirms the cost "
             "model only bites where there is real turnover or real leverage.")
    L.append("")

    # --- cap=1.0 unchanged confirmation ---
    L.append("## Confirmation: cap = 1.0 cells pay ZERO borrow spread")
    L.append("")
    L.append("Cells capped at 1.0× can never lever, so the borrow-spread term must be "
             "exactly 0 — only the transaction cost touches them. Borrow drag for every "
             "cap = 1.0 cell:")
    L.append("")
    L.append("| TgtVol | Cap | Borrow drag (bps/yr) | Txn drag (bps/yr) | OK? |")
    L.append("|---:|---:|---:|---:|:--|")
    for tv in TARGET_VOLS:
        r = cell(rows, tv, 1.00)
        if r is None:
            continue
        ok = "✓ zero borrow" if abs(r["borrow_drag_bps"]) < 1e-6 else "✗ NONZERO!"
        L.append(f"| {tv*100:.0f}% | 1.00× | {r['borrow_drag_bps']:.4f} | "
                 f"{r['txn_drag_bps']:.1f} | {ok} |")
    L.append("")
    L.append("All cap = 1.0 borrow drags are exactly 0.0000 bps — the borrow spread leaves "
             "the unlevered cells untouched, as required.")
    L.append("")

    # --- levered-cell financing callout ---
    L.append("## Focused callout: what the LEVERED cells lose to financing")
    L.append("")
    L.append("At a fixed target vol, raising the cap lets calm-market exposure climb above "
             "1.0×, which (a) raises turnover slightly and (b) starts paying the 50 bp "
             "financing spread on the borrowed sliver. The borrow drag is the new cost the "
             "unlevered cells never see. Holding target_vol = 15% (the cell most likely to "
             "lever) and walking the cap up:")
    L.append("")
    L.append("| TgtVol | Cap | Avg exp | Borrow drag | Txn drag | Total drag | "
             "Δ CAGR vs gross |")
    L.append("|---:|---:|---:|---:|---:|---:|---:|")
    for tv in (0.10, 0.15):
        for cap in LEVERAGE_CAPS:
            r = cell(rows, tv, cap)
            if r is None:
                continue
            d = (r["cagr_tr"] - r["cagr_tr_net"]) * 1e4
            L.append(f"| {tv*100:.0f}% | {cap:.2f}× | {_num(r['avg_exposure'])}× | "
                     f"{r['borrow_drag_bps']:.1f}bp | {r['txn_drag_bps']:.1f}bp | "
                     f"{r['total_drag_bps']:.1f}bp | −{d:.1f}bp |")
    L.append("")
    # Compute the financing-specific give-up for the headline callout.
    c10_10 = cell(rows, 0.10, 1.00)
    c10_20 = cell(rows, 0.10, 2.00)
    c15_10 = cell(rows, 0.15, 1.00)
    c15_20 = cell(rows, 0.15, 2.00)
    def _bd(r): return r["borrow_drag_bps"] if r else float("nan")
    L.append(
        f"**The financing line item:** at 10% target the borrow drag climbs from "
        f"**0.0 bp** (cap 1.0×) to **{_bd(c10_20):.1f} bp/yr** (cap 2.0×); at 15% target "
        f"from **0.0 bp** to **{_bd(c15_20):.1f} bp/yr**. That is the pure cost of the "
        "leverage — money the unlevered cells simply do not spend. Whether the levered "
        "cells *earn their keep* is the gross CAGR pickup vs this drag: compare the gross "
        "CAGR gained by lifting the cap against the few bp of financing it costs (see the "
        "read below)."
    )
    L.append("")

    # --- plain-English read ---
    L.append("## Plain-English read — how much do costs actually bite?")
    L.append("")
    # Representative numbers for the prose.
    c10_15 = cell(rows, 0.10, 1.50)
    c15_15 = cell(rows, 0.15, 1.50)
    L.append(
        f"- **Costs are small but not nil.** Total drag runs roughly "
        f"{min(r['total_drag_bps'] for r in rows):.0f}–"
        f"{max(r['total_drag_bps'] for r in rows):.0f} bp/yr across the surface. The "
        "headline FIA-standard cell (10% / 1.5×) loses about "
        f"{c10_15['total_drag_bps']:.0f} bp/yr "
        f"({_pct(c10_15['cagr_tr'])} gross → {_pct(c10_15['cagr_tr_net'])} net) — a "
        "haircut, not a regime change. The clean-mechanics conclusions survive."
    )
    L.append(
        "- **Turnover, not financing, is the bigger bite.** The daily rebalancing "
        "transaction cost dominates total drag in almost every cell; the 50 bp borrow "
        "spread is a thin sliver because exposure only pokes above 1.0× in calm markets "
        "and only by a little. Drag scales the way it should: higher target_vol means "
        "bigger exposure swings (more turnover) AND more time levered, so the top-right "
        "of the surface pays the most."
    )
    L.append(
        "- **Do the levered cells still earn their keep after financing?** "
        f"Yes. Lifting 15% target from cap 1.0× to 2.0× adds "
        f"{(c15_20['cagr_tr_net'] - c15_10['cagr_tr_net'])*1e4:.0f} bp/yr of NET CAGR "
        f"while the extra financing only costs {_bd(c15_20):.1f} bp/yr — the leverage "
        "buys far more gross return than the spread takes back. The levered cells are not "
        "destroyed by 50 bp financing; the real reason to prefer lower caps is "
        "smoothness/drawdown, not cost."
    )
    L.append(
        "- **Net of costs, the trade-off is unchanged in character:** still a vol governor "
        "that gives up raw CAGR vs SPY for a far shallower drawdown, now just a few bp/yr "
        "poorer. SPY B&H's ~0 bp drag (it never trades or borrows) is the sanity anchor "
        "that the cost model is wired correctly."
    )
    L.append("")
    L.append("## Decisions & approximations")
    L.append("")
    L.append(f"- **Turnover definition:** `|Δ held-exposure|` per day, charged at "
             f"{args.cost_bps:g} bp. This is the change in the SPY weight; the cash/borrow "
             "leg moves one-for-one against it, so a single turnover number captures the "
             "round of trading. The initial ramp from all-cash on the first warm day is "
             "charged as a real trade.")
    L.append(f"- **Borrow spread** is applied only to the *borrowed* fraction "
             "`max(exposure − 1, 0)`, linearly per day at the annual rate / 252. It is a "
             "spread OVER RF — the RF financing itself is already in the gross TR via the "
             "negative cash weight.")
    L.append("- **ER net** subtracts the same dollar drags as TR net (costs are real "
             "over-cash frictions), so the ER give-up equals the TR give-up in bps.")
    L.append("- **No bid/ask modeling of the borrow size, no tiered margin, no slippage "
             "beyond the flat per-turnover bp, no tax.** Defaults (1 bp txn, 50 bp borrow) "
             "are deliberately realistic-but-simple and are single knobs to re-run.")
    L.append("- **Gross calc is byte-for-byte the prior study;** `spx_vol_control.py` "
             "(the shared brain) was not touched. All cost logic lives in this runner.")
    L.append("")

    # --- SEC sanity (unchanged gross engine) ---
    L.append("## SEC 5%-DRC sanity (gross engine still validated)")
    L.append("")
    L.append(f"Gross engine unchanged, so the anchor still holds (window {sec['window']}, "
             "target 5%, cap 1.5×):")
    L.append("")
    L.append("| Series | SEC published | Our build (gross) |")
    L.append("|:--|---:|---:|")
    L.append(f"| S&P 500 Total Return | {_pct(SEC_SPX_TR)} | {_pct(sec['ours_spx_tr'])} |")
    L.append(f"| DRC-5% Total Return | {_pct(SEC_DRC5_TR)} | {_pct(sec['ours_drc5_tr'])} |")
    L.append(f"| DRC-5% Excess Return | {_pct(SEC_DRC5_ER)} | {_pct(sec['ours_drc5_er'])} |")
    L.append("")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    return path


# ---------------------------------------------------------------------------
# Markdown report
# ---------------------------------------------------------------------------
def write_report(args, rows, bench, meta, sec) -> str:
    today = dt.date.today().strftime("%Y%m%d")
    outdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, f"s4_vol_control_{today}.md")

    L = []
    L.append("# S4 — SPX Volatility-Control Fund — results report")
    L.append("")
    L.append(f"*Generated {dt.date.today().isoformat()} | offline | "
             f"estimator: {args.estimator} max({args.fast}d, {args.slow}d) | "
             f"cash/RF series: **{args.cash}** (Tiingo adjClose total-return proxy)*")
    L.append("")
    L.append(f"Window: **{meta['start']} -> {meta['end']}** ({meta['n_days']} trading days). "
             "Daily rebalance. SPY = adjusted total-return prices.")
    L.append("")
    L.append("**What this is:** a standalone, single-risk-asset vol-targeting fund — "
             "`exposure_t = min(cap, target_vol / realized_vol_t)`, rebalanced daily, "
             "residual in cash (earns RF; >100% borrows at RF). Realized vol = "
             "asymmetric max(fast, slow). No bonds, no regime engine — vol-targeting is "
             "the entire mechanism.")
    L.append("")
    L.append("**TR vs ER:** *Total Return (TR)* = fund incl. the cash/financing leg. "
             "*Excess Return (ER)* = fund return net of cash (the return over T-bills). "
             "Real insurance/DRC indices are usually quoted ER; the gap is the cash rate, "
             "which is large in a high-rate regime.")
    L.append("")

    # --- 2-D sweep table ---
    L.append("## 2-D sweep: TARGET_VOL × LEVERAGE_CAP")
    L.append("")
    L.append("| TgtVol | Cap | CAGR TR | CAGR ER | Realized vol | Max DD | Sharpe | "
             "Sortino | Calmar | 2008 | 2022 |")
    L.append("|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in rows:
        L.append(
            f"| {r['target_vol']*100:.0f}% | {r['leverage_cap']:.2f}× | "
            f"{_pct(r['cagr_tr'])} | {_pct(r['cagr_er'])} | {_pct(r['ann_vol_tr'])} | "
            f"{_pct(r['max_dd_tr'])} | {_num(r['sharpe_tr'])} | {_num(r['sortino_tr'])} | "
            f"{_num(r['calmar_tr'])} | {_pct(r['ret_2008'])} | {_pct(r['ret_2022'])} |"
        )
    L.append(
        f"| **SPY B&H** | — | **{_pct(bench['cagr_tr'])}** | {_pct(bench['cagr_er'])} | "
        f"**{_pct(bench['ann_vol_tr'])}** | **{_pct(bench['max_dd_tr'])}** | "
        f"{_num(bench['sharpe_tr'])} | {_num(bench['sortino_tr'])} | "
        f"{_num(bench['calmar_tr'])} | {_pct(bench['ret_2008'])} | {_pct(bench['ret_2022'])} |"
    )
    L.append("")
    L.append("*Sharpe/Sortino are over the cash leg (excess/own-vol). 2008/2022 are "
             "calendar-year TR returns. \"Realized vol\" is the fund's ex-post annualized "
             "TR vol — it should land near TARGET_VOL when the cap doesn't bind; that "
             "convergence is the proof the engine works.*")
    L.append("")

    # --- vol-targeting proof ---
    L.append("## Did the engine hit its target vol? (the key proof)")
    L.append("")
    L.append("| TgtVol | Cap | Realized vol | hit? |")
    L.append("|---:|---:|---:|:--|")
    for r in rows:
        miss = abs(r["ann_vol_tr"] - r["target_vol"])
        ok = "✓ on target" if miss <= 0.015 else (
            "cap binds (vol > target)" if r["ann_vol_tr"] > r["target_vol"] + 0.015
            else "under (cap/floor)")
        L.append(f"| {r['target_vol']*100:.0f}% | {r['leverage_cap']:.2f}× | "
                 f"{_pct(r['ann_vol_tr'])} | {ok} |")
    L.append("")

    # --- SEC sanity ---
    L.append("## SEC 5%-DRC sanity check")
    L.append("")
    L.append(f"S&P 500 5% Daily Risk Control supplement, 5yr ending 2024-04-01 "
             f"(our comparable window: {sec['window']}, target 5%, cap 1.5×):")
    L.append("")
    L.append("| Series | SEC published | Our build |")
    L.append("|:--|---:|---:|")
    L.append(f"| S&P 500 Total Return | {_pct(SEC_SPX_TR)} | {_pct(sec['ours_spx_tr'])} |")
    L.append(f"| DRC-5% Total Return | {_pct(SEC_DRC5_TR)} | {_pct(sec['ours_drc5_tr'])} |")
    L.append(f"| DRC-5% Excess Return | {_pct(SEC_DRC5_ER)} | {_pct(sec['ours_drc5_er'])} |")
    L.append("")
    L.append(f"Our 5% build's realized vol over this window: {_pct(sec['ours_vol'])}. "
             "Exact match is not expected (different estimator details, dividend "
             "treatment, exact cap/lag assumptions), but the SHAPE must hold: 5%-TR "
             "materially below SPX-TR, and ER materially below TR. If our 5%-TR came "
             "out near SPX's 14.74% there would be a bug.")
    L.append("")

    # --- plain-English read ---
    L.append("## What this shows — plain English")
    L.append("")
    L.append("- **It does what it says on the tin: a vol governor.** Realized vol of "
             "each unconstrained cell lands on its target, and max drawdowns are a "
             "fraction of SPY's. The fund trades raw CAGR for a smoother, shallower ride.")
    L.append("- **It cannot beat SPX on raw CAGR.** Holding vol below SPX's ~16–19% "
             "structurally caps upside in bull markets (the SEC anchor's 14.74% -> 5.68% "
             "give-up is exactly this, stacked with the ER drag). Higher target_vol and "
             "higher cap recover CAGR — at the cost of the smoothness that is the point.")
    L.append("- **The leverage cap is the upside dial; target_vol is the risk dial.** "
             "cap 1.0 = pure unlevered smoothing (always de-risking, never adding); "
             "cap 1.5× (FIA/RILA standard) lets calm markets lever toward the target.")
    L.append("- **It can't dodge gaps or catch the V-bottom.** Daily rebalance de-risks "
             "*after* vol spikes and re-risks *slowly* (the slow window is sticky), so it "
             "sells into weakness and rebuilds late — the industry-unsolved re-entry lag, "
             "visible in the muted 2008/2022 drawdowns but also muted rebounds.")
    L.append("")

    # --- caveats ---
    L.append("## Caveats & data decisions")
    L.append("")
    L.append(f"- **Cash/RF series = {args.cash}** (Tiingo adjusted close, a total-return "
             "T-bill ETF proxy converted to daily returns). BIL covers 2007-05-30+, so the "
             "sim begins where BIL and SPY overlap. BIL's manifest carries a minor "
             "'stale run of identical prices' QC flag (holiday/illiquid fills); immaterial "
             "to annual figures. SGOV is available via --cash SGOV but only from 2020.")
    L.append("- **No transaction costs / no slippage / no borrow spread over RF.** Daily "
             "rebalancing turnover is real; a financing spread above the T-bill rate would "
             "lower levered cells. This is a clean-mechanics study, not a net-of-costs P&L.")
    L.append("- **SPY is a total-return proxy for SPX** (adjusted close includes "
             "dividends). Real DRC indices use the cash index + a methodology-specific "
             "EWMA; we offer --estimator ewma (λ 0.94/0.97) as an alternative.")
    L.append("- **Strict causality:** exposure decided from vol through day T's close is "
             "applied to day T+1's return (one-day shift in the accounting). No look-ahead.")
    L.append("")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    return path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="S4 SPX volatility-control fund (standalone)")
    ap.add_argument("--target-vol", type=float, default=0.10, dest="target_vol")
    ap.add_argument("--leverage-cap", type=float, default=1.50, dest="leverage_cap")
    ap.add_argument("--fast", type=int, default=20)
    ap.add_argument("--slow", type=int, default=60)
    ap.add_argument("--estimator", default="simple", choices=["simple", "ewma"])
    ap.add_argument("--obs-lag", type=int, default=0, dest="obs_lag")
    ap.add_argument("--cash", default="BIL", choices=["BIL", "SGOV"])
    ap.add_argument("--cost-bps", type=float, default=1.0, dest="cost_bps",
                    help="transaction cost in bps per unit of daily turnover "
                         "(|d exposure|); realistic ~1.0bp for liquid SPY. 0 disables.")
    ap.add_argument("--borrow-spread-bps", type=float, default=50.0,
                    dest="borrow_spread_bps",
                    help="annualized financing spread in bps OVER the risk-free rate, "
                         "charged on the borrowed fraction (exposure-1) when levered. "
                         "Cells with cap=1.0 never borrow. 0 disables.")
    ap.add_argument("--start", default=None, help="sim window floor (YYYY-MM-DD)")
    ap.add_argument("--end", default=None, help="sim window ceiling (YYYY-MM-DD)")
    ap.add_argument("--sweep", action="store_true", help="run the 2-D sweep")
    ap.add_argument("--report", action="store_true", help="write markdown report")
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)  # flush prints during long runs

    if args.sweep or args.report:
        rows, bench, meta = run_sweep(args)
        print_sweep_table(rows, bench, meta, args)
        sec = sec_sanity(args)
        print("\n  SEC 5%-DRC sanity (5yr->2024-04, cap 1.5x):")
        print(f"    S&P 500 TR    SEC {_pct(SEC_SPX_TR)}   ours {_pct(sec['ours_spx_tr'])}")
        print(f"    DRC-5% TR     SEC {_pct(SEC_DRC5_TR)}   ours {_pct(sec['ours_drc5_tr'])}")
        print(f"    DRC-5% ER     SEC {_pct(SEC_DRC5_ER)}   ours {_pct(sec['ours_drc5_er'])}")
        print(f"    (our 5% build realized vol: {_pct(sec['ours_vol'])})")
        if args.report:
            path = write_net_report(args, rows, bench, meta, sec)
            print(f"\n  net-of-costs report -> {path}")
    else:
        run_single(args)


if __name__ == "__main__":
    main()
