# Flow-Model Research Project — Brief & Kickoff

## Purpose
Decide whether the MSR newsletter's **proprietary "Systematic Flow Risk" call** (and the related `regime_strategic` / `regime_pvband_rr` composites) carry **real, usable predictive edge** — enough to justify keeping the subscription as a directional/conviction layer — or whether they're noise we can drop in favor of the reproducible gamma/vol signals.

This is a *separate, focused study* because the flow model is the one genuinely proprietary, hard-to-reproduce piece of the dataset, and an initial scan suggested it may add directional value that the (reproducible) gamma signal does not.

## Background (what these signals are)
The newsletter estimates **systematic fund positioning** (vol-control, CTA, risk-parity flows) and distills it into discrete daily calls:
- `regime_flow_risk` ∈ {Bullish, Neutral, Bearish}
- `regime_strategic` ∈ {Risk On, Neutral, Risk Off} (slow, ~15-day persistence)
- `regime_pvband_rr` ∈ {Long, Neutral, Short}

These are **Tier C** — not reproducible from market data (unlike GEX/flip/vol bands). So their value must be proven to justify dependence on the vendor.

## What we found so far (the hypothesis to validate — NOT yet proven)
In a first-pass value test on 281 daily reports (2025-05-01 → 2026-06-18):
- `regime_flow_risk` **separated forward returns**: Bullish-flow → +3.1% fwd-20d / 80% hit-rate vs Bearish-flow → +1.2% / 52%.
- It added separation **within** gamma states (i.e., edge beyond the reproducible signal).
- `regime_strategic` "Risk Off" preceded the *strongest* forward returns (contrarian / caught bottoms) — but only **7 independent episodes**.

### Why this is only a hypothesis (the caveats that this project must break)
1. **Bull-market confound** — the entire sample drifted up +23%; almost every forward return is positive, so "Bullish → more positive" is partly just drift.
2. **Thin episodes** — flow_risk = 38 runs, strategic = 7. Low statistical power.
3. **No bear regime** — the real test (does Bearish-flow *protect* in a downturn?) can't be answered in this sample.

## What this project needs to do
1. **Break the confound with more history.** Our 281-day labeled set is fixed (the newsletter only goes back so far in our folder), so the key move is to test the *mechanism* against a longer multi-regime SPX history pulled from **Tiingo / IBKR** — i.e., reconstruct or proxy a flow signal and test it across 2015–2025 incl. real bear markets. (If the flow model can't be reproduced, at minimum stress-test the *relationship* the labels imply.)
2. **Risk-adjust.** Replace raw forward returns with excess-over-drift / Sharpe / hit-rate-vs-baseline so the bull drift is removed.
3. **Condition properly.** Test flow_risk *incremental* to gamma (2-way), and at the horizon that fits a ~multi-day-persistent signal (5–20d), not next-day.
4. **Find the regimes where it pays.** The goal isn't a yes/no — it's "in what conditions does the flow call add edge, and how would that change buy/sell/hedge sizing?"
5. **Decide & document** keep-vs-drop, and if keep, the exact rule for using it.

## Data provided (in this folder)
- **`_msr_flow_research.csv`** — 281 daily rows: `date, spx_last, spx_gamma_state, spx_above_flip, gex_throttle, regime_flow_risk, regime_strategic, regime_pvband_rr`, plus precomputed `fwd_ret_{1,5,10,20}d` and `fwd_absmove_1d`. Ready to analyze.
- **`_msr_methodology_spec.md`** — full feature definitions, reproducibility tiers, validated relationships.
- **`msr.db`** — the full database if deeper joins are needed.
- External: Tiingo (price history) + IBKR (chains) are available for extending the test window.

## Where to run this
Start it in a **fresh session** (clean context budget — this turned into a long thread). Bring three things: this brief, `_msr_flow_research.csv`, and `_msr_methodology_spec.md`. Then paste the kickoff prompt below.

## Kickoff prompt (paste into the new session)
> I'm researching whether a market-newsletter's proprietary "Systematic Flow Risk" signal has real predictive edge. I have a 281-row daily dataset (`_msr_flow_research.csv`) with three regime-label columns (`regime_flow_risk`, `regime_strategic`, `regime_pvband_rr`), reproducible context (`spx_gamma_state`, `spx_above_flip`, `gex_throttle`), and precomputed forward SPX returns (1/5/10/20-day). A first pass suggested `regime_flow_risk` separates forward returns and adds edge beyond the gamma signal — BUT the sample is one bull-market regime (+23%), only 38 flow episodes, and no bear market, so it's drift-confounded and unproven. I also have Tiingo (price history) and a live IBKR connection for pulling longer/multi-regime data. Help me design and run a rigorous test: risk-adjusted (remove the bull drift), incremental-to-gamma, at multi-day horizons, and ideally stress-tested against a longer SPX history including bear markets. The deliverable is a keep-vs-drop decision on the newsletter's flow signal, and if "keep," the exact rule for how it should size or gate a momentum book. Start by reviewing the brief in `_msr_flow_project_brief.md` and proposing a test plan.
