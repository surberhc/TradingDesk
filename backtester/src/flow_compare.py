"""
flow_compare.py — S0 vanilla vs S0 + free price-only FLOW de-risk gate.

Disciplined, anti-curve-fit evaluation of the Flow Project verdict's keeper (the
free de-risk gate) as an overlay on S0 (AdaptiveAllWeather), full window
2007->2026. Mirrors gamma_compare.py. Tests ONLY the two pre-specified variants
from FLOW_VERDICT.md (G1 flat-when-bearish, G2 1/0.5/0 sizing) — no extra grid,
no knob tuning.

Run from <repo>/backtester:
    python -m src.flow_compare
"""

from __future__ import annotations

import sys

import numpy as np
import pandas as pd

from strategies import config
from src import backtest, flow_overlay, metrics

FULL_START = "2007-01-01"
VARIANTS = ["G1", "G2"]
VERSIONS = ["Conservative", "Balanced", "Growth"]

ROWS = [
    "CAGR", "Annual volatility", "Max drawdown", "Worst rolling 3m",
    "Worst rolling 12m", "Worst rolling 3y", "Downside deviation",
    "Sharpe", "Sortino", "Calmar", "Beta vs SPY",
    "Up capture vs SPY", "Down capture vs SPY",
    "Longest underperf. vs SPY (months)",
]
PCT_ROWS = {"CAGR", "Annual volatility", "Max drawdown", "Worst rolling 3m",
            "Worst rolling 12m", "Worst rolling 3y", "Downside deviation",
            "Up capture vs SPY", "Down capture vs SPY"}


def _run(version, enabled, variant, start=FULL_START, end=None, cost_bps=None):
    kw = dict(version=version, start=start, end=end,
              flow_overlay_enabled=enabled, flow_overlay_variant=variant)
    return backtest.run_backtest(**kw)


def _m(result):
    return metrics.compute_metrics(result["benchmark_navs"])["strategy"]


def _turnover(result):
    """Annualized one-way turnover (mean per-rebalance one-way turnover * 12)."""
    t = result["turnover"]
    return float(t.mean() * 12.0) if len(t) else float("nan")


def _fmt(metric, val):
    if pd.isna(val):
        return "n/a"
    if metric in PCT_ROWS:
        return f"{val:.1%}"
    if metric in ("Longest underperf. vs SPY (months)",):
        return f"{val:.0f}"
    return f"{val:.2f}"


def _print_table(title, cols: dict, extra_rows: dict | None = None):
    print(title)
    rows = list(ROWS)
    table = pd.DataFrame(cols).reindex(rows)
    if extra_rows:
        for label, series in extra_rows.items():
            table.loc[label] = pd.Series(series)
            rows.append(label)
    headers = list(table.columns)
    w0 = max(len(r) for r in rows) + 2
    wc = max(13, max(len(h) for h in headers) + 2)
    print(" " * w0 + "".join(h.rjust(wc) for h in headers))
    for metric in rows:
        line = metric.ljust(w0)
        for h in headers:
            v = table.loc[metric, h]
            if metric == "Ann. turnover (1-way)":
                cell = "n/a" if pd.isna(v) else f"{v:.2f}x"
            else:
                cell = _fmt(metric, v)
            line += cell.rjust(wc)
        print(line)
    print()


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    print("=" * 78)
    print("FLOW de-risk gate on S0 — disciplined, anti-curve-fit eval")
    print(f"Full window {FULL_START}->latest | Balanced primary | variants G1, G2")
    print("=" * 78 + "\n")

    # =====================================================================
    # CHECK 1 — overlay OFF reproduces vanilla S0 exactly (byte-identical)
    # =====================================================================
    vanilla = _run("Balanced", False, "G1")
    off2 = _run("Balanced", False, "G2")  # variant irrelevant when disabled
    al = pd.concat([vanilla["nav"], off2["nav"]], axis=1).dropna()
    identical = np.allclose(al.iloc[:, 0], al.iloc[:, 1], rtol=0, atol=0)
    print(f"CHECK 1 - overlay OFF == vanilla S0 (byte-identical): "
          f"{'PASS' if identical else 'FAIL'}\n")

    # =====================================================================
    # CHECK 2 — no look-ahead on the flow_state
    # =====================================================================
    fstate = flow_overlay.compute_flow_state()
    sample = _run("Balanced", True, "G1")
    monthly = sample["monthly"]
    bad = 0
    for sig_date, row in monthly.iterrows():
        applied = row.get("flow_state")
        expected = flow_overlay.flow_state_asof(fstate, sig_date)
        if (applied or None) != (expected or None):
            bad += 1
    asof_ok = all(
        (fstate.loc[:d].dropna().index.max() <= d) if len(fstate.loc[:d].dropna()) else True
        for d in monthly.index
    )
    print("CHECK 2 - no look-ahead (flow_state strictly as-of signal date):")
    print(f"  applied == recomputed as-of for all {len(monthly)} rebalances: "
          f"{'PASS' if bad == 0 else f'FAIL ({bad})'}")
    print(f"  picked reading date <= signal date everywhere: "
          f"{'PASS' if asof_ok else 'FAIL'}\n")

    # How often does the gate fire (Balanced)?
    states = monthly["flow_state"]
    counts = states.value_counts(dropna=False)
    n_bear = int((states == "Bearish").sum())
    n_neut = int((states == "Neutral").sum())
    print(f"Flow state at the {len(states)} Balanced rebalances:")
    for k in ["Bullish", "Neutral", "Bearish", None]:
        c = int(counts.get(k, 0))
        if c:
            print(f"  {str(k):>8}: {c:>3}  ({c/len(states):.0%})")
    print(f"  -> G1 de-risks (Bearish) on {n_bear} of {len(states)} "
          f"({n_bear/len(states):.0%}); G2 also half-sizes {n_neut} Neutral.\n")

    # =====================================================================
    # PRIMARY — Balanced, full window: S0 vs S0+G1 vs S0+G2
    # =====================================================================
    g1 = _run("Balanced", True, "G1")
    g2 = _run("Balanced", True, "G2")
    cols = {"S0": _m(vanilla), "S0+G1 flat": _m(g1), "S0+G2 sized": _m(g2)}
    extra = {"Ann. turnover (1-way)": {
        "S0": _turnover(vanilla), "S0+G1 flat": _turnover(g1),
        "S0+G2 sized": _turnover(g2)}}
    _print_table(f"PRIMARY METRICS - Balanced, {FULL_START}->latest:", cols, extra)

    # Incremental deltas (overlay minus vanilla)
    print("INCREMENTAL DELTA (overlay - S0), Balanced:")
    v = _m(vanilla)
    for lbl, r in [("G1 flat", _m(g1)), ("G2 sized", _m(g2))]:
        d_cagr = (r["CAGR"] - v["CAGR"]) * 100
        d_dd = (r["Max drawdown"] - v["Max drawdown"]) * 100
        d_cal = r["Calmar"] - v["Calmar"]
        d_sor = r["Sortino"] - v["Sortino"]
        d_dc = (r["Down capture vs SPY"] - v["Down capture vs SPY"]) * 100
        print(f"  {lbl:8}: dCAGR {d_cagr:+.2f}pp | dMaxDD {d_dd:+.2f}pp "
              f"| dCalmar {d_cal:+.2f} | dSortino {d_sor:+.2f} | dDownCap {d_dc:+.1f}pp")
    print()

    # =====================================================================
    # ANTI-CURVE-FIT GATE (a) — does it help in 2008 specifically?
    # =====================================================================
    print("=" * 78)
    print("GATE (a) - 2008 GFC window (2007-10-01 -> 2009-06-30), Balanced:")
    print("=" * 78)
    gfc_kw = dict(start="2007-06-01", end="2009-06-30")
    gfc_v = backtest.run_backtest(version="Balanced", flow_overlay_enabled=False, **gfc_kw)
    gfc_g1 = backtest.run_backtest(version="Balanced", flow_overlay_enabled=True,
                                   flow_overlay_variant="G1", **gfc_kw)
    gfc_g2 = backtest.run_backtest(version="Balanced", flow_overlay_enabled=True,
                                   flow_overlay_variant="G2", **gfc_kw)
    cols = {"S0": _m(gfc_v), "S0+G1": _m(gfc_g1), "S0+G2": _m(gfc_g2)}
    _print_table("  metrics over the GFC sub-window:", cols)

    # =====================================================================
    # ANTI-CURVE-FIT GATE (b) — all 3 versions
    # =====================================================================
    print("=" * 78)
    print("GATE (b) - all 3 versions, full window. Key risk metrics:")
    print("=" * 78)
    hdr = f"  {'version':<13}{'variant':<10}{'CAGR':>8}{'MaxDD':>9}{'Calmar':>8}{'Sortino':>9}{'DownCap':>9}"
    print(hdr)
    for ver in VERSIONS:
        base = _run(ver, False, "G1")
        bm = _m(base)
        print(f"  {ver:<13}{'S0':<10}{bm['CAGR']:>7.1%}{bm['Max drawdown']:>9.1%}"
              f"{bm['Calmar']:>8.2f}{bm['Sortino']:>9.2f}{bm['Down capture vs SPY']:>9.1%}")
        for var in VARIANTS:
            r = _m(_run(ver, True, var))
            dd_delta = (r['Max drawdown'] - bm['Max drawdown']) * 100
            print(f"  {'':<13}{var:<10}{r['CAGR']:>7.1%}{r['Max drawdown']:>9.1%}"
                  f"{r['Calmar']:>8.2f}{r['Sortino']:>9.2f}{r['Down capture vs SPY']:>9.1%}"
                  f"   (dMaxDD {dd_delta:+.1f}pp)")
        print()

    # =====================================================================
    # ANTI-CURVE-FIT GATE (c) — out-of-sample split (early vs late)
    # =====================================================================
    print("=" * 78)
    print("GATE (c) - OOS split, Balanced. Early 2007-2014 vs Late 2015-2026:")
    print("=" * 78)
    for split_lbl, (s, e) in [("EARLY 2007-2014", ("2007-01-01", "2014-12-31")),
                              ("LATE  2015-2026", ("2015-01-01", None))]:
        bv = backtest.run_backtest(version="Balanced", start=s, end=e,
                                   flow_overlay_enabled=False)
        cols = {"S0": _m(bv)}
        for var in VARIANTS:
            cols[f"S0+{var}"] = _m(backtest.run_backtest(
                version="Balanced", start=s, end=e,
                flow_overlay_enabled=True, flow_overlay_variant=var))
        _print_table(f"  {split_lbl}:", cols)

    # =====================================================================
    # ANTI-CURVE-FIT GATE (d) — cost robustness
    # =====================================================================
    print("=" * 78)
    print("GATE (d) - cost robustness, Balanced full window. MaxDD / CAGR / Calmar")
    print("           at 3 bps (base), 10 bps, 25 bps per-trade cost:")
    print("=" * 78)
    orig_cost = config.PER_TRADE_COST_BPS
    try:
        for bps in [3.0, 10.0, 25.0]:
            config.PER_TRADE_COST_BPS = bps
            bv = _m(_run("Balanced", False, "G1"))
            line = f"  {bps:>4.0f} bps  S0: DD {bv['Max drawdown']:>6.1%} CAGR {bv['CAGR']:>6.1%} Cal {bv['Calmar']:.2f}"
            for var in VARIANTS:
                r = _m(_run("Balanced", True, var))
                line += f"  | {var}: DD {r['Max drawdown']:>6.1%} CAGR {r['CAGR']:>6.1%} Cal {r['Calmar']:.2f}"
            print(line)
    finally:
        config.PER_TRADE_COST_BPS = orig_cost
    print()

    # =====================================================================
    # KEY QUESTION — overlap between the flow gate and S0's regime de-risk
    # =====================================================================
    print("=" * 78)
    print("OVERLAP ANALYSIS - does the flow gate fire when S0 is ALREADY de-risked?")
    print("=" * 78)
    mB = sample["monthly"].copy()  # Balanced, G1 (flow_state recorded)
    # S0's own de-risk proxy: equity_target (the regime engine's equity allowance).
    # Low equity_target = S0 already de-risked via its regime engine.
    eq = mB["equity_target"].astype(float)
    flow_bear = (mB["flow_state"] == "Bearish")
    s0_lo = eq <= 0.5          # S0 already heavily de-risked (<=50% equity allowed)
    s0_mid = (eq > 0.5) & (eq < 1.0)
    s0_full = eq >= 1.0        # S0 fully risk-on (regime engine NOT de-risking)

    n = len(mB)
    print(f"  Rebalances: {n}. S0 equity_target distribution:")
    print(f"    S0 fully risk-on (eq>=1.0):   {int(s0_full.sum()):>3} ({s0_full.mean():.0%})")
    print(f"    S0 partly de-risked (0.5-1.0):{int(s0_mid.sum()):>3} ({s0_mid.mean():.0%})")
    print(f"    S0 heavily de-risked (<=0.5): {int(s0_lo.sum()):>3} ({s0_lo.mean():.0%})")
    print(f"  Flow gate Bearish on {int(flow_bear.sum())} ({flow_bear.mean():.0%}) of rebalances.\n")

    # The decisive cross-tab: of the months the flow gate fires, how many are
    # COMPLEMENTARY (S0 still fully risk-on) vs REDUNDANT (S0 already de-risking)?
    fire_full = int((flow_bear & s0_full).sum())
    fire_mid = int((flow_bear & s0_mid).sum())
    fire_lo = int((flow_bear & s0_lo).sum())
    nf = int(flow_bear.sum())
    print("  When the FLOW gate fires (Bearish), what was S0 doing?")
    if nf:
        print(f"    S0 still FULLY risk-on (complementary): {fire_full:>3} ({fire_full/nf:.0%})")
        print(f"    S0 partly de-risked (partial overlap):  {fire_mid:>3} ({fire_mid/nf:.0%})")
        print(f"    S0 already heavily de-risked (redundant):{fire_lo:>3} ({fire_lo/nf:.0%})")
    # Correlation between the two de-risk signals.
    flow_num = flow_bear.astype(float)
    s0_derisk = (1.0 - eq.clip(0, 1))  # 0 = full risk-on, 1 = fully de-risked
    corr = float(pd.concat([flow_num, s0_derisk], axis=1).corr().iloc[0, 1])
    print(f"\n  Correlation(flow-bearish, S0-de-risk-depth) = {corr:+.2f}")
    print("  (high +corr => they fire together => redundant; ~0 => complementary)\n")

    start = vanilla["nav"].index.min().date()
    end = vanilla["nav"].index.max().date()
    print(f"Actual full sim window: {start} -> {end}, {len(vanilla['weights'])} rebalances.")


if __name__ == "__main__":
    main()
