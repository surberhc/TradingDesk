"""
Stage B, Task 2 step 3-4: compare s8_sim_calibration_2025_2026.csv (the
mechanical simulator's independent re-derivation) against the real-fills
headline numbers already established in this project:
  - docs/S8_SPEC.md section 4: +$138,982 / +108.8% blended, all templates,
    236 trading days, real execution-level fills.
  - british_ic/S8_80_4_ONLY_FULL_BACKTEST.md cut (a): 80-$4 true-labeled only,
    141 days, S8(B2-corrected) total +$66,536 (+52.1%).

Both real-fills numbers are on a REAL-SIZE basis (actual contracts traded per
combo, varying day to day). The simulator's pnl_per_spread is a PER-SPREAD
(1-contract) basis. This script reports the simulator's per-spread total AND
a size-matched estimate using combo_ledger.csv's real short_open_qty per
template/day, so both an honest apples-to-apples-shape comparison (per-spread)
and a size-adjusted dollar estimate are available. See SIMULATOR_STAGE_B_PROGRESS.md
for the sizing-choice discussion.
"""

import pandas as pd
import numpy as np

REFERENCE_BALANCE = 127_710.0
REAL_HEADLINE_TOTAL = 138_982.0
REAL_HEADLINE_PCT = 1.088

sim = pd.read_csv("s8_sim_calibration_2025_2026.csv")
valid = sim.dropna(subset=["pnl_per_spread"])

print("=" * 78)
print("SIMULATOR CALIBRATION SUMMARY (per-spread basis)")
print("=" * 78)
print(f"Total simulated trades: {len(sim)}")
print(f"Valid P&L rows: {len(valid)} / {len(sim)}")
total_per_spread = valid["pnl_per_spread"].sum()
print(f"\nTotal simulated P&L (per-spread, $/contract basis): ${total_per_spread:,.2f}")
print(f"As a %% of reference balance ${REFERENCE_BALANCE:,.0f}: {total_per_spread/REFERENCE_BALANCE:+.1%}")

print(f"\nReal headline (docs/S8_SPEC.md sec 4, real fills, real sizing): "
      f"${REAL_HEADLINE_TOTAL:,.0f} ({REAL_HEADLINE_PCT:+.1%})")

print("\nPer-template simulated totals (per-spread):")
by_tmpl = valid.groupby("template")["pnl_per_spread"].agg(["sum", "count", "mean"]).sort_values("sum", ascending=False)
by_tmpl.columns = ["total_pnl_per_spread", "n_trades", "mean_pnl_per_spread"]
print(by_tmpl.to_string())

print("\nExit reason breakdown:")
print(sim["exit_reason"].value_counts(dropna=False).to_string())
print(f"\nWin rate (pnl_per_spread > 0): {(valid['pnl_per_spread'] > 0).mean():.1%}")

# ---- size-matched estimate using combo_ledger real short_open_qty ----
print("\n" + "=" * 78)
print("SIZE-MATCHED ESTIMATE (median real short_open_qty applied to sim per-spread P&L)")
print("=" * 78)
combo = pd.read_csv("combo_ledger.csv")
# short_open_qty is signed negative (a short position); use absolute contract count
median_qty = combo["short_open_qty"].abs().median()
mean_qty = combo["short_open_qty"].abs().mean()
print(f"Real combo_ledger.csv short_open_qty: median={median_qty}, mean={mean_qty:.2f}, "
      f"n={len(combo)}")
print(f"\nSize-matched total (median qty x per-spread total): "
      f"${total_per_spread * median_qty:,.2f}")
print(f"Size-matched total (mean qty x per-spread total): "
      f"${total_per_spread * mean_qty:,.2f}")
print("\nCAVEAT: this is a rough scalar size-match (one constant multiplier), not a "
      "true per-trade size match -- real sizing varies by day/template/regime and "
      "the simulator has no sizing model of its own. Treat as an order-of-magnitude "
      "translation, not a precise dollar reconciliation.")

# ---- 80-4-only comparable cut ----
print("\n" + "=" * 78)
print("80-$4-ONLY CUT (comparable to S8_80_4_ONLY_FULL_BACKTEST.md cut (a): "
      "+$66,536 / +52.1%, 141 days, real B2-corrected)")
print("=" * 78)
for tk in ["British IC - Puts - 80 - $4", "British IC - Calls - 80 - $4"]:
    sub = valid[valid["template"] == tk]
    print(f"{tk}: n={len(sub)}, total=${sub['pnl_per_spread'].sum():,.2f}, "
          f"mean=${sub['pnl_per_spread'].mean():,.2f}")
combined_80_4 = valid[valid["template"].isin(
    ["British IC - Puts - 80 - $4", "British IC - Calls - 80 - $4"])]
print(f"\nCombined 80-$4 (puts+calls) simulated total (per-spread): "
      f"${combined_80_4['pnl_per_spread'].sum():,.2f}")
print(f"Size-matched (median qty): ${combined_80_4['pnl_per_spread'].sum()*median_qty:,.2f}")

print("\n" + "=" * 78)
print("SIGN + ORDER-OF-MAGNITUDE CHECK")
print("=" * 78)
sim_sign = "POSITIVE" if total_per_spread > 0 else "NEGATIVE"
real_sign = "POSITIVE" if REAL_HEADLINE_TOTAL > 0 else "NEGATIVE"
print(f"Simulated total sign: {sim_sign}  |  Real headline sign: {real_sign}  |  "
      f"{'MATCH' if sim_sign==real_sign else 'MISMATCH -- RED FLAG'}")
sim_size_matched = total_per_spread * median_qty
ratio = sim_size_matched / REAL_HEADLINE_TOTAL if REAL_HEADLINE_TOTAL else float("nan")
print(f"Size-matched sim total / real headline total ratio: {ratio:.2f}x")
