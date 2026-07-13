r"""
s8_regime_bucketing.py -- is S8's real-fills edge concentrated in one type of market
day, or does it hold up across regime diversity?

Cheaper alternative to the aborted 2022-2026 mechanical-simulator extension (see
s8_mechanical_simulator.py's removal, commit 8372db0): rather than manufacture MORE
calendar years of synthetic trades to test regime coverage, this buckets the ONE
year of REAL fills already trusted (alpha_vs_beta_daily_series.csv, 236 rows,
pnl_s8 = B2-corrected real daily P&L) by the regime that was ACTUALLY in effect
each day. Zero synthetic execution -- no strike selection, no fill model, no spot
estimation to get wrong.

Regime label comes from S0's EXISTING, FROZEN regime engine
(strategies/strategies/parts/regime.py -- market_health_score() + apply_hysteresis()),
called the exact same way dailyreport/eod_report.py's build_s0_regime() does (same
data_loader.load_prices/load_vix/load_hy_oas calls, same CREDIT_PROXY denom lookup).
No new thresholds introduced for this test -- avoids curve-fitting the test itself.

Sanity gate: the regime-bucketed totals must reconcile to the already-published
headline (+$138,982 S8 / +$42,765 actual) before the bucket table is trusted.

PAPER / research only. OFFLINE. STRICTLY READ-ONLY on all source CSVs/parquets.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

OUT_DIR = Path(__file__).resolve().parent
DAILY_CSV = OUT_DIR / "alpha_vs_beta_daily_series.csv"
RESULTS_CSV = OUT_DIR / "s8_regime_bucketing_results.csv"
REPORT = OUT_DIR / "S8_REGIME_BUCKETING.md"

_BACKTESTER_ROOT = str((OUT_DIR.parent / "backtester").resolve())
if _BACKTESTER_ROOT not in sys.path:
    sys.path.insert(0, _BACKTESTER_ROOT)

HEADLINE_S8_TARGET = 138_982.0
HEADLINE_ACTUAL_TARGET = 42_765.0
SANITY_TOLERANCE = 5.0  # dollars -- this script does no reconstruction of its own,
                         # just resums the already-reconciled daily series, so the
                         # tolerance can be tight (alpha_vs_beta_decomposition.py's
                         # own sanity gate already used $100 for its OWN reconstruction)


def load_daily_series() -> pd.DataFrame:
    if not DAILY_CSV.exists():
        raise FileNotFoundError(
            f"{DAILY_CSV} not found -- run alpha_vs_beta_decomposition.py first, "
            "this script only re-buckets its already-reconciled daily output."
        )
    df = pd.read_csv(DAILY_CSV, parse_dates=["date"])
    return df


def load_regime_series(dates: pd.DatetimeIndex) -> tuple[pd.Series, dict]:
    """Score + confirmed regime, computed the SAME way dailyreport/eod_report.py's
    build_s0_regime() does -- S0's own live regime engine, no new thresholds."""
    from src import data_loader
    from strategies import config as s_config
    from strategies.parts import regime as s_regime

    prices = data_loader.load_prices()
    hyg = data_loader.load_prices([s_config.CREDIT_PROXY[0]])[s_config.CREDIT_PROXY[0]]
    denom_t = s_config.CREDIT_PROXY[1]
    credit_denom = (prices[denom_t] if denom_t in prices.columns
                    else data_loader.load_prices([denom_t])[denom_t])
    vix, vix_src = data_loader.load_vix()
    hy_oas, hy_oas_src = data_loader.load_hy_oas()

    score_df = s_regime.market_health_score(
        prices, hyg=hyg, credit_denom=credit_denom, vix=vix, hy_oas=hy_oas)
    confirmed = s_regime.apply_hysteresis(score_df["score"])

    meta = dict(vix_src=vix_src, hy_oas_src=hy_oas_src,
                score_coverage_start=score_df["score"].first_valid_index(),
                score_coverage_end=score_df.index.max())

    # Reindex the confirmed-regime series onto S8's exact trading dates. ffill only
    # (never bfill) -- a date with no regime score yet must not borrow a FUTURE
    # regime label; it stays NaN and is reported as uncovered, not silently guessed.
    aligned = confirmed.reindex(confirmed.index.union(dates)).ffill().reindex(dates)
    return aligned, meta


def bucket(daily: pd.DataFrame, regime_by_date: pd.Series) -> pd.DataFrame:
    d = daily.copy()
    d["regime"] = regime_by_date.reindex(d["date"]).to_numpy()
    g = d.groupby("regime", dropna=False).agg(
        n_days=("pnl_s8", "size"),
        total_pnl=("pnl_s8", "sum"),
        mean_pnl=("pnl_s8", "mean"),
        win_rate=("pnl_s8", lambda x: float((x > 0).mean())),
    ).reset_index()
    return g.sort_values("total_pnl", ascending=False).reset_index(drop=True)


def main():
    print("[S8 regime-bucket] loading already-reconciled daily P&L series...", flush=True)
    daily = load_daily_series()
    n_days = len(daily)
    print(f"  {n_days} trading dates {daily['date'].min().date()}..{daily['date'].max().date()}",
          flush=True)

    total_s8 = float(daily["pnl_s8"].sum())
    total_actual = float(daily["pnl_actual"].sum())
    print(f"  total S8 (B2): ${total_s8:,.2f} (target +$138,982)", flush=True)
    print(f"  total actual:  ${total_actual:,.2f} (target +$42,765)", flush=True)
    gap_s8 = abs(total_s8 - HEADLINE_S8_TARGET)
    gap_actual = abs(total_actual - HEADLINE_ACTUAL_TARGET)
    sanity_pass = gap_s8 <= SANITY_TOLERANCE and gap_actual <= SANITY_TOLERANCE
    print(f"  [SANITY CHECK] gap to headline: S8 ${gap_s8:,.2f}, actual ${gap_actual:,.2f} "
          f"({'PASS' if sanity_pass else 'FAIL -- INVESTIGATE, do not trust bucket table'})",
          flush=True)
    if not sanity_pass:
        raise RuntimeError(
            f"Daily series totals (S8=${total_s8:,.2f}, actual=${total_actual:,.2f}) do not "
            f"reconcile to the published headline within ${SANITY_TOLERANCE:,.2f}. Stopping."
        )

    print("[S8 regime-bucket] computing S0's regime label for every trading date "
          "(frozen engine, no new thresholds)...", flush=True)
    regime_by_date, meta = load_regime_series(pd.DatetimeIndex(daily["date"]))
    n_uncovered = int(regime_by_date.isna().sum())
    print(f"  regime label available for {n_days - n_uncovered}/{n_days} dates "
          f"({n_uncovered} uncovered -- before the regime engine's own warm-up window)",
          flush=True)
    print(f"  VIX source: {meta['vix_src']}, HY OAS source: {meta['hy_oas_src']}", flush=True)

    tbl = bucket(daily, regime_by_date)
    tbl.to_csv(RESULTS_CSV, index=False)
    print(f"[S8 regime-bucket] wrote {RESULTS_CSV}", flush=True)
    print(tbl.to_string(index=False), flush=True)

    covered_days = int(tbl.loc[tbl["regime"].notna(), "n_days"].sum())
    covered_pct = covered_days / n_days if n_days else float("nan")
    n_profitable_buckets = int((tbl.loc[tbl["regime"].notna(), "total_pnl"] > 0).sum())
    n_buckets = int(tbl["regime"].notna().sum())

    write_report(tbl, n_days, covered_days, covered_pct, n_profitable_buckets, n_buckets, meta)
    print(f"[S8 regime-bucket] DONE -> {REPORT}", flush=True)


def write_report(tbl, n_days, covered_days, covered_pct, n_profitable_buckets, n_buckets, meta):
    L = []
    L.append("# S8 -- REGIME-BUCKETED REAL-FILLS RESULT\n")
    L.append(
        "Rebuild of the 2026-07-10 same-session finding (previously only narrated in "
        "the conductor log, not committed as a script -- this file closes that gap so "
        "the numbers are reproducible, not just asserted).\n"
    )
    L.append("## Method\n")
    L.append(
        "- Real daily S8 P&L: `alpha_vs_beta_daily_series.csv` (`pnl_s8`, 236 rows), "
        "already sanity-gated against the published +$138,982 / +$42,765 headline "
        "before this script trusts it.\n"
        "- Regime label: S0's existing, FROZEN regime engine "
        "(`strategies/strategies/parts/regime.py` -- `market_health_score()` + "
        "`apply_hysteresis()`), called exactly the way "
        "`dailyreport/eod_report.py:build_s0_regime()` does (same "
        "`data_loader.load_prices/load_vix/load_hy_oas` calls, same CREDIT_PROXY "
        "denominator lookup). No new thresholds introduced for this test.\n"
        f"- VIX source: {meta['vix_src']}. HY OAS source: {meta['hy_oas_src']}.\n"
        "- Zero synthetic execution: no strike selection, no fill model, no spot "
        "estimation -- purely a join of two already-real series by date.\n"
    )
    L.append("## Result\n")
    L.append("| Regime | Days | S8 total P&L | S8 avg/day | S8 win rate |")
    L.append("|---|---|---|---|---|")
    for _, row in tbl.iterrows():
        label = row["regime"] if pd.notna(row["regime"]) else "Uncovered (pre warm-up)"
        L.append(f"| {label} | {int(row['n_days'])} | ${row['total_pnl']:,.0f} | "
                  f"${row['mean_pnl']:,.0f} | {row['win_rate']:.1%} |")
    L.append("")
    L.append(
        f"Profitable in {n_profitable_buckets} of {n_buckets} regime buckets, covering "
        f"{covered_days} of {n_days} days ({covered_pct:.0%}).\n"
    )
    L.append("## Caveat, stated plainly\n")
    L.append(
        "The extreme-regime buckets are thin (single digits to low teens of days). This "
        "is NOT proof S8 survives a genuine multi-year bear market or a 2008-style event "
        "-- it is evidence the edge isn't fragile to the regime mix that actually occurred "
        "within the one year real fills exist for. A narrower claim than multi-year "
        "coverage would be, but one that depends on no synthetic execution.\n"
    )
    REPORT.write_text("\n".join(L), encoding="utf-8")


if __name__ == "__main__":
    main()
