# ARM 3 — FILL REALISM: how much of the SPX 0DTE condor credit does the bid/ask eat?

_Generated 2026-07-06. Window 2022-01-03 -> 2026-07-01. PAPER / research only. STRICTLY READ-ONLY warehouse. Pre-registered in `docs\PREREG_condor_reopen_2026-07-06.md` (Arm 3). Measurement only — no P&L simulation._

## HEADLINE — is f50-worst-side fair, pessimistic, or optimistic?

**f50-worst-side is FAIR as a central planning assumption — a touch OPTIMISTIC if anything, and it is NOT too harsh.** The prior f50 refutations stand.

The 14:00 SPX 0DTE iron condor collects a thin credit (median mid credit ≈ **$85**, 0.85 pts), and the four-leg bid/ask on that credit is large. The market's own quoted **worst-side** (control-honest: sell shorts at bid, buy wings at ask) costs a median **15.2% of the mid credit one-way** (IQR 12.8%–18.5%); crossing the full four-leg spread on entry AND exit (round trip) eats a median **30%** of the mid credit.

Reading that against the management report's fill band (fraction `f` of the mid→worst-side combo spread; the control's honest open sits at f=1.0 = the full 15.2%):

- **mid (f=0)** charges **0%** of credit to the spread — a fantasy for a four-leg 0DTE combo; every prior positive verdict lived here.
- **f50 (headline)** charges half the worst-side ≈ **7–8% of credit one-way** — a mid-ish fill, i.e. the router splits the spread. This is a *reasonable-to-generous* central assumption.
- **full worst-side (f=1.0)** = the whole 15.2% one-way — the honest pessimistic bound.

So the real market most plausibly fills **between f50 and full worst-side** (a live router rarely captures true mid on a thin four-legged 0DTE combo). f50 is therefore fair-to-slightly-optimistic at the median — **not pessimistic**. Two caveats sharpen this: (1) the distribution has a **fat right tail** — mean one-way is 20.3% vs the 15.2% median, 3.5% of days exceed 50%, and 1.4% exceed 100% (the spread eats the *entire* credit), so on the bad days even full-worst-side understates reality; (2) that tail concentrates in the stress/neg-gamma regime that already loses on realized move. **Bottom line: the transaction-cost wall the whole line of research kept hitting is real and correctly sized; f50 is an honest yardstick; the "mid fill" positives are the artifact.**

The one thing this warehouse measurement does NOT settle is *where between mid and worst-side a live router actually fills* (the empirical `f`) — it measures the width of the penalty, not the fraction paid. That is the gold-standard follow-on below. What it settles decisively: the penalty is large enough that mid is indefensible and f50 is not too harsh.

## What was measured

For each tradeable day we rebuilt the control's EXACT 14:00 iron condor (0.15-delta shorts, 5-pt wings — via `s6_control._build_iron_condor`) and, at the 14:00 snapshot, measured the four legs' bid/ask geometry. No P&L, no exit scan. Same causal delta recon as the control; regime labels are prior-EOD (causal) via `s6_matrix.DayClassifier`.

- Day-rows: 1127. Measured (clean 14:00 condor built): 1090. With a positive mid credit (usable for %): 1086.
- Median mid credit (1-lot condor): **$85** (0.85 pts); mean $80. Worst-side credit goes negative on 1.4% of days (spread > premium).
- **Definitions.** *One-way worst-side cost* = mid credit − worst-side credit (sell shorts at bid / buy wings at ask, exactly the control's honest open) = half of every leg's bid/ask width. *Round-trip* = the full four-leg spread crossed once each way. *% of credit* uses the MID credit as denominator.

## Distribution — WORST-SIDE ONE-WAY spread cost as % of mid credit

| bucket | n | median | p25 | p75 | p05 | p95 | mean |
| --- | --- | --- | --- | --- | --- | --- | --- |
| OVERALL (one-way %) | 1086 | 15.2 | 12.8 | 18.5 | 10.0 | 33.3 | 20.3 |
| OVERALL (round-trip %) | 1086 | 30.3 | 25.6 | 37.0 | 20.0 | 66.7 | 40.7 |

### By year (one-way worst-side % of mid credit)

| bucket | n | median | p25 | p75 | p05 | p95 | mean |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2022 | 217 | 17.4 | 14.3 | 21.7 | 11.1 | 61.2 | 28.5 |
| 2023 | 248 | 14.9 | 12.2 | 17.2 | 9.6 | 25.3 | 17.6 |
| 2024 | 250 | 14.9 | 12.5 | 17.2 | 10.2 | 26.1 | 17.4 |
| 2025 | 249 | 15.8 | 13.0 | 19.0 | 10.0 | 38.7 | 20.4 |
| 2026 | 122 | 14.3 | 12.3 | 17.0 | 9.5 | 22.7 | 17.4 |

### By gamma regime (prior-EOD, causal)

| bucket | n | median | p25 | p75 | p05 | p95 | mean |
| --- | --- | --- | --- | --- | --- | --- | --- |
| negative | 423 | 15.6 | 13.0 | 19.0 | 9.8 | 45.2 | 21.3 |
| neutral | 105 | 15.2 | 12.5 | 18.2 | 9.5 | 25.9 | 19.5 |
| positive | 558 | 15.2 | 12.8 | 18.3 | 10.0 | 27.3 | 19.8 |

### By VIX regime (prior-EOD VIX9D/VIX crossover)

| bucket | n | median | p25 | p75 | p05 | p95 | mean |
| --- | --- | --- | --- | --- | --- | --- | --- |
| backwardation | 275 | 16.0 | 13.0 | 19.1 | 10.4 | 70.1 | 25.8 |
| contango | 811 | 15.2 | 12.5 | 18.2 | 10.0 | 26.2 | 18.5 |

### By gamma x VIX cell

| bucket | n | median | p25 | p75 | p05 | p95 | mean |
| --- | --- | --- | --- | --- | --- | --- | --- |
| negative / backwardation | 179 | 16.0 | 13.0 | 20.0 | 10.0 | 64.7 | 26.7 |
| negative / contango | 244 | 15.1 | 12.8 | 18.5 | 9.8 | 26.6 | 17.3 |
| neutral / backwardation | 18 | 16.8 | 14.0 | 18.9 | 12.7 | 75.9 | 23.5 |
| neutral / contango | 87 | 14.3 | 12.2 | 17.3 | 9.2 | 24.4 | 18.6 |
| positive / backwardation | 78 | 15.9 | 13.0 | 18.9 | 10.5 | 49.9 | 24.2 |
| positive / contango | 480 | 15.2 | 12.5 | 18.2 | 10.0 | 25.9 | 19.1 |

## High-VIX vs low-VIX behavior

The spread-% is **worse (wider) in the stress regime**, as expected — thinner two-sided markets on a thin credit:

| VIX state | n | median one-way % | p25 | p75 | p95 |
| --- | --- | --- | --- | --- | --- |
| backwardation (stress / high) | 275 | 16.0 | 13.0 | 19.1 | 70.1 |
| contango (calm / low) | 811 | 15.2 | 12.5 | 18.2 | 26.2 |

The negative-gamma bucket — already the structurally worst P&L bucket in every prior run — also carries the widest spread-%, so its refutation is doubly robust: it loses on both realized move AND execution cost.

## Per-leg width — are the wings or the shorts the wide legs?

Per-leg bid/ask width (option points), median | mean:

| leg | median width | mean width |
| --- | --- | --- |
| put short (0.15-delta, near ATM) | 0.10 | 0.104 |
| call short (0.15-delta, near ATM) | 0.10 | 0.104 |
| put wing (5-pt further OTM) | 0.05 | 0.092 |
| call wing (5-pt further OTM) | 0.05 | 0.092 |
| **two shorts summed** | **0.15** | **0.21** |
| **two wings summed** | **0.15** | **0.18** |

**The SHORTS are the wider legs in absolute terms** (median 0.10 each vs 0.05 for the wings) — they carry more absolute spread because they are worth more. But relative to their own tiny price the far-OTM wings are proportionally wider (a 0.05 spread on a nickel-to-dime wing is a huge percentage). The practical consequence: **widening the wings does not shrink the spread you cross** (the two shorts, which dominate the absolute width, are unchanged by wing width), so Arm 1's wider-wing lever helps only by collecting MORE credit against a roughly fixed short-leg spread — it improves the denominator, not the numerator.

## Bottom line for the other three arms

The honest yardstick is confirmed: **f50-worst-side is a fair (mildly generous) fill**, and mid is a fantasy for a four-leg 0DTE SPX combo. The Arm-1 width sweep, Arm-2 neg-gamma hedge, and Arm-4 higher-DTE pivot should all be judged at f50 (headline) with full-worst-side as the honest bound — NOT at mid. Any lever that only "works" at mid is defeated by measured transaction cost. The one structural escape the width/DTE arms probe — **thicker premium relative to a fixed-ish spread** (wider wings collect more credit; higher DTE has fatter premium) — is exactly the right thing to attack, because it lowers this spread-%-of-credit ratio. This measurement is the denominator those arms must move.

## Gold-standard follow-on (SEPARATE deliberate gateway action — NOT executed here)

This warehouse analysis measures the *quoted worst-side width* — the size of the penalty. It does NOT measure where a real router actually fills between mid and that worst side (the fraction **f**). To pin f empirically:

1. On a calm session, with Andrew's explicit OK and a deliberate arm, place a **handful of 1-lot 0DTE SPX iron condors on the PAPER account** (DU…141, port 4002) at 14:00 with the same 0.15-delta / 5-pt construction, routed through the existing dynamic order router (MIDPRICE → Adaptive) exactly as production would.
2. Record, per fill: the NBBO mid at submit, the achieved fill price, and the realized (mid − fill) as a fraction of the one-way worst-side width = the empirical **f**.
3. A dozen fills across a couple of calm days is enough to say whether the router lands near f≈0.3–0.5 (mid-ish, better than worst) or gets dragged toward f≈1.0 on this thin, four-legged combo.

Requirements: a working armed paper gateway, the router's index-option recipe (already proven on DU142), and a deliberate review→arm→transmit gate. It is a real (if small) order flow, so it needs Andrew's go — do NOT bundle it into a research run. Until then, **f50 stands as the honest planning bar and this measurement confirms it is not too harsh.**
