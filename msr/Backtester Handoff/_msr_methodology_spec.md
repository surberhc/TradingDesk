# MSR Signal Dataset — Methodology & Usage Spec

Companion to `msr.db` and the two feature tables:
- **`_msr_features_market.csv`** — 281 daily rows, market-wide SPX regime/vol features (one overlay for any instrument).
- **`_msr_features_sector.csv`** — 4,710 rows, per-ETF features for the 17 sector/asset ETFs (sector-rotation overlays).

All features are **point-in-time** ("what the report showed that morning") with **no look-ahead** — safe to merge into a backtester on `date`. Source period: 2025-05-01 → 2026-06-18 (one mostly-bull regime + the April-2025 stress tail).

Each feature is tagged with a **reproducibility tier**:
- **A** — free from a price feed (Tiingo): realized vol, returns, beta.
- **B** — rebuildable from an options chain (IBKR) + public methodology (GEX, expected move): flip, gamma state, throttle, bands, strikes, skew.
- **C** — newsletter-proprietary (their flow/composite models): `regime_flow_risk`, `regime_strategic`, `regime_pvband_rr`. Not reproducible; flagged so you know what depends on the subscription.

---

## SECTION 1 — How to use it (playbook)

The core mental model: **this is a volatility/regime layer, not a directional engine.** Momentum is your directional engine. These signals tell you *when to trust it, how big to size it, and when to hedge it.*

### Market-wide features (`_msr_features_market.csv`)

| Feature | Plain meaning | Decision it informs |
|---|---|---|
| `spx_gamma_state` (Pos/Neu/Neg) | Dealer hedging regime | **Vol/risk gate.** Negative = fragile, moves amplified; Positive = dampened. *Validated:* next-day move 0.85% (Neg) vs 0.50% (Pos). |
| `spx_above_flip` (1/0) | Is SPX above the gamma flip | Cleanest version of the above. Below flip = the high-vol side. |
| `dist_to_flip_pct` | % cushion above/below flip | Continuous fragility. Small + positive = near the cliff edge → trim/hedge. |
| `gex_throttle` | Vol-suppression strength | Low = expect bigger moves → de-risk; high = pinned. |
| `spx_expected_move_pct` | Implied move (band width) | **Vol-target denominator** for position sizing. |
| `risk_skew` (\|down\|−up) | Band asymmetry | >0 = more downside room priced → hedge tilt / reduce longs. |
| `support/focal/resistance_strike` + `dist_to_*` | Gamma walls | **Stops, targets, fade levels.** Focal = magnet near expiry. |
| `spx_rvol_1m`, `vol_trend`, `rvol_pctile` | Realized vol level/direction/rank | Momentum works best in **falling / low-percentile** vol. |
| `gamma_flip_cross` | Event: crossed the flip | **Hedge-on/off trigger.** `cross_down` = de-risk event. |
| `regime_flow_risk` (C) | Bull/Neutral/Bear flow call | **Directional/conviction candidate** (see evidence) — not yet validated. |
| `regime_strategic` (C) | Risk On/Neutral/Off (slow) | Slow strategic backdrop; contrarian at extremes (thin evidence). |

### The four ways to wire it into momentum (all testable in the backtester)
1. **Regime gate** — only take momentum longs when `spx_above_flip=1` (or gamma ≠ Negative). Biggest drawdown-reduction lever; deep drawdowns occurred 100% in negative gamma.
2. **Vol-target sizing** — size ∝ 1 / `spx_expected_move_pct` (or `spx_rvol_1m`). Smaller below flip, larger above.
3. **Hedging overlay** — add protection on `gamma_flip_cross=cross_down` or `risk_skew>0`; cheap insurance while *above* flip (low IV).
4. **Entry/exit timing** — use `support/focal/resistance` as stops/targets; fade toward focal in Positive gamma, ride breakouts in Negative gamma.

### Sector features (`_msr_features_sector.csv`)
Same logic per ETF: `risk_skew` and `rvol_pctile` rank which sectors are fragile vs. calm; `spread_pct` sizes sector bets; `beta_2y` for market-relative exposure. Use to tilt a sector-rotation momentum book toward low-skew/low-vol-percentile sectors.

---

## SECTION 2 — Rebuild-it-yourself (engineering)

Goal: reproduce the **Tier A/B** features from your own Tiingo + IBKR feeds so the live signal doesn't depend on the newsletter. (Our 281-day DB is the **calibration set** — tune your output to match it within margin, then run forward.)

| Feature | Formula / method | Source | Tier | Gotcha |
|---|---|---|---|---|
| `spx_rvol_1m`/`_3m` | stdev(log returns, 21/63d) × √252 | Tiingo | A | exact match, free |
| returns, `vol_trend`, `rvol_pctile` | arithmetic on price | Tiingo | A | use expanding (not full-sample) rank to stay point-in-time |
| `beta_2y` | 2y regression vs SPX | Tiingo | A | exact |
| GEX profile, `gex_flip` | Σ strike: gamma×OI×spot×sign; flip = cumulative-gamma zero-cross | IBKR chain (strikes/OI/IV) | B | **needs per-strike open interest** — the key dependency; dealer-sign convention shifts flip by a strike or two |
| `spx_gamma_state`, `above_flip` | sign of net dealer gamma / price vs flip | IBKR | B | follows directly from GEX profile |
| `gex_throttle` | normalized gamma density (their exact formula proprietary) | IBKR | B | reproduce concept; calibrate scale to our 281-day series |
| `spx_expected_move_pct`, bands, `risk_skew` | ATM IV × √t (expected move); bands = spot×(1±move) | IBKR IV | B | their exact expiration-weighting is proprietary; ATM straddle gets you close |
| `support/focal/resistance_strike` | largest-gamma strikes around spot | IBKR | B | focal = max |gamma| strike |
| `regime_flow_risk`, `regime_strategic`, `regime_pvband_rr` | their vol-control/CTA/risk-parity flow models + composite rules | — | **C** | **not reproducible** — proprietary; keep newsletter if these prove valuable (see flow project) |

**The one hard dependency: per-strike open interest.** IBKR serves OI as a *forward daily snapshot*, not deep history. So a self-built GEX backtest only accrues from the day you start collecting; backfill requires purchased historical options data (ORATS/CBOE) or continued newsletter ingestion. Plan: start the IBKR OI collector now, keep ingesting the newsletter to extend history in parallel.

---

## SECTION 3 — Validated relationships (the evidence)

- **Dealer gamma → next-day realized volatility** (strong, monotonic): Positive 0.50% / Neutral 0.65% / Negative 0.85%; above-flip 0.50% vs below-flip 0.83%. **Reproducible (Tier B).**
- **Negative gamma ⊃ all deep drawdowns**: 100% of >3% drawdown days were in negative gamma (avg drawdown −3.13% Neg vs −0.34% Pos). Coincident/confirming, *not* leading (next-5-day move after a negative flip ≈ −0.4%).
- **Bands well-calibrated as daily ~1.3σ ranges**: ~86% next-day containment.
- **`regime_flow_risk` shows directional promise** (Tier C, UNVALIDATED): Bullish-flow fwd-20d +3.1% / 80% hit vs Bearish +1.2% / 52%; separates returns *within* gamma states. **Caveat: one bull regime, 38 episodes, drift-confounded** → this is the subject of the flow project, not a proven edge.

---

## SECTION 4 — Caveats
1. **Single regime era** — mostly bull + one brief crash. Directional findings are drift-confounded; vol findings are more robust.
2. **`spx_rvol_1m` is SPY's realized vol** as the SPX proxy (Tier A; swap for a direct SPX calc when you rebuild).
3. **Tier C signals depend on the subscription** and are unvalidated for direction.
4. **`implied_move_pct` was dropped** — it was a static template constant (1.26%) in the source, not live data.
5. Strikes/bands are rounded levels; treat as zones, not precise prices.
