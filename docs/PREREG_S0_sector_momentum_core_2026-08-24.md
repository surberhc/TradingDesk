# PRE-REGISTRATION - S0: can a SECTOR-BUILT equity sleeve with 12-1 momentum beat the broad-beta sleeve, or is it the July drag in new clothes?

**Registered:** 2026-08-24 (written and committed BEFORE any run - the timestamp is the point).
**Author:** desk research (Claude), on Andrew's instruction ("draft the prereg with the ceiling check first", 2026-08-24).
**Source proposal:** Andrew's "Sector Momentum Core Strategy" memo, emailed 2026-08-24 (25 sections). This prereg adapts that memo to S0's chassis and to what our data can actually support; every departure from the memo is named in section 8.
**Status at registration:** hypothesis only.

> **OUTCOME (stamped 2026-08-24, after the run). PHASE 2 REFUTED - "July stands."**
> The decisive gate ("beats the NEUTRAL-ONLY arm") FAILED in all nine cells and both OOS
> halves: neutral-only 8.49% CAGR vs overlay cells 8.10%-8.30% (-0.19 to -0.39 pts), monotone
> in both swept dimensions. Worse than the Amendment-1 random control (random 5-of-11:
> 8.51% mean, stdev 0.21, +0.02 pts) - the composite selects sectors slightly worse than a
> coin flip. The 12-1 skip month, the drifting neutral, the strategic-weight sizing and the
> position caps - all four pre-registered differences from July - changed nothing, which
> establishes that July was not an artifact of its momentum window.
> **What SURVIVES: the NEUTRAL-ONLY arm** (Phase 1), performance-neutral vs baseline and
> delivering the 11-holding book. Full result:
> `backtester/output/S0_sector_momentum_phase2_2026-08-24.md`. Phase 0 is a KILL GATE - it can end this study in a single cheap run, before any signal is built.

> Why this prereg exists. A closely-related study already REFUTED sector/size tilting in S0 (PREREG_S0_equity_sleeve_broadening_2026-07-20.md; report backtester/output/S0_equity_sleeve_broadening_2026-07-20.md). Re-testing a refuted idea is only legitimate if we name IN ADVANCE what is materially different and what would have to be true for the answer to change. Section 2 does that. If the new elements do not carry the result, the honest verdict is "July stands" and we say so.

## 0. The question in one line
S0's equity sleeve is broad beta (as of 2026-08-24: SPY 2/3 + RSP 1/3). Does rebuilding that sleeve out of the 11 Select Sector SPDRs - a drifting strategic neutral plus a 12-1-momentum tactical overlay - improve the TOTAL PORTFOLIO enough to justify going from 2 funds to 11?

## 1. What is already established (do not re-litigate)

From the 2026-07-20 study, on the full S0 chassis:
- A momentum-gated sector+size tilt of 10/20/30% of the sleeve was a consistent DRAG in all nine TILT_PCT x N cells, in BOTH OOS halves, under BOTH fund pairs.
- Bull-regime return was LOWER IN EVERY CELL (-0.8 to -4.7 pts) - the thesis failed on its own turf.
- CAPM alpha CIs spanned zero everywhere, and alpha FELL as the tilt grew. Realized beta was flat, so there was not even extra beta to attribute.
- Diagnosed mechanism: 2015-2026 was an abnormally NARROW, mega-cap-led bull; cap-weight beat breadth.

From structural analysis run 2026-08-24 (pre-registration work, recorded here so it cannot later be re-framed as a result):
- The 11 sectors replicate SPY at R2 0.991 (TE 1.83%/yr) but RSP at only R2 0.977 (TE 2.99%/yr), and the RSP best-fit weights are distorted (XLI 22%, XLF 17%, XLB 12%). Sector ETFs are CAP-WEIGHTED WITHIN SECTOR, so they can reproduce sector concentration but NOT the within-sector mega-cap de-concentration that is objective #2 of the memo. RSP is the only instrument in the lineup that delivers that.
- The memo's static neutral table tracks the CURRENT S0 sleeve at corr 0.9960, TE 1.72%/yr - functionally the same exposure via 11 funds instead of 2.
- The memo's static table CARRIES LOOK-AHEAD. Its 27.4% XLK weight is an Aug-2026 figure; backtested statically from 2018-07 it returns 15.50% CAGR, but sleeve CAGR is MONOTONIC in the XLK weight (20.0% -> 14.59%, 24.0% -> 15.08%, 27.4% -> 15.50%, 31.0% -> 15.93%). At the ~20% weight contemporaneous in mid-2018 it returns BELOW plain SPY. The memo's own section 19 forbids this; its section 3 quarterly-recalculation rule is the fix, and this study MUST use the drifting neutral, never the static table.

## 2. What is materially NEW versus July (the only grounds for re-testing)

1. 12-1 RELATIVE MOMENTUM WITH THE SKIP MONTH (memo section 6). July used mean(RS_3m, RS_6m) - no skip month, shorter horizons, arguably inside short-term-reversal territory. 12-1 is the best-documented cross-sectional momentum signal in the literature and HAS NEVER BEEN TESTED HERE. This is the headline difference and the strongest reason the answer could change.
2. A DRIFTING STRATEGIC NEUTRAL, recalculated quarterly from then-current SPY/RSP sector weights (memo section 3). July had no strategic sector core at all - it tilted AWAY from broad beta. A neutral that evolves with the market is both a genuine design improvement and the anti-look-ahead fix for section 1's finding.
3. STRATEGIC-WEIGHT-SCALED TACTICAL SIZING with position caps (memo sections 13-14): tactical_factor = strategic_weight x composite, capped at the lesser of +10 pts of portfolio or +50% of the sector's strategic weight. July used flat TILT_PCT / N sizing, which let small sectors take outsized positions.
4. A PERMANENTLY-DIVERSIFIED CORE (memo section 4): 70% of the sleeve always sits at strategic weights, so no sector is ever fully exited. July's construction could vacate broad beta entirely.

If Phase 2 shows the result is insensitive to (1)-(4), the finding is "July stands" and this study closes.

## 3. PHASE 0 - THE CEILING CHECK (kill gate; run FIRST, build nothing until it passes)

BOUND THE PRIZE BEFORE ENGINEERING ANYTHING. S0's realized portfolio beta is ~0.22 and the equity sleeve is regime-throttled, so even a flawless sector overlay may move the total portfolio by a rounding error. That is the exact caveat that shelved the breadth study (PREREG_S0_breadth_leadership_regime_gate_2026-07-20.md section 1c).

- Construction: a PERFECT-HINDSIGHT sector oracle - each month, hold the sector ETFs that will be the top performers over the FOLLOWING month. Look-ahead is allowed ON PURPOSE; this is an upper bound, never a strategy.
- Run: full S0 chassis, Growth + Balanced, 2018-07 -> present (the all-11-sectors window), reporting the gain at the TOTAL PORTFOLIO level, not the sleeve level.
- PRE-COMMITTED KILL THRESHOLD: if the cheating oracle adds < 75 bps/yr of total-portfolio CAGR, we STOP and report "the prize is too small to chase." No signal is built, no data is purchased. (75 bps chosen as ~10x the +5 bps the entire VTI de-duplication was worth, and well above backtest noise; it is a floor on being WORTH THE COMPLEXITY, not a performance target.)
- A realistic non-cheating signal captures only a fraction of an oracle. If the oracle itself is thin, nothing downstream can rescue it.

## 4. Chassis - what changes and what is frozen

Only the INTERNAL COMPOSITION OF THE EQUITY SLEEVE changes. The Regime Engine (equity band), Duration engine, re-entry ladder, defensive sleeve and real-asset sleeve are FROZEN per CLAUDE.md rule #1. Production config stays byte-for-byte unchanged: the study lives behind runtime-only knobs, default OFF, exactly as the July study did.

- Plug-in point: parts/sector.py already owns "how the equity sleeve splits internally" and already receives the full sector universe. No architectural change is required.
- Compatibility with the memo's section 15: the memo explicitly declines to decide whether equities should be owned at all - that stays S0's regime engine's job. The separation is clean and is preserved.

## 5. The signal (pre-specified, SIMPLE, no tuning)

Composite score per sector, monthly, from memo section 5 - REWEIGHTED because breadth is not computable (see section 7):

| Metric | Memo weight | THIS STUDY | Note |
|---|---|---|---|
| 12-1 relative momentum | 35% | 39% | the headline new signal |
| 6-1 relative momentum | 25% | 28% | responsiveness |
| 6m risk-adjusted momentum | 15% | 17% | return / realized vol |
| Absolute trend (10m MA; 50/200 cross) | 15% | 16% | memo section 9 scoring, unchanged |
| Sector breadth | 10% | 0% - DROPPED | no constituent data (section 7) |

Breadth's 10% is redistributed PRO RATA across the surviving four - not reallocated by judgment, so no new free parameter is introduced.

- Eligibility (memo section 12), unchanged: top-5 composite AND above the 10-month MA.
- Sizing (memo sections 13-14), unchanged: strategic_weight x composite, normalized across eligible sectors, capped at min(+10 pts, +50% of strategic weight); unused tactical budget returns pro rata to the strategic core (memo section 15).
- SWEPT, for a PLATEAU not a peak: core/tactical split in {80/20, 70/30, 60/40} (the memo's own three risk levels). NO OTHER KNOB IS SWEPT. Momentum horizons, MA lengths and caps are taken from the memo as-is precisely because they are the conventional, economically-defensible values (memo section 24).

## 6. Test arms

1. BASELINE: current production S0 (SPY 2/3 + RSP 1/3).
2. NEUTRAL-ONLY (THE DECISIVE CONTROL): the drifting strategic sector neutral with NO momentum overlay. This isolates repackaging from selection. If the overlay does not beat this arm, the momentum model adds nothing and the exercise reduces to holding 11 funds instead of 2.
3. FULL: neutral + momentum overlay, across the core/tactical sweep.
4. BETA-MATCHED BROAD-BETA: as July - scale baseline equity to each arm's realized beta and compare, separating skill from beta. CAPM regression on SPY with block-bootstrap 95% CI on annualized alpha (block ~20d, >=2000 resamples, seed np.random.default_rng(20260824)).
5. The memo's own benchmarks (section 22): static SPY, static RSP, static 50/50 - reported for context, at the sleeve level.

Regime split (bull/expansion vs defensive/crisis, by the existing Market Health Score band) reported for every arm, as July did.

## 7. Known blockers - stated up front, not discovered later

- SECTOR BREADTH (memo section 10) IS NOT COMPUTABLE. The warehouse is ETF-only; we have no constituent-level "% above 200d MA". This is the same wall that shelved the breadth prereg on 2026-07-20. It is DROPPED (section 5), not proxied - a proxy-on-proxy result would be uninterpretable. Buying constituent data is a separate decision, not a silent scope expansion.
- HISTORY IS SHORT. XLC begins 2018-06-19, XLRE 2015-10-08. An honest 11-sector test starts 2018-07 - losing the 2015-08 and 2018-Q4 episodes and ~30% of S0's chassis window, leaving COVID-2020 and the 2022 bear as the only stress episodes. This materially weakens any OOS claim and is a standing reason to distrust a marginal positive.
- TURNOVER AND COST. 11 positions with a monthly overlay, in accounts that are often small. The memo's 1-pt no-trade band (section 18) is included, and cost/slippage is charged on the same basis as the July study. Whole-share feasibility in sub-$25k accounts is a real constraint (see the "Growth (Small)" tier) and is reported, not assumed away.

## 8. Departures from the memo (deliberate, listed)

- Breadth dropped (section 7).
- Strategic weights must be DRIFTING/QUARTERLY, never the static Aug-2026 table (section 1).
- The memo's section 16 market-risk overlay is OUT OF SCOPE - S0's regime engine already owns that decision and is frozen.
- Testing starts 2018-07, not the memo's implied longer history (section 7).
- Earnings-revision breadth (memo section 21) is out of scope, as the memo itself proposes.

## 9. Discipline and hard gates

A. HARD GATES - these decide whether a result is REAL (non-negotiable, CLAUDE.md rule #1).
- Phase 0 ceiling >= 75 bps/yr total-portfolio CAGR, or the study stops (section 3).
- BEATS THE NEUTRAL-ONLY ARM. If not, there is no selection skill and the answer is "repackaging."
- OOS survival: the benefit appears in BOTH halves of the (already short) window - split 2018-07 -> 2022-12 and 2023-01 -> present.
- PLATEAU: a contiguous region of the core/tactical sweep behaves consistently. One lucky cell = curve-fit = dead.

### AMENDMENT 1 - significance bar for Phase 2 (added 2026-08-24, AFTER Phase 0, BEFORE Phase 1/2 is built)

Phase 0 measured the noise floor of NO-SKILL sector selection: random 5-of-11 picks produced a
standard deviation of 84 bps/yr of total-portfolio CAGR across seeds (range 7.56%-9.79%). The
75 bps Phase 0 kill threshold therefore sits BELOW that noise floor. It remains valid for what it
gated - a CEILING of 1,877 bps is far outside any noise band - but it is NOT a usable
significance bar for a realistic signal.

Amended, pre-committed BEFORE any Phase 2 number exists:
- A Phase 2 point estimate is judged against the RANDOM-SELECTION distribution, not against
  baseline. The random arm (>= 20 seeds) is run as a permanent control arm and its spread is
  reported alongside every result.
- The block-bootstrap 95% CI on alpha (prereg section 6, arm 4) must EXCLUDE ZERO. A point
  estimate inside the random arm's +/-1 sigma band is reported as "indistinguishable from luck",
  regardless of sign.
- The NEUTRAL-ONLY arm is expected to land near baseline (Phase 0 measured +15 bps for
  equal-weight-all-11). Beating it by less than the random spread is NOT evidence of skill.

This amendment RAISES the bar. It is recorded here rather than applied silently, and it was
written before Phase 1 was built.

### AMENDMENT 2 - tactical allocation mechanics (added 2026-08-24, AFTER Phase 1, BEFORE any Phase 2 result)

Building the overlay exposed an internal contradiction between the memo's own rules. Recorded
and fixed BEFORE any performance number exists, so this is specification, not tuning.

THE PROBLEM (measured on 2026-08-21 weights, 70/30 split). The section 14 cap is the LESSER of
+10 pts and +50% of strategic weight. The 50% leg binds on 4 of the 5 eligible sectors, so only
25.2 of the 30 tactical points can be placed. Section 15 then returns the stranded 4.8 pts PRO
RATA across all eleven sectors - which sends 2.03 pts to the SIX sectors that just FAILED the
eligibility screen (XLY scored 8/100 and received tactical money), and hands the largest single
share back to XLK, the biggest strategic weight. Sections 14 and 15 together therefore work
AGAINST the memo's own objective #2, reducing mega-cap dependence.

FIX 1 - CASCADE (structural; no cap raised, no parameter added). Unplaced tactical budget is
RE-OFFERED to the eligible sectors that still have cap room, iteratively, until either the
budget is placed or every eligible sector is at its cap. Only the true residual returns to the
strategic core per section 15. Tactical money stays inside sectors that passed BOTH tests.

FIX 2 - SWEEP THE MULTIPLIER, DO NOT PICK IT. SECTOR_TACTICAL_MAX_ADD_MULT joins the sweep at
{0.50, 0.75, 1.00} and must show a PLATEAU. Raising it to 1.00 because XLE looked constrained
on one date would be exactly the curve-fitting rule #1 forbids. The +10 pt ABSOLUTE ceiling is
NOT swept - it stays fixed as the hard concentration guard, which Phase 0's -46.91% anti-oracle
drawdown says not to loosen.

Sweep grid becomes 3 core splits x 3 multipliers = 9 cells. The plateau gate applies across
BOTH dimensions.

OBSERVED SIDE EFFECT, recorded now so it is not mistaken for a finding later: with the cascade,
a HIGHER multiplier REDUCES mega-cap concentration (XLK 30.5% at mult 0.50 -> 29.2% at 0.75 and
1.00), because budget that would have leaked back to strategic weights instead reaches the
smaller eligible sectors. This is mechanical, not evidence of edge.

B. THE ADOPT DECISION - a return-vs-drawdown TRADE-OFF, Andrew's call on balance. Per his standing instruction (2026-07-20): no hard drawdown floor or ceiling. Report the bull-regime return gain side by side with each episode's drawdown change - the actual exchange rate - plus the operational cost (11 funds vs 2, turnover, small-account feasibility). Beta attribution informs this decision; it does not gate it.

Valid honest outcomes, none hidden:
- "The ceiling is too thin - stop."
- "Repackaging only; the neutral arm explains it all."
- "12-1 genuinely changes the July answer" (the interesting win).
- "July stands."
- "A benefit exists but the 2018+ window is too short to trust it."

Any ADOPT verdict ships DEFAULT-OFF until Andrew explicitly arms it. The review -> arm gate is unchanged.

## 10. Deliverables

- PHASE 0 CEILING-CHECK REPORT FIRST, as a standalone go/no-go. Nothing else is built until it clears.
- If it clears: study-only knobs (SECTOR_CORE_*) defaulting OFF; drifting-neutral construction from SPY/RSP sector weights; the four-metric composite in parts/sector.py behind the study flag.
- Report backtester/output/S0_sector_momentum_core_2026-08-24.md: lead with the NEUTRAL-VS-OVERLAY VERDICT, then the sweep grid, the five arms, regime split, OOS halves, per-episode drawdowns, turnover/cost, and the gate walk with numbers.
- Tests: causality / no-look-ahead on the composite and on the quarterly neutral rebuild; a synthetic no-skill series must be reported as no-skill.

## 11. Related record

- PREREG_S0_equity_sleeve_broadening_2026-07-20.md - the refuted predecessor.
- backtester/output/S0_equity_sleeve_broadening_2026-07-20.md - its result report.
- PREREG_S0_breadth_leadership_regime_gate_2026-07-20.md - SHELVED (no constituent breadth data); the source of the "size the prize first" discipline used in section 3.
- Equity-sleeve de-duplication (VTI removed, SPY 2/3 + RSP 1/3) landed 2026-08-24, separately from this study. Full-chassis Growth 2015-02 -> 2026-08: CAGR 7.68% -> 7.73%, Sharpe 0.927 -> 0.937, maxDD -10.12% -> -10.08%. Backtester suite 444 passed (unchanged).
