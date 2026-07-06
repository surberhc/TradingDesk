# PRE-REGISTRATION ADDENDUM — Short strangle with a REGIME GATE

**Registered:** 2026-07-06 (committed BEFORE the gated run). Amends PREREG_short_strangle_alpha_2026-07-06.md.
**Author:** desk research (Claude), on Andrew's explicit instruction (2026-07-06): premium-selling must be
tested in its DEPLOYED role — gated by regime — not always-on. Gate design "layer all three," Andrew's call.
**Status:** hypothesis only. The un-gated strangle (a WASH: alpha −0.83%, CI spans 0) is the regime-OFF CONTROL.

> Judging an always-on naked short-vol book through 2020 COVID is a strawman (CLAUDE.md: test a strategy in
> the role it is actually used). Mechanical premium-selling is deployed WITH a "when to be on" gate. This
> addendum conditions entry on regime and judges the strategy in that gated role. THE ANTI-CURVE-FIT
> GUARDRAIL IS NON-NEGOTIABLE: a gate that merely reduces exposure is not an edge. Every gate — each of the
> three individually AND the composite — must BEAT A RANDOM GATE WITH THE SAME DUTY CYCLE (same number of
> on-weeks, chosen at random) on both alpha and total P&L. If it does not beat the random-duty-cycle placebo,
> the "regime filter" adds nothing beyond trading less, and the strategy is REFUTED even gated. This is the
> exact placebo that killed the re-entry ladder (memory regime-engine-tuning). We do NOT hand-pick a gate
> threshold to rescue the result; thresholds are swept for a PLATEAU.

## 1. The three gates (layered — on only when ALL say "on")
Applied CAUSALLY at ENTRY only (a new weekly strangle opens only if the gate is "on" that week, using data
through the entry day). Positions already open are managed to their normal exit regardless (deployed reality).
1. **Existing frozen regime engine** — reuse S0's blessed risk-on/off (trend) state AS-IS, zero new knobs.
   On = risk-on (not in a confirmed downtrend). No tuning of the regime engine whatsoever.
2. **VIX term structure** — on = CONTANGO (VIX < VIX3M, calm); stand down in backwardation (stress).
   One mechanism-justified rule, no free threshold (contango is the natural 1.0 cutoff); a small sweep of the
   VIX/VIX3M ratio cutoff {1.00, 0.98, 0.95} reported for plateau, headline at 1.00.
3. **IV-rank richness** — on = IVR ≥ threshold, IVR = trailing-252-trading-day percentile of the VIX close.
   Threshold SWEPT {0, 25, 50, 75}; verdict requires a PLATEAU across it, never a single hand-picked level.

**Composite gate (headline):** on = regime risk-on AND contango AND IVR ≥ 50. Reported alongside each gate
used ALONE (so we can see which layer, if any, carries value and whether layering helps or just thins sample).

## 2. Chassis
Unchanged from the base strangle prereg (16/20-delta shorts, DTE {30,45}, uncapped-intrinsic settle, weekly
ladder, honest fill band {0,0.25,0.50=headline,1.0}, 2020-2021 clean-delta path, blackout skip). Management:
report BOTH hold-to-expiry (headline — it dominated managed in the control) and managed(50%/21DTE) for
completeness. Reserved capital = put_strike×100 (basis-invariant for Sharpe/beta/alpha-sign).

## 3. The placebo (mandatory, decisive)
For each gate (three solo + composite), with its realized DUTY CYCLE d (= on-weeks / total-weeks): draw ≥500
RANDOM gates that turn on a random d-fraction of weeks (fixed seed np.random.default_rng(20260706)), run the
strangle book under each, and build the null distribution of alpha AND total P&L. The regime gate must exceed
the **97.5th percentile** of its random-duty-cycle null on total P&L (and have positive alpha CI) to count as
a real regime edge. Report the gate's percentile rank within its null.

## 4. Sample-power guard (honest reporting)
Report the ON-WEEK COUNT (number of entries actually taken) for every gate. The composite is selective; if it
fires on fewer than ~50 entries the sample is too thin to trust and the result is reported as UNDERPOWERED /
inconclusive, NOT as a pass. State this explicitly; do not over-read a thin gated sample.

## 5. Discipline & pass criteria (judged in the DEPLOYED, gated role)
- **OOS split** 2018-06→2021-12 / 2022-01→2026-07: gated-on alpha positive in BOTH halves.
- **Plateau** across delta {16,20} × DTE {30,45} × IVR {0,25,50,75} — not one cell.
- **Deployed-role judging:** the gate is ALLOWED to stand down in crashes; we do NOT fail it for regimes it
  gates off. The target is positive, placebo-beating alpha in the on-regime.
PASS requires ALL: (1) gated-on annualized alpha > 0 with bootstrap 95% CI excluding 0, across the mid→0.50
fill band; (2) the gate BEATS its random-duty-cycle placebo (>97.5th pct on total P&L) — for the composite AND
for at least the frozen-regime-engine gate solo; (3) adequate sample (≥~50 on-weeks); (4) OOS both halves;
(5) plateau across the grid. Fail any → REFUTED: "regime timing adds nothing beyond trading less; mechanical
SPX premium-selling shows no clean VRP alpha across condor + CSP + strangle, gated or not." A clean refutation
is a full, valid, headline result. A robust PASS graduates the gated strangle to a deployment study + anchors
the diversified premium suite once the snapshot download lands.
