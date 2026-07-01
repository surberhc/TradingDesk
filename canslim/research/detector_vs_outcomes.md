# Detector verdict vs. realized outcome — is the deterministic base/pivot detector a quality FILTER?

**Question (cheap, pure-analysis).** The deterministic base/pivot detector reproduces ~72%
of advisor APS's recorded pivots and *rejects* ~25% (deep bases, extended/add-on entries,
handle defects). Are the setups it rejects his **winners** or his **losers**? If a strict
deterministic replica preferentially discards his losers, the 25% it can't copy is a feature
(discipline), not a bug (lossy copy).

**Method.** Joined `detector_validation.csv` (bought, per-pick verdict) to
`stop_analysis_trades.csv` (realized ticker/entry/exit/return, 2023–2026) on ticker, using a
one-to-one nearest-buy-date assignment to disambiguate multi-buy repeats. Bucketed by verdict,
computed win rate / avg / median return and a nominal **$10k-per-trade** P/L (we have no share
counts, so equal-weight nominal $ is the honest comparator). Descriptive only.

## Join coverage
- 120 realized trades → **117 matched** to a detector verdict.
- Unmatched (no bought-detector row): **IONQ, SMCI, SQQQ** (SQQQ is an inverse ETF, not a base
  pick; IONQ/SMCI not in the bought-detector set).
- Bought names with no realized trade in the stop file: ASTS, DELL, INTC.
- 112 of 117 matches are same-day (gap = 0). **5 weak joins** (>30d: CCJ, TOST, AXON, VRT) —
  sensitivity below shows they don't move the conclusion.
- Carryover data-gap caveats (NVDA/ERJ/PSTG/SQQQ) noted; ERJ lands in `no_base_datagap`.

## Bucket table (nominal $10k/trade)

| Bucket | n | Win % | Avg ret % | Median ret % | $ P/L (10k) |
|---|---:|---:|---:|---:|---:|
| **CONFIRMED clean** (base found, pivot ±5%) | 44 | **22.7** | **−2.45** | −6.31 | **−10,792** |
| rejected: DEEP base | 18 | 38.9 | +10.41 | −6.48 | +18,734 |
| rejected: EXTENDED / add-on past pivot | 12 | 50.0 | +23.89 | +3.54 | +28,672 |
| found, pivot off >5% or GT corrupt | 39 | 33.3 | +2.44 | −5.44 | +9,515 |
| rejected: handle ambiguity | 2 | 100.0 | +0.68 | +0.68 | +135 |
| no base / data-gap | 2 | 50.0 | +7.51 | +7.51 | +1,501 |

## HEADLINE — trading only textbook setups vs. taking everything

| Population | n | Win % | Avg ret % | Median % | $ P/L (10k) |
|---|---:|---:|---:|---:|---:|
| **CONFIRMED-only** (what a strict replica would trade) | 44 | **22.7** | **−2.45** | −6.31 | **−10,792** |
| **ALL his trades** | 117 | 33.3 | +4.08 | −5.54 | +47,765 |
| **REJECTED-only** (deep + extended) | 30 | 43.3 | +15.80 | −6.30 | +47,406 |
| REJECTED-broad (+ handle / no-base) | 34 | 47.1 | +14.42 | −4.51 | +49,042 |

**The hypothesis is inverted.** Restricting to the detector-confirmed textbook setups does
NOT improve on taking everything — it **destroys** the record: win rate falls 33% → 23%,
average return goes from +4.1% to **−2.5%**, and a +$47.8k book (nominal) becomes **−$10.8k**.
Nearly the entire realized gain lives in the trades the strict detector would have thrown away:
the deep-base and extended-entry buckets alone are +$47.4k — essentially the whole book.

## Winner / loser composition
- **Worst 23 trades (bleeders):** only **43%** were detector-rejected; **the rest are
  detector-confirmed or found**. Confirmed-clean names sit right among the biggest losers
  (WLDN −33%, AGX −20%, PRCT −17%, VIAV −16%, VRT −14%, DUOL −14%, PLTR −13%).
- **Best 23 trades:** only **17%** were detector-confirmed. The monster winners are almost all
  rejected/found — VKTX +162% (deep), OKLO +138% (extended), MSTR +113% (extended), RBLX +71%,
  VKTX +62% (deep), RKLB +60% (deep), CRDO +48% (extended).
- Across the whole book: **losers are 44% confirmed / 23% rejected; winners are 26% confirmed /
  41% rejected.** Confirmation skews toward his losers; rejection skews toward his winners —
  the opposite of the filter thesis.

## Sensitivity (drop the 5 weak joins, gap >30d)
CONFIRMED-only 23.8% win / −2.11% avg / −$8.9k · ALL 33.9% / +4.67% / +$52.3k ·
REJECTED-only 43.3% / +15.80% / +$47.4k. **Conclusion unchanged.**

## Verdict — plain English
**No. The detector's stricter discipline does NOT filter toward his better trades — it filters
them OUT.** In this realized 2023–2026 sample, the ~25% of setups the deterministic replica
can't reproduce (deeper-than-textbook bases and extended/add-on entries) are where his edge
actually showed up: they carry the higher win rate, far higher average return, and virtually
all of the dollar gain. Trading only the clean textbook bases would have turned a winning book
into a losing one.

That does **not** mean the detector is worthless — but its role is **assist-grade pivot/base
labeling, not a trade gate.** Two readings, both curve-fit-safe:
1. His *edge is the discretion the textbook rejects* — buying leaders extended out of deep
   bases (momentum/strength names) in a strong-tape regime, which O'Neil's published depth/
   extension caps explicitly forbid. The book's caps are the wrong filter for how he actually wins.
2. **Regime confound (the honest caveat):** 2023–2026 was largely a bull tape, which rewards
   exactly the aggressive extended/deep-base buys that get punished in a correction. This is
   his realized outcome on names *he* chose in *this* window — **not a forward test, not
   causal, and small (n≈117, several buckets <20).** A deep-base/extended entry that ran +160%
   here could be the trade that ends a career in a 2022- or 2008-type tape. Do **not** conclude
   "widen the depth caps" — that would be curve-fitting the detector to a bull-market sample
   (rule #1). The clean finding is narrower: **the detector is a labeler, not a P/L filter,
   on this data.**

*Files:* `detector_vs_outcomes_joined.csv` (per-trade join + bucket), this report. Nominal
$10k/trade equal-weight (no share counts available). n=117 matched of 120 trades.
