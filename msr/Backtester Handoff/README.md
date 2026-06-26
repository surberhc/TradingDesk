# Backtester Handoff (Option A) — Start Here

Point-in-time MSR signal features to merge into your momentum backtester as a
volatility/regime overlay. No look-ahead — each row is "what the report showed that morning."

## Files here
- `_msr_features_market.csv` — 281 daily rows × 23 market-wide SPX features (gamma state,
  above-flip, expected move, risk skew, gamma walls, rvol/trend/percentile, flip-cross events,
  + the Tier-C proprietary regime calls). Apply as a market-wide overlay to any instrument.
- `_msr_features_sector.csv` — 4,710 rows, per-ETF features for 17 ETFs (sector-rotation overlays).
- `_msr_methodology_spec.md` — usage playbook (buy/sell/size/hedge logic) + rebuild-it-yourself
  engineering (formulas, Tiingo/IBKR sources, reproducibility tiers A/B/C). READ THIS.
- `msr.db` — full database for deeper joins.

## How to use
- Merge `_msr_features_market.csv` into your backtest on `date`.
- Integration method is intentionally open — test filter / sizing / hedging / timing as hypotheses.
- Reproducibility: Tier A = free (Tiingo prices), Tier B = options feed (IBKR), Tier C = newsletter-only.

## Note on updating the data
New data is NOT ingested from this folder. To add new reports, drop the PDFs into the main
Tier 1 Alpha folder and run `py _msr_ingest.py` THERE (the pipeline + raw files live up one level),
then re-copy the refreshed CSVs/msr.db here.

These are COPIES; the originals + the live pipeline live one level up in the Tier 1 Alpha folder.
