# Pre-registration — Regime-Conditioned Profit-Target Modulation on the Managed 0DTE Iron Condor

**Registered:** 2026-07-03, BEFORE seeing any results. The base managed-0DTE-condor terrain map is still running; this hypothesis and its pass/fail criteria are committed here first so they cannot be retrofitted to the data.

## Mechanism (stated before the sweep)
The Tier-1 / gamma signal's one validated skill is predicting next-day realized volatility (gamma_state -> next-day RV is strong and monotonic; expected-move bands ~86% calibrated). It has NO directional or standalone-P&L edge — that has been refuted many ways. The mechanistically-correct use is as a MODULATOR of an existing strategy's risk-taking, not as an edge of its own. Hypothesis: on days the signal forecasts high realized vol / negative dealer gamma, the 0DTE iron condor's intraday-gamma tail is fattest, so taking profit EARLY protects the book; on calm / positive-gamma days, letting the trade run harvests more front-loaded theta at low tail risk. Therefore conditioning the PROFIT-TARGET level on the pre-entry vol forecast should improve risk-adjusted return versus a single static target.

## Precondition (gate on running this test at all)
The base managed 0DTE condor must first show a REAL, robust base edge: a positive net-of-honest-cost plateau that beats the random-exit-matched-holding placebo. If the base has NO edge, this modulation is NOT run as a rescue. A dynamic overlay that "rescues" an edgeless base is treated as presumptively curve-fit and reported as such.

## The single pre-committed rule (one signal, one modulated knob)
- Signal: the day's expected-realized-vol / gamma-state forecast, known PRE-ENTRY (as-of prior close / that morning). Use a CLEAN vol source — the VIX / VIX9D family or gamma_state — and explicitly NOT the warehouse `expected_move_pct` column, which is corrupt for 2020-2021 (see the IV-corruption finding).
- Rank trading days into terciles by that forecast.
- Top third (twitchy / high-vol forecast / negative gamma): TIGHT profit target ~25% of credit (take fast, exit early).
- Bottom third (calm / low-vol forecast / positive gamma): LOOSE target ~50-75% of credit (let it run).
- Middle third: the static best target found in the base run.
- Everything else — entry chassis, short delta, honest 4-leg fills on entry and every exit, costs — held IDENTICAL to the base managed condor. Only the profit-target level is conditioned.

## Control
The static best-target managed condor from the base run (same average aggressiveness, no day-conditioning).

## Placebo (decisive pass/fail)
RANDOM-RELABEL placebo: shuffle which days are labeled "twitchy" vs "calm" (preserving tercile proportions) and re-run the conditional rule on the shuffled labels many times. The real-label result must beat the shuffled-label distribution at >= 95th percentile to claim the SIGNAL carries the value rather than the mere act of varying the target. If real labels do not beat shuffled labels, the vol signal added nothing and the result is a refutation.

## Additional discipline (all pre-committed)
- PLATEAU not peak: the tight and loose target levels must work as a BAND (tight in {20,25,30}%, loose in {50,60,75}%), not single lucky cells.
- OOS: observe on 2022-2024, confirm on 2024-2026; report both halves for every arm.
- PER-REGIME: break out by year (2022 bear vs calm stretches). Honest limit: 0DTE intraday data is 2022+, so the tail sample is small; crash-dependent results carry less weight than calm-regime results.
- HONEST COSTS: everything net of realistic 4-leg fills.
- DEGREES-OF-FREEDOM BUDGET: exactly ONE conditioning signal and ONE modulated knob (profit target). No stacking of size / strike / entry conditioning in this test. Any additional conditional is a SEPARATE future pre-registration.

## Success criteria (ALL must hold to claim an edge)
1. Base precondition met (base managed condor has a real edge).
2. Real-label conditional beats the static-target control on risk-adjusted terms (Sharpe / Sortino / max drawdown), net of honest costs.
3. Beats the random-relabel placebo (>= 95th percentile).
4. Survives OOS (holds on the 2024-2026 confirmation half).
5. Sits on a plateau, not a single cell.
Failing any one of these is recorded as a refutation — a valid and valuable outcome.

## Provenance
Follows the honest-testing playbook. Related findings: the gamma signal is awareness-not-alpha; the warehouse IV corruption (2020-2021); the S2/S3 intraday condor refuted four ways (static gating already dead — this is profit-target MODULATION, a different and untested knob).

## Addendum (2026-07-03) — Fill-model pre-commitment (recorded before the friendlier-fill results exist)

The base 0DTE managed-condor terrain map (running now) uses the engine's existing HONEST-FILL model: every leg crosses the full bid-ask spread on both entry and exit (sell-at-bid / buy-at-ask on the way in, buy-at-ask / sell-at-bid on the way out) — i.e. 100% of the quoted spread on all legs, the pessimistic bound. That over-models cost for a strategy actually executed as a single 4-leg SPX combo, which fills near NET MID because the market maker hedges the package as one unit.

Committed BEFORE seeing any friendlier-fill numbers:
- Execution is a REPORTED SENSITIVITY AXIS on the NET COMBO package, not one hard-coded assumption. Levels: net-mid (0% of net spread, optimistic bound) / 25% of net spread / 50% of net spread / 100% (worst-side every leg, the current run).
- HEADLINE = 50% of the net spread (conservative-but-fair). Mid and 25% reported around it; 100% retained as the worst-case bound.
- PASS RULE: a positive verdict must HOLD ACROSS THE net-mid -> 50% band. A result that survives only at pure mid is recorded as execution-fragile, NOT an edge.
- ANTI-CURVE-FIT-THE-COST-MODEL: the fill fraction is fixed by execution reality and chosen INDEPENDENTLY of the P&L. It is never selected because it makes the strategy profitable — doing so would be curve-fitting the cost assumption, the same sin as curve-fitting the strategy.
- The fill fraction is a REAL ENGINE PARAMETER that propagates through the profit-target management (more credit collected in + a cheaper close shifts when the 25/50/75% target is actually touched), NOT a post-hoc multiplier on final P&L.

This pre-commitment applies to the base 0DTE condor terrain map and to the regime-conditioned profit-target modulation described above.

---
