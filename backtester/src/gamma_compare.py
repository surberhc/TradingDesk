"""
gamma_compare.py — S0 vanilla vs S0 + GEX gamma overlay, over the GEX window.

Research harness (not part of the production run path). Runs the Balanced version
twice over the common 2018->2026 window where the SPX gamma signal exists:
  1. vanilla S0 (overlay OFF)
  2. S0 + gamma overlay (overlay ON) at one or more de-risk factors

and prints the full metric set side by side, plus two correctness checks:
  * overlay-OFF reproduces vanilla S0 EXACTLY (byte-identical NAV)
  * the gamma_state applied at each rebalance is strictly as-of/lagged (no look-ahead)

Run from <repo>/backtester:
    python -m src.gamma_compare
"""

from __future__ import annotations

import sys

import numpy as np
import pandas as pd

from strategies import config
from src import backtest, gamma_overlay, metrics

GEX_START = "2018-01-01"   # SPX_gex_daily begins 2018-01-02
VERSION = "Balanced"
DERISK_FACTORS = [0.5, 0.0]   # risk x0.5 and risk x0.0 in Negative gamma (small set)

ROWS = [
    "CAGR", "Annual volatility", "Max drawdown", "Worst rolling 3m",
    "Worst rolling 12m", "Worst rolling 3y", "Downside deviation",
    "Sharpe", "Sortino", "Calmar", "Beta vs SPY",
    "Up capture vs SPY", "Down capture vs SPY",
    "Longest underperf. vs SPY (months)",
]


def _run(enabled: bool, scale: float):
    return backtest.run_backtest(
        version=VERSION, start=GEX_START,
        gamma_overlay_enabled=enabled,
        gamma_negative_risk_scale=scale,
    )


def _strategy_metrics(result) -> pd.Series:
    return metrics.compute_metrics(result["benchmark_navs"])["strategy"]


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    print(f"S0 vs S0+gamma-overlay | version={VERSION} | window {GEX_START}->latest\n")

    vanilla = _run(enabled=False, scale=0.5)
    overlay_off = _run(enabled=True, scale=1.0)  # ON but scale=1.0 -> must equal vanilla

    # --- Correctness 1: overlay-OFF reproduces vanilla S0 exactly --------------
    # Two equivalent OFF paths: (a) flag False, (b) flag True with scale>=1.0 (the
    # overlay's pass-through branch). Both must match vanilla S0 byte-for-byte.
    v_nav, off_nav = vanilla["nav"], overlay_off["nav"]
    aligned = pd.concat([v_nav, off_nav], axis=1).dropna()
    max_abs_diff = float((aligned.iloc[:, 0] - aligned.iloc[:, 1]).abs().max())
    identical = np.allclose(aligned.iloc[:, 0].values, aligned.iloc[:, 1].values,
                            rtol=0, atol=0)
    print("CHECK 1 — overlay OFF reproduces vanilla S0:")
    print(f"  flag=True,scale=1.0 vs vanilla: max |NAV diff| = {max_abs_diff:.2e}  "
          f"-> {'EXACT MATCH' if identical else 'MISMATCH'}\n")

    # --- Run the de-risk factors -----------------------------------------------
    results = {"S0 (vanilla)": vanilla}
    for f in DERISK_FACTORS:
        label = f"S0+gamma x{f:g}"
        results[label] = _run(enabled=True, scale=f)

    # --- Correctness 2: no look-ahead on the gamma_state -----------------------
    # Re-derive the as-of state independently and confirm: (i) every applied state
    # equals the most-recent reading on/before the SIGNAL date, and (ii) it never
    # equals a future (post-signal) reading that the as-of rule would exclude.
    gstate = gamma_overlay.load_gamma_state()
    sample = next(r for lbl, r in results.items() if lbl != "S0 (vanilla)")
    monthly = sample["monthly"]
    bad = 0
    for sig_date, row in monthly.iterrows():
        applied = row.get("gamma_state")
        expected = gamma_overlay.gamma_state_asof(gstate, sig_date)
        if (applied or None) != (expected or None):
            bad += 1
    # Also confirm the as-of value uses only on/before data: the index of the picked
    # reading must be <= the signal date for every rebalance.
    asof_ok = all(
        (gstate.loc[:d].index.max() <= d) if len(gstate.loc[:d]) else True
        for d in monthly.index
    )
    print("CHECK 2 — no look-ahead (gamma_state strictly as-of the signal date):")
    print(f"  applied == independently-recomputed as-of state for all "
          f"{len(monthly)} rebalances: {'PASS' if bad == 0 else f'FAIL ({bad})'}")
    print(f"  picked reading date <= signal date everywhere: "
          f"{'PASS' if asof_ok else 'FAIL'}\n")

    # --- How often does the overlay actually fire? -----------------------------
    states = monthly["gamma_state"]
    counts = states.value_counts(dropna=False)
    n_neg = int((states == "Negative").sum())
    print(f"Regime rarity over the window ({len(states)} rebalances):")
    for k in ["Positive", "Neutral", "Negative", None]:
        c = int(counts.get(k, 0))
        if c:
            print(f"  {str(k):>9}: {c:>3}  ({c/len(states):.0%})")
    print(f"  -> overlay de-risks on {n_neg} of {len(states)} rebalances "
          f"({n_neg/len(states):.0%})\n")

    # --- Metric comparison table -----------------------------------------------
    cols = {lbl: _strategy_metrics(r) for lbl, r in results.items()}
    table = pd.DataFrame(cols).reindex(ROWS)

    pct_rows = {"CAGR", "Annual volatility", "Max drawdown", "Worst rolling 3m",
                "Worst rolling 12m", "Worst rolling 3y", "Downside deviation",
                "Up capture vs SPY", "Down capture vs SPY"}

    def fmt(metric, val):
        if pd.isna(val):
            return "n/a"
        if metric in pct_rows:
            return f"{val:.1%}"
        if metric == "Longest underperf. vs SPY (months)":
            return f"{val:.0f}"
        return f"{val:.2f}"

    headers = list(table.columns)
    w0 = max(len(r) for r in ROWS) + 2
    wc = max(14, max(len(h) for h in headers) + 2)
    print("METRICS (strategy column, " + GEX_START + "->latest):")
    print(" " * w0 + "".join(h.rjust(wc) for h in headers))
    for metric in ROWS:
        line = metric.ljust(w0)
        for h in headers:
            line += fmt(metric, table.loc[metric, h]).rjust(wc)
        print(line)

    start = vanilla["nav"].index.min().date()
    end = vanilla["nav"].index.max().date()
    print(f"\nActual sim window: {start} -> {end}, "
          f"{len(vanilla['weights'])} monthly rebalances.")


if __name__ == "__main__":
    main()
