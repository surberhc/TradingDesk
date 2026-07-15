# ARM 4 — Higher-DTE managed iron condor (30 & 45 DTE)
_Generated 2026-07-06. Instrument **SPXW** EOD warehouse chains. PAPER / research only. Pre-registered in docs/PREREG_condor_reopen_2026-07-06.md._

## Setup (frozen, pre-registered)
- Short-leg |delta| **0.16**, wings **50**-pt, target DTE in **[30, 45]**.
- Management: take at **50%** of entry credit OR **21-DTE** OR **2x-credit** disaster stop, else expiry.
- Single-book, no overlap; re-enter first EOD after a close. Commission $0.65/leg/contract, 8 legs round-trip.
- Honest fills: worst-side (**headline**) and mid (optimistic ceiling).
- OOS split at **2024-06-30** (train `entry < split`, test `>=`).

## Headline — total P&L (honest worst-side fill)
| DTE | trades | total $ | win% | avg $ | worst $ | avg hold (d) | credit/width | mid-fill total $ |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 30 | 266 | -91,425 | 36.5 | -344 | -4,805 | 9.8 | 19.2% | -11,258 |
| 45 | 132 | -75,863 | 42.4 | -575 | -4,515 | 20.9 | 18.8% | -11,966 |

## Per-year total P&L (worst-side fill)
| DTE | 2018 | 2019 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 30 | -12,287 | -7,177 | -14,241 | -1,648 | -4,996 | -11,187 | -15,432 | -16,942 | -7,514 |
| 45 | -8,459 | -7,388 | -19,149 | -2,318 | -1,703 | -2,203 | -9,293 | -21,199 | -4,152 |

## OOS train/test (worst-side fill)
| DTE | train total $ | train win% | test total $ | test win% |
| --- | --- | --- | --- | --- |
| 30 | -55,136 | 40.4 | -36,290 | 26.0 |
| 45 | -44,776 | 44.3 | -31,087 | 37.1 |

## Per-regime total P&L — VIX contango vs backwardation (worst-side)
| DTE | contango $ | backwardation $ | unknown $ |
| --- | --- | --- | --- |
| 30 | -70,679 | -20,746 | 0 |
| 45 | -42,174 | -33,690 | 0 |

## Exit-reason mix (count) + P&L by reason (worst-side)
| DTE | take | dte21 | stop | expiry | take $ | dte21 $ | stop $ | expiry $ |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 30 | 3 | 243 | 16 | 4 | 1,839 | -59,539 | -34,448 | 722 |
| 45 | 17 | 84 | 28 | 3 | 9,887 | -22,557 | -62,931 | -263 |

## Honest-fill impact (mid vs worst-side, total $)
| DTE | mid total $ | worst total $ | fill cost $ | fill cost as % of |mid| |
| --- | --- | --- | --- | --- |
| 30 | -11,258 | -91,425 | 80,167 | 712.1% |
| 45 | -11,966 | -75,863 | 63,898 | 534.0% |

## Vs the naive 45-DTE benchmark (no disaster stop)
| version | trades | total $ | win% | worst $ | avg hold (d) |
| --- | --- | --- | --- | --- | --- |
| managed 45 (this harness) | 132 | -75,863 | 42.4 | -4,515 | 20.9 |
| naive 45 (no disaster stop) | 111 | -51,829 | 49.5 | -5,460 | 24.9 |

**Reproduction check + difference explained.** The naive arm here (-$51,829, 111 trades, 49.5% win)
reproduces the prior report's naive 45-DTE figure (§7 of `condor_management_20260703.md`: -$53,205,
89 trades, 37.1% win) within the difference expected from switching the SPX root to the denser SPXW
root and using cleaner same-day re-entry. It is the same losing shape. **Adding the textbook 2x-credit
disaster stop made it WORSE, not better** (-$75,863 vs -$51,829). The stop does cap the single worst
trade (-$4,515 vs -$5,460) exactly as intended — but it fires 28 times, each locking in a ~2x-credit
loss *plus* the worst-side spread paid to close options that still hold weeks of extrinsic. The naive
arm instead lets some of those breaches ride to expiry, where a few recover before settlement. So the
"proper management" the pre-registration asked for does what textbooks claim to the *distribution*
(fewer max losses, tighter tail) yet still loses on *total* — because every managed exit re-crosses the
4-leg spread on premium-rich options.

## Matched placebo
Not run. The pre-registration gates the matched random-exit placebo on a DTE showing a **positive**
edge (it exists to prove management, not "being in the market", is the source of a *win*). Both DTEs are
decisively negative at the honest worst-side fill, so there is no positive edge to attribute — the
placebo is moot. (The harness will run it automatically via `--placebo` if a future variant turns
positive.)

## The decisive decomposition — why the higher-DTE thesis breaks
Arm 4's premise (from the pre-reg): 0DTE dies on transaction cost because premium is thin vs the 4-leg
spread; higher DTE should escape that because credit is thick vs cost. **The premise is half right and
that half is fatal:**

- **At ENTRY the thesis holds.** Entry credit is ~19% of wing width and the 4-leg entry spread is only
  ~9% of the mid credit (e.g. a 45-DTE SPXW condor collects ~9.9 pts honest vs ~10.9 mid — 1.0 pt of
  spread on a fat credit). Entry economics are genuinely a different, healthier regime than 0DTE.
- **At EXIT the thesis collapses.** Textbook management never holds to expiry — it closes at 21-DTE or on
  a 2x stop. Closing a 30/45-DTE condor with 9-24 days still on it means buying back options that still
  carry **weeks of extrinsic value**, and the 4-leg bid/ask on that premium-rich position is thick again.
  The honest-fill table makes this unmissable: mid P&L is only **~-$11-12k** (near scratch) at BOTH DTEs,
  but worst-side P&L is **-$76-91k**. The entire loss — 500-700% of the mid figure — is the spread paid
  to *exit early*. Management reintroduces the exact transaction-cost tax it was supposed to escape,
  because it swaps "hold cheap-at-expiry options to $0" for "sell still-expensive options across the
  spread."
- `dte21` (30-DTE) and `stop` (45-DTE) are the two loss engines, and both are spread-dominated forced
  early closes. The only positive buckets are `take` (small, rare) and held-`expiry` (tiny) — the exits
  that either caught a cheap close or paid no exit spread at all.

## VERDICT
**REFUTES.** A properly managed higher-DTE iron condor does **not** show a robust positive edge after
honest costs. It fails the pre-registered spine on every axis:

- **No plateau:** both grid cells lose big at the headline worst-side fill (30-DTE -$91k, 45-DTE -$76k).
  There is no adjacent-cell agreement on a *positive* number — only agreement on *loss*.
- **OOS confirms, not rescues:** train and test are both negative for both DTEs (30: -$55k/-$36k;
  45: -$45k/-$31k). Nothing hides in a single period.
- **Every regime loses:** negative in both VIX contango and backwardation, both DTEs.
- **Management made it worse:** the disaster stop the naive benchmark lacked *increased* the total loss.
- **Placebo moot:** no positive edge to attribute.

The higher-DTE pivot does confirm the pre-registration's diagnosis of *where* 0DTE dies (entry cost is
thin there, thick-credit here) — but it also proves that **managing** a condor at any DTE re-imposes the
same 4-leg spread tax at exit. The transaction-cost constraint is not a 0DTE artifact; it is a
managed-iron-condor artifact. This is a clean refutation and, per the decision rule, a valid publishable
outcome. Nothing is adopted.

_Cross-instrument robustness (SPX root, coarser 25-pt strikes): 30-DTE -$79,244 (118 trades, 36.4% win),
45-DTE -$69,887 (108 trades, 38.0% win) at the worst-side fill. Both DTEs lose big on SPX too — a
cross-instrument plateau of loss, not edge. Same losing shape as SPXW; the refutation is not root-specific._
