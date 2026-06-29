# Regime `sharp_recovery` refinement — ANTI-CURVE-FIT research test

*Generated 2026-06-29. RESEARCH ONLY — nothing adopted, no canonical config/data/strategy logic changed on disk. All tests run in-process with monkeypatches restored after each run. Strategy=Balanced, window 2007-01-01→2026-06, local `bt_data` (read-only).*

## VERDICT: **SHELVE.** Fails the per-episode safety gate (Gate 4) at **−118 bp on the GFC 2008-09 episode**, and that failure is **independent of the trigger refinement** — so it cannot be tuned away. The refinement is principled and works exactly as designed on its target case (2015-16), but the override's binding constraint is a genuine V it must not suppress.

The clean negative is the valuable result: **the re-entry MAX-LAG override cannot be safely sped up, even with a perfect clean-V filter.** Re-entry stays `MAX_LAG=6`. This closes the PRIME lead.

---

## 0. The reframing that drives everything (a key empirical finding)

Before designing any refinement I instrumented **when the `sharp_recovery` override actually fires** under the production config (`_sharp_recovery_diag.py`, walk verified byte-identical to `reentry.compute_ladder_stages`):

| `MAX_LAG` (months) | # override firings, 2007→2026 | firing dates |
|---:|---:|:--|
| **6 (PRODUCTION)** | **1** | 2019-10-31 |
| 5 | 1 | 2019-10-31 |
| 4 | 8 | 2009-07, 2010-12, 2012-03, **2016-05**, 2019-10, 2023-01, 2024-01, 2025-07 |
| 3 | 9 | 2009-06, 2010-12, 2012-02, **2016-04**, 2019-10, 2023-01, 2023-12, 2024-09, 2025-06 |

**At the production `MAX_LAG=6`, the override fires exactly ONCE in 20 years (2019-10-31), and that firing is itself a clean V** (+4.6% trailing 6m, +31% off the trough, off a −19.3% drawdown). The "fires in sideways grinds like 2015-16" behaviour the lead is premised on **does not exist at `MAX_LAG=6`** — it only appears once you *shorten* the lag to 3–4, which is what makes 2016-04/2016-05 fire.

**Consequence for the test design:** a trigger refinement that suppresses grind firings has *nothing to suppress* on the current config. The refinement is only meaningful as a **paired change**: shorten `MAX_LAG` (to actually get faster re-entry — the real goal) **and** add a clean-V filter (to kill the grind firings that shortening introduces). That is the only honest way to test it, and the prior `MAX_LAG 6→3` failure is exactly the right baseline to improve on. So the test is: *can the V-filter let `MAX_LAG=3` finally pass the per-episode gate that killed it bare?*

---

## 1. The principled trigger (Gate 1 — PASS)

**Economic rationale (stated before tuning, not reverse-engineered to 2015-16).** A clean V-recovery and a sideways grind differ in the **trajectory of the rebound off the low**, not in the end-state level. The current trigger is a pure *level* test — `sharp_recovery = (score > stage4_score) & (price above 200d OR 10m MA)` — which a grind eventually satisfies just by drifting up. A V is distinguished by two causal, economically-motivated properties:

- **(i) Rebound momentum** — in a V, price is *rising right now*; in a grind it *drifts*. Operationalized as trailing 6-month SPY return `ret_6m ≥ V_MOM_MIN`.
- **(ii) The low is held, not re-violated** — in a clean V the trough is set once and price climbs and *stays* away from it; a grind keeps chopping back near its low. Operationalized as `recent_off_low ≥ V_NOLOW_MIN`, where `recent_off_low = (last-3-month low) / (trailing-18-month low) − 1` — i.e. even the *recent* low sits well above the trough.

These are descriptors of **shape**, with a clear reason, not a constant fitted to rescue one year. Characterizing the firing dates confirms the distinction is real and present in the data:

| firing date | trailing ret_6m | off-low climb | prior DD depth | recent_off_low | nature |
|:--|--:|--:|--:|--:|:--|
| 2009-06-30 | +4.7% | +36.8% | −51.5% | +19.9% | **clean V** (GFC bottom) |
| 2019-10-31 | +4.6% | +31.2% | −19.3% | +22.2% | **clean V** (only baseline firing) |
| 2024-09-30 | +11.3% | +45.1% | −10.5% | +30.4% | clean V |
| **2016-04-29** | **−0.1%** | +13.4% | −13.0% | **0.0%** | **sideways grind** (flat, low re-touched) |
| **2023-01-31** | **+0.5%** | +14.5% | −24.5% | **+4.1%** | grind-like (flat, low re-touched) |

The grind firings (2016, 2023-01) are exactly the ones with **flat momentum and a recently re-violated low** — the V-filter targets them on principle.

## 2. Out-of-sample / leave-one-out (Gate 2 — PASS, and it's what sinks the idea)

The decisive OOS check: **does the binding episode (GFC) depend on the filter parameters at all?** If the GFC hit survives across *every* filter setting — including settings that fully fix 2015-16 — then it is a filter-independent structural constraint, not a curve-fit artifact of any chosen parameter.

| mom | nolow | GFC ΔMDD (bp) | 2015-16 ΔMDD (bp) | gate |
|--:|--:|--:|--:|:--|
| 0.00 | 0.05 | **−118** | −166 | GFC FAILS |
| 0.00 | 0.10 | **−118** | **0** | GFC FAILS |
| 0.02 | 0.10 | **−118** | **0** | GFC FAILS |
| 0.04 | 0.10 | **−118** | **0** | GFC FAILS |
| 0.06 | 0.15 | **−118** | **0** | GFC FAILS |
| …all 12 cells… | | **−118** | (0 wherever 2015-16 is fixed) | **GFC FAILS** |

**The GFC episode worsens −118 bp for every single filter setting**, including all the ones that drive 2015-16 to exactly 0 bp. The filter perfectly removes the grind misfire it was designed for and **leaves the binding constraint untouched**, because the 2009 firing is a *real* V the filter correctly keeps. This is the strongest possible anti-curve-fit evidence: there is no parameter that both fixes the known-failing case and clears the gate, and the binding case is structural, not a tuning artifact.

## 3. Parameter plateau, not a peak (Gate 3 — PASS)

Two-knob sweep, `MAX_LAG=3 + V-filter`, reported as `CAGR% / worst-episode ΔMDD bp`:

```
            nolow=0.05   nolow=0.10   nolow=0.15   nolow=0.20
mom=0.00 |  8.69/-166    8.73/-118    8.73/-118    8.52/  -0
mom=0.02 |  8.74/-118    8.73/-118    8.73/-118    8.52/  -0
mom=0.04 |  8.73/-118    8.73/-118    8.73/-118    8.52/  -0
mom=0.06 |  8.72/-118    8.72/-118    8.72/-118    8.52/  -0
```

The interior is a **stable plateau** (CAGR ~8.72–8.74%, worst-episode −118 bp from GFC) across mom∈[0.02,0.06] × nolow∈[0.05,0.15] — robust, not a lone spike. The 2015-16 grind is removed everywhere except the loosest `nolow=0.05, mom=0.00` corner. The `nolow=0.20` column over-tightens and disables the override entirely (worst −0 bp because nothing fires, losing the benefit too). So the refinement *behaves* robustly — but the plateau it sits on still fails the gate, because the plateau floor is the GFC −118 bp.

## 4. Per-episode safety gate (Gate 4 — **FAIL**)

Hard gate (same as the one that killed `MAX_LAG 6→3`): **no historical crisis episode may worsen by > 50 bp** in episode-window maxDD. Representative interior cell `mom=0.02, nolow=0.10`:

| episode | base maxDD | refined maxDD | ΔMDD (bp) | base endNAV | refined endNAV | ΔendNAV (bp) |
|:--|--:|--:|--:|--:|--:|--:|
| **GFC 2008-09** | −8.80% | −9.97% | **−118 ✗** | +25.41% | +28.32% | +290 |
| 2011 euro | −5.92% | −5.92% | 0 ✓ | +6.83% | +7.75% | +91 |
| **2015-16 grind** | −7.38% | −7.38% | **0 ✓ (FIXED)** | +3.55% | +3.55% | 0 |
| 2018-Q4 | −10.20% | −10.20% | 0 ✓ | −1.56% | −1.56% | 0 |
| COVID 2020 | −10.00% | −10.00% | 0 ✓ | +7.50% | +7.50% | 0 |
| 2022 bear | −7.36% | −7.36% | 0 ✓ | +5.95% | +5.95% | 0 |

For contrast, `MAX_LAG=3` **bare** (the prior HELD failure, reproduced exactly): GFC **−118 bp**, 2015-16 **−150 bp** — two episodes blown.

**What the refinement achieved:** it cleanly **fixed 2015-16** (−150 bp → 0 bp) — the principled grind filter works perfectly. **What it could not fix:** the GFC episode (−118 bp), which is *not a grind misfire* — it is the override correctly firing on the genuine 2009 V and re-risking ~3 months earlier, which **deepens the recovery-window drawdown by 118 bp while improving episode-end NAV by +290 bp**. This is the same risk-budget trade-off the prior session documented: the override always fires *after* the trough (full-window maxDD is byte-identical −10.20% either way — no new tail risk), it wins episode-end NAV, but it costs intra-window drawdown on real V-recoveries. The 50 bp gate is a drawdown-first mandate, and −118 bp fails it.

## 5. Look-ahead clean (Gate 5 — PASS)

- **Truncation test:** recomputed the V-filter features (`ret_6m`, `recent_off_low`) on history truncated at each test date; max |full − truncated| feature difference = **0.00e+00**. Features are pure trailing-window ops (`pct_change(126)`, `rolling(min)`), so a value at T uses only data ≤ T.
- **Byte-parity:** V-filter with infinitely-loose thresholds reproduces the canonical backtest exactly (max NAV diff **0.00e+00**) — the patch adds nothing when off.
- **Canonical suite:** `test_no_lookahead.py` + `test_reentry.py` pass 8/8 with the harness loaded.

---

## Curve-fit quadruple-check (explicit)

1. **Did we tune a number to rescue 2015-16?** No. The two knobs are *shape* descriptors with a stated economic reason (rebound momentum; low-not-re-violated). The grind firings *characterize* as flat-momentum / low-re-touched **before** any threshold was picked.
2. **Does the win survive on data it wasn't chosen on?** The binding case (GFC) is **filter-parameter-independent** — it fails −118 bp for all 12 settings, including every one that fixes 2015-16. There is no setting that passes the gate, so there is nothing to overfit *to*.
3. **Plateau or peak?** Plateau (CAGR flat ~8.73%, worst −118 bp across the interior). But the plateau floor is itself a gate failure.
4. **Could we declare victory by moving the goalposts?** Only by (a) dropping the 50 bp gate, (b) excluding 2008, or (c) measuring episode-end NAV instead of window maxDD (on which it *wins* +290 bp at GFC). All three would be exactly the goalpost-moving the standing order forbids. Held to the same gate that killed `MAX_LAG 6→3`, it fails the same way, for the same structural reason.

## Bottom line

The refinement is **real and principled** — it does exactly what it was supposed to (kills the 2015-16 grind misfire on a clean economic criterion, no look-ahead, robust plateau). It is **shelved anyway**, because speeding up the override (`MAX_LAG=3`, the only context in which the trigger refinement even matters) re-risks earlier into the *genuine* 2009 V and deepens that window's drawdown by 118 bp — a true V the filter must not and does not suppress. No clean-V refinement can clear the per-episode gate, because the binding episode is itself a clean V.

This confirms and sharpens the prior session's conclusion: **the regime engine's re-entry side is a robust plateau; there is no safe headroom to speed it up, and the `sharp_recovery` override stays gated by 2008/2009 regardless of how the trigger is sharpened.** Re-entry stays `MAX_LAG=6`. Lead closed. Bank the negative.

*Scripts (scratch, under `backtester/`): `_sharp_recovery_diag.py` (firing instrumentation), `_sharp_recovery_test.py` (variant + sweep + gate). Machine-readable results: `output/_sharp_recovery_results.json`. No canonical file modified.*
