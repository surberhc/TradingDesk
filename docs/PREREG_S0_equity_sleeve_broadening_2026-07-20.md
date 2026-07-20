# PRE-REGISTRATION — S0: does BROADENING the equity sleeve (size + sector tilt) beat broad-beta, or is it just more equity beta?

**Registered:** 2026-07-20 (written and committed BEFORE any run — the timestamp is the point).
**Author:** desk research (Claude), on Andrew's explicit instruction ("write it up as the pre-registration for sign-off", 2026-07-20).
**Status at registration:** hypothesis only. No sector-tilt-ON or size-tilt backtest exists on the record — the S0 sector engine is built, unit-tested, and DEFAULT OFF (`SECTOR_TILT_PCT = 0.0`); no performance study has ever been run. This document defines the test and its pass/fail bar BEFORE any number is generated.

> **The whole point of this prereg.** Andrew's standing objection is that we "lock down a bunch of parameters, run one test, and come back saying it doesn't work." This document exists so the success bar, the sweep grid, and the honest-doubt null are all fixed in advance and judged on **net merit** (per CLAUDE.md's counterweight), not rigged to fail. A weakness disqualifies only if it outweighs the strengths.

## 0. The question in one line
S0's equity sleeve is today ~100% US large-cap: it equal-weights SPY + VTI + RSP (three flavors of the same bet). **Does carving out a static slice of that sleeve for stronger-momentum SIZE (small/mid) and SECTOR exposure improve the strategy — or does it merely add higher-beta equity risk we could get more cheaply by just holding more SPY?**

## 1. Hypothesis
Replacing a fixed fraction of the broad-beta equity sleeve with a momentum-gated tilt into **small/mid-cap** and **leading sectors** delivers **higher return in bull/expansion regimes** (Andrew's thesis: leadership and the size premium show up in the bulls) **without materially worsening the crisis-episode drawdowns** (the regime engine already cuts total equity in bad regimes, so the tilt shrinks with it) **and beyond what simply holding more broad-beta would deliver** (i.e. the gain is selection, not leverage-in-disguise).

**Honest doubt (the null we are actively trying to confirm).** The tilt's extra return is **fully explained by higher equity beta** — small caps and hot sectors are higher-beta, so of course they earn more in a bull market. A broad-beta S0 scaled to the *same realized beta* matches or beats the broadened version on risk-adjusted terms, AND the tilt worsens the 2020/2022 drawdowns because leadership sectors lead the *next* drawdown and small caps fall hardest. In that case the honest verdict is **"just more beta — buy more SPY instead,"** and it is reported as such, not quietly re-specified until it looks like edge.

## 2. Chassis — HOW the tilt goes in (frozen for this study)
Only the **internal composition of the equity sleeve** changes. Everything else in S0 is untouched: the Regime Engine (equity band), Duration engine, re-entry ladder, defensive sleeve, real-asset sleeve, all frozen per CLAUDE.md rule #1. Production config is byte-for-byte unchanged — the tilt lives behind a study-only flag, default OFF.

- **New study universe (equity-tilt candidates):** the 11 SPDR sectors (already in `config.SECTORS`) **+ two size funds: IJH (S&P MidCap 400) and IJR (S&P SmallCap 600).** IJH/IJR chosen to extend the *same* index family down the cap scale (GICS-consistent with the SPDR sectors); **VO / VB (Vanguard mid/small) pre-registered as the alternate pair** for a robustness re-run. Both size funds have history to 2000, so the equity side is testable back through 2008. Andrew's only fund constraint (2026-07-20): quality, liquid, cheap — IJH/IJR (0.05/0.06% ER) and VO/VB (0.04/0.05% ER) all qualify.
- **The split (the one genuinely new knob):** `EQUITY_TILT_PCT` = static fraction of the equity sleeve made available to the tilt. The remaining `(1 − TILT_PCT)` stays broad-beta (SPY/VTI/RSP as today). **Swept `{0.00 (baseline), 0.10, 0.20, 0.30}`.** 0.30 is the ceiling, matching the existing sector-engine clamp. No value is tuned to maximize anything; the verdict rests on a plateau (§4), not a peak.
- **Gate — reuse the blessed metric, do NOT invent a new one.** Candidates eligible for the tilt = those **above their 200-day trend AND beating SPY on relative strength** (score = mean(RS_3m, RS_6m), RS_k = ret(asset,k) − ret(SPY,k)) — the exact simple basis the sector engine already uses (`parts/sector.py`, `SPEC.md §5`: "SIMPLE basis only… No 8-factor score"). Size funds are gated on the identical basis. Pick the top **N ∈ {3, 4, 5}** eligible; weight each `min(TILT_PCT / N, SECTOR_MAX_WEIGHT=0.15)`; any unfilled tilt budget (too few eligible) falls back to broad-beta. **No new bespoke "should we diversify now" indicator is added** — "when" is already answered by (a) this momentum gate and (b) the regime engine shrinking the whole sleeve in bad regimes.
- **Everything downstream unchanged:** the tilt output feeds the existing portfolio assembler exactly where the sector engine already plugs in; sizing, reserve/band logic, execution lag all frozen.

## 3. The test — three arms that separate SKILL from BETA (the headline)
1. **BASELINE (control):** current S0, `EQUITY_TILT_PCT = 0` — broad-beta equity sleeve. Full metrics.
2. **BROADENED:** S0 with the tilt on, across the `TILT_PCT × N` grid.
3. **BETA-MATCHED BROAD-BETA (the killer control):** for each broadened variant, measure its realized equity beta vs SPY, then build a *broad-beta-only* S0 whose equity weight is scaled to the **same realized beta**, and compare. **This is the decisive arm:** if the broadened variant does NOT beat the beta-matched broad-beta portfolio on risk-adjusted terms (Sharpe / Calmar), its extra return was just beta and the answer is "buy more SPY." Cross-checked with a regression of each variant's daily return on SPY (report alpha, beta, R², and a block-bootstrap 95% CI on alpha; block ≈ 20d, ≥2000 resamples, fixed seed `np.random.default_rng(20260720)`).

**Regime split — test it in the role it's actually used (Andrew's lens).** Partition the timeline into **expansion/bull** vs **defensive/crisis** regimes (by the existing Market Health Score band). Report the tilt's contribution separately in each bucket. His thesis lives in the bulls; the mandate's floor lives in the crises. Both are reported; neither is hidden.

## 4. Discipline
- **OOS split:** train 2015-01 → 2020-12, test 2021-01 → present. The benefit must appear in **both** halves — not carried by one regime.
- **Per-episode drawdowns (the floor):** 2015-08, 2018-Q4, 2020-02→04 (COVID), 2022 (bear) reported separately, each on the full S0 chassis (all these funds are in-universe from 2015). **2008 GFC:** reported as a best-effort *equity-sleeve-only* sub-study (size/sector funds exist to 2000, but S0's defensive/real-asset sleeves are inception-thin pre-2015, so a full-chassis 2008 run is directional, not authoritative — stated as such).
- **Plateau, not peak:** the verdict rests on a **contiguous region** of `TILT_PCT × N` behaving consistently, plus survival of the IJH/IJR → VO/VB swap. A benefit that appears in one lucky cell and vanishes next to it is curve-fit and is REFUTED.
- **Frozen everything-else:** no regime/defensive/real-asset/duration knob is touched. Warehouse read-only. Reuses the backtester's own `run_backtest()` (structural parity — paperbot cannot drift).

## 5. Pass / adopt criteria — net-merit, NOT a checklist (signed off by Andrew 2026-07-20)
Two kinds of criteria, and they are deliberately NOT the same thing. Andrew's standing instruction: do not turn this into rigid if-then floors that let us "shoot ourselves in the foot" by rejecting a great return over a couple of drawdown points.

**A. HARD gates — these decide whether the result is REAL (non-negotiable, CLAUDE.md rule #1).** Not goalposts: a result that fails these isn't a worse trade-off, it's a curve-fit mirage.
- **OOS survival:** the benefit appears in BOTH train (2015→2020) and test (2021→present) halves — not carried by one regime.
- **Plateau + fund-swap:** it holds across a contiguous `TILT_PCT × N` region and survives IJH/IJR → VO/VB. One lucky cell = curve-fit = dead.

**B. The adopt decision — a pure return↔drawdown TRADE-OFF, Andrew's call on balance. NO hard drawdown floor, NO drawdown ceiling.** (Andrew, 2026-07-20: "these can't be hard fast rules, it's if-then"; and on a fixed floor: "what if we're 10 points better on return? wouldn't that be shooting ourselves in the foot?" — he selected *no hard ceiling, pure trade-off*.)
- **The trade-off surface (the deliverable that matters):** at every grid cell, report the bull-regime return gain SIDE BY SIDE with the change in each crisis-episode drawdown — the actual exchange rate. Deeper drawdown is fully acceptable if the return more than pays for it (e.g. +10% bull return for +3 pts drawdown may be an easy yes; +1% for +5 pts an easy no). Andrew decides case by case; there is no auto-fail on drawdown at any level.
- **Beta attribution — informs the trade-off, does NOT gate it.** The beta-matched broad-beta arm answers exactly one question: *"could we get this same return more cheaply by just holding more SPY, at a BETTER drawdown?"* If plain-more-SPY dominates the tilt (same/again return, less drawdown), the honest read is "buy SPY." If the tilt delivers return that SPY-leverage cannot, it is real selection. This shapes the decision; it is not a pass/fail hurdle.

**Valid, honest headline outcomes (any is a full finding, none is hidden):**
- "Broadening is just higher equity beta — plain-more-SPY dominates it" (beta attribution says buy SPY).
- "Adds real bull return the index can't replicate, at a drawdown cost Andrew judges worth it" — the clean win.
- "Adds bull return but the drawdown cost isn't worth it *to Andrew* on balance" — a legitimate no, decided on merit, not by a floor.
- "Benefit is in-sample / one-cell only — curve-fit" (fails a HARD gate; dead regardless of how good it looked).
- Any ADOPT verdict still ships **default-OFF** until Andrew explicitly arms it (review → arm gate unchanged), and graduates to a pre-registered *deployment* study first.

## 6. Deliverables
- New study-only param `EQUITY_TILT_PCT` (default `0.0`) + size funds behind a study flag so **frozen production S0 is unchanged**. Engine: extend `strategies/parts/sector.py` (or a sibling `parts/equity_tilt.py`) to include the two size funds on the same RS/trend gate; reuse existing gate helpers.
- Beta-matched-broad-beta arm + SPY regression/bootstrap harness in the backtester (no tuning to data).
- Report `backtester/output/S0_equity_sleeve_broadening_2026-07-20.md`: **LEAD WITH THE SKILL-VS-BETA VERDICT**, then the `TILT_PCT × N` grid (return/Sharpe/Calmar/maxDD per cell), the three-arm comparison, the bull-vs-crisis regime split, OOS halves, per-episode drawdowns, and the 5-criteria PASS/FAIL walk with numbers.
- Tests: causal / no-look-ahead on the tilt selection; a synthetic-data sanity check that a *higher-beta-but-no-skill* series is correctly reported as "just beta" (alpha ≈ 0) by the beta-matched arm. Frozen config untouched, warehouse read-only.
