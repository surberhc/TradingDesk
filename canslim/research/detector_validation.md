# CAN SLIM base/pivot detector — validation vs advisor's recorded pivots

**Phase 3 of the replica build.** Question: can a *deterministic* detector, encoding only
O'Neil's published base geometry (`canslim_oneil_spec.md` §3), independently reproduce the
advisor's discretionary chart judgment — the bases he saw and the pivots he recorded?

**Discipline:** every numeric bound in `base_detector.py` is cited to the spec and was NOT
tuned to maximize agreement with his picks. Where his effective tolerance differs from
textbook, that is reported as a FINDING below, not fitted away. Detection runs on WEEKLY
bars (daily resampled to W-FRI); pivot/volume refined on daily. Hard no-lookahead: only
bars on/before the as-of week inform each detection (unit-tested in
`canslim/tests/test_base_detector.py::test_no_lookahead_causality`).

---

## Headline results — the 118 "bought" picks (his highest-conviction actions)

| Metric | Value |
|---|---|
| Bought picks | 118 |
| Priceable (had usable deep history) | 116 (98.3%) |
| Corrupt ground-truth pivot cells (excluded from pivot accuracy) | 5 |
| **Detection rate** (valid base found at his as-of week, of priceable) | **72.4%** (84/116) |
| Pivot pairs compared (both he & detector produced a pivot, GT clean) | 81 |
| **Pivot within ±2%** | **35.8%** |
| **Pivot within ±5%** | **56.8%** |
| **Pivot within ±10%** | **84.0%** |
| Pivot median abs error | 3.49% |
| Pivot mean abs error | 4.76% |

Pattern mix of detected bases (bought): double_bottom=60, cup_with_handle=18, flat_base=4, consolidation=2

### Failure modes (bought — why the detector found no base, or the row was unusable)
| Cause | Count |
|---|---|
| depth:base_deeper_than_textbook | 19 |
| extended_entry:price>pivot+5% | 11 |
| data_gap:no_price | 2 |
| handle:ambiguous_or_defect | 2 |

---

## Combined (bought + priceable watchlist events)

| Metric | Value |
|---|---|
| Total picks evaluated | 709 |
| Priceable | 642 (90.6%) |
| Detection rate (of priceable) | 74.9% (481/642) |
| Pivot pairs | 456 |
| Pivot within ±2% / ±5% / ±10% | 41.0% / 62.5% / 85.3% |
| Pivot median / mean abs error | 3.22% / 4.76% |

Pattern mix (combined): double_bottom=341, cup_with_handle=118, flat_base=14, consolidation=8

### Failure modes (combined)
| Cause | Count |
|---|---|
| depth:base_deeper_than_textbook | 77 |
| data_gap:no_price | 61 |
| extended_entry:price>pivot+5% | 52 |
| data_gap:short_history(0bars) | 17 |
| handle:ambiguous_or_defect | 11 |
| data_gap:history_ends_before_asof | 6 |
| data_gap:short_history(56bars) | 1 |
| shape:no_pattern_matched | 1 |
| data_gap:short_history(36bars) | 1 |
| data_gap:short_history(32bars) | 1 |

---

## Findings — how codifiable is his chart eye?

1. **Pivot geometry is highly codifiable where a base is found.** Among clean pivot pairs,
   the median absolute pivot error is ~3.49% and ~56.8%
   land within ±5% — i.e., the "+$0.10 above pattern resistance" rule reproduces his recorded
   buy point to within a few percent most of the time. The +$0.10 offset is trivially exact;
   the residual error is *where the resistance line is drawn*, which the code gets right when
   the pattern is a clean flat/cup/W.

2. **Detection (does a valid base exist at all) is the harder, more discretionary half.**
   Detection rate ~72.4% means the code independently confirms a
   textbook base for most, but not all, of his buys. The misses cluster on:
   - **Base deeper than textbook** — he tolerates cups/bases deeper than O'Neil's 33%/cup,
     15%/flat published caps. This is a genuine *tolerance difference*, reported (not fitted):
     his effective depth ceiling runs looser than the book.
   - **Extended entries / add-ons** — several "bought" rows are pullback-adds or gap-outs
     bought *past* the pivot (his own comments say "bought after pullback to 10dma",
     "gapped out of base on EPS"). No fresh base exists at that instant by construction;
     the detector correctly finds none.
   - **Handle ambiguity** — the upper-half / downward-drift / depth handle rules are the
     most judgment-laden; a minority of cups are rejected on handle defects a human would
     wave through.

3. **His labels are generic; the detector's pattern names are best-effort.** His comments
   rarely name a specific O'Neil pattern (he writes "base", "b/o", "pivot test"). The detector
   assigns a concrete pattern; the double-bottom detector fires most often because many of his
   consolidations have a mid-range peak near his recorded pivot. Pattern-label agreement is
   therefore weaker than pivot agreement — but pattern *label* is not what he records; the
   *pivot* is, and that is what we can reproduce.

4. **Data quality is a real, quantified drag.** 5 bought
   rows carry a pivot cell that is impossible for the actual price at that date (spreadsheet
   parse/row misalignment, e.g. AXON 34.20 when it traded ~$180). These are excluded from
   pivot accuracy rather than counted as detector misses.

## Verdict — MOSTLY codifiable, with an irreducible discretionary residue

A deterministic detector reproduces the advisor's chart eye **mostly, not fully**:
- **The pivot he writes down is largely mechanical** — once the base is identified, "+$0.10
  above resistance" recovers his number within ±5% the majority of the time.
- **Whether a proper base exists is partly discretionary** — the code agrees on a clear
  majority, but he runs looser depth tolerances than the textbook and buys extended/add-on
  entries where no fresh base exists. Those are the systematic gaps, and they are *findings
  about his style*, not detector bugs. Forcing agreement there would mean curve-fitting the
  bounds to him — explicitly declined.

**Bottom line:** the pivot arithmetic is reproducible; the base *recognition* is assist-grade,
not replacement-grade. This matches O'Neil's own line (spec §9): base/pivot recognition is
"the irreducibly discretionary core" — codeable to a strong majority, not to 100%.

*Coverage:* 116 of 118 bought names priceable (98.3%); the combined bought + RS≥90 watchlist
batch is 642 of 709 priceable (90.6%), 456 clean pivot pairs compared. The only bought gaps
are ERJ (Embraer ADR — no IBKR SMART definition) and PSTG (no SMART definition on the paper
gateway) — both delisted/renamed on the contract DB, not data we can fabricate. Combined
data-gaps (~85) are mostly delisted/M&A'd watchlist tickers and a handful of 2018-2019 events
that predate the pulled window. Price basis: IBKR TRADES (split-adjusted chart price, matching
his un-dividend-adjusted pivots); Tiingo fallback split-adjusted the same way.

**Curve-fit discipline — the load-bearing finding.** The single largest source of detector
"misses" is *base depth*: 77 of the combined misses are bases the detector rejects as deeper
than O'Neil's published caps (33% cup / 15% flat / 40% double-bottom) but that the advisor
evidently accepted. His effective depth tolerance runs LOOSER than the textbook. We report
this and did NOT widen the depth bounds to absorb it — doing so would be fitting the detector
to his data, which rule #1 forbids. The bounds stay at the spec's published numbers; the gap
is a true finding about his style, not a knob to turn.
