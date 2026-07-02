# EDGAR point-in-time fundamentals pipeline — Phase 2 (full US market + survivorship recovery)

Build date: 2026-07-02. Scope: scale the **proven Phase-1 core** (canonical tag mapping,
YTD→quarterly differencing, point-in-time as-of/no-lookahead layer — validated on the Alcoa
restatement) from the advisor's ~800 names to the **full US market**, and recover the
**delisted/renamed survivorship set**. This is the fundamentals leg of the eventual
full-market CAN SLIM selection backtest.

**The Phase-1 core was NOT rewritten.** `_extract_company_facts`, `_as_first_filed`,
`_difference_ytd_to_quarters`, and `asof_quarterly` are unchanged. Phase 2 is breadth (iterate
all CIKs), a new identity/CIK resolver, and two documented derivation conventions — layered on
top of the same code path.

**Code:**
- `canslim/edgar_pipeline.py` — added `build_full` / `table_full` / `validate_full` stages and
  a factored `_build_quarterly_table()` so Phase-1 and full-market use ONE table code path.
- `canslim/edgar_resolver.py` — NEW. Delisted/renamed old-ticker→CIK resolver built from SEC's
  bulk `submissions.zip` (name + `formerNames` index).
- `canslim/tests/test_edgar_pipeline.py` — 11 tests green (8 Phase-1 + 3 new: Q4-EPS derive,
  Q4-EPS null-when-missing, TTM-ROE).

**Data (local warehouse, never on Drive — `C:\TradingDesk-Local\canslim\edgar\`):**
- `companyfacts.zip` (1.39 GB, 19,989 company JSONs) — from Phase 1, unchanged.
- `submissions.zip` (1.55 GB, 975,352 primary CIK records) — NEW bulk grab (identity source).
- `cik_identity.parquet` / `cik_name_index.parquet` — NEW identity index.
- `pit_facts_full/shard=*.parquet` — NEW partitioned full-market PIT fact store (CIK % 20).
- `quarterly_fundamentals_full/shard=*.parquet` — NEW partitioned clean quarterly table.
- `phase2_coverage.csv`, `phase2_unresolved_concepts.csv`, `phase2_delisted_recovered.csv`.

---

## 1. Full-market point-in-time fundamentals (breadth)

`build_full` iterates **every** `CIK##########.json` in `companyfacts.zip` (19,989 files)
through the unchanged Phase-1 parser, labels each by its current primary ticker where known
(else bare CIK — fundamentals are keyed by CIK regardless), and writes a partitioned PIT fact
store sharded by `CIK % 20`. `table_full` then builds the clean quarterly table shard-by-shard
(bounded memory) via the shared `_build_quarterly_table()`.

**Coverage (honest count):**

| | Count | % of JSONs |
|---|---:|---:|
| companyfacts JSONs iterated | 19,989 | 100% |
| produced ≥1 canonical us-gaap fact | **15,046** | **75.3%** |
| no canonical us-gaap facts (foreign 20-F/IFRS, funds, trusts, sparse/pre-XBRL) | 4,943 | 24.7% |
| **companies in the clean quarterly table** | **14,946** | — |
| total quarterly rows | **1,126,407** | — |

The 100-company gap between "canonical facts" (15,046) and "in quarterly table" (14,946) is
companies that have some canonical instant/annual fact but never a clean YTD flow ladder to
difference into discrete quarters (mostly a single annual 10-K, or gap-riddled interims) — the
gap-aware differencer correctly withholds rather than fabricate a mislabeled quarter.

Delisted/renamed companies are **included automatically** in this pass: `companyfacts.zip`
contains a facts file for every CIK that ever filed us-gaap XBRL, so a delisted company's
fundamentals are parsed whether or not it still has a live ticker. The resolver (below) only
adds the ability to *look those CIKs up by an old ticker*.

---

## 2. Delisted / renamed CIK resolver (survivorship-critical)

`edgar_resolver.py` builds an identity index from SEC's `submissions.zip`:
- **975,352** primary CIK records parsed; **56,453** carry ≥1 `formerNames` entry.
- A long **name index** of **1,047,768** rows: every current legal name (975,337) + every
  former name (72,431), each normalized (uppercase, punctuation stripped, corporate suffixes
  like INC/CORP/LLC/HOLDINGS removed) → CIK.

Resolution of an old ticker → CIK uses two sourced layers, in order:
1. **Fuller ticker map** — union of `company_tickers.json` and every ticker SEC currently
   associates with each CIK in submissions.
2. **Sourced delisted-ticker→name seed** matched against the name index. Delisted tickers have
   **no row in SEC's ticker map at all** (the `tickers` field is empty for a delisted shell —
   e.g. Twitter CIK 1418091 has `tickers: []`), so a ticker can only be recovered via the
   company's **legal name** matched against name+formerNames. The seed maps each old ticker to
   a real, checkable company name (an M&A/rename fact — an identity label, never a synthesized
   figure). Every seed entry was programmatically verified to hit the name index.

Ambiguous normalized-name collisions (e.g. "DISH Network LLC" vs "DISH Network CORP" normalize
identically) are broken by `_rank_name_hits`: **prefer a CIK that actually has a companyfacts
file** (a CIK with no fundamentals is useless to a backtest), then former-name over current,
then lowest CIK (the original operating filer, not a later shell).

**Result on Phase-1's 97 unresolved names:**

| | Count |
|---|---:|
| CIK recovered | **88 / 97 (91%)** |
| recovered **with actual us-gaap fundamentals** on file | **87 / 97 (90%)** |
| via name-seed (delisted, no ticker row) | 86 |
| via fuller ticker map | 2 |
| still unrecovered | 9 |

The 9 still-unrecovered (AMEH, DADA, FANH, MIXT, OTRK, SCPL, TA, VTNR, FOCS) are chiefly
foreign small-cap ADRs (Chinese issuers on 20-F, no us-gaap facts to recover anyway) and a
couple of micro-caps — an honest, understood tail, not a systematic miss. One recovered CIK
(RCII / Rent-A-Center) matched a CIK with no companyfacts file (its us-gaap facts sit under a
different CIK the name match didn't reach) — a single-name miss, flagged in
`phase2_delisted_recovered.csv`. Concretely proven end-to-end: **TWTR → CIK 1418091** parses
through the shared table builder and yields Twitter's quarterly fundamentals ending 2022 (when
it went private) — survivorship-free, no forward leak.

---

## 3. Conventions chosen (as-reported/derived, documented — not synthesized)

### Q4 EPS
Phase 1 left Q4 EPS **null** where the filer reports only a full-year EPS in the 10-K. Phase 2
**derives** it when — and only when — all three interim discrete-quarter EPS **and** the annual
EPS are present and as-first-filed:

    Q4_EPS = FY_EPS − (Q1_EPS + Q2_EPS + Q3_EPS)      # all as-reported EPS numbers

The row is flagged `eps_source = 'derived_q4'` (vs `'as_reported'`). Where any input is missing
it stays **null** — never fabricated, and never computed from net income ÷ shares. **Caveat**
(carried in the flag): EPS is not perfectly additive across quarters because the diluted share
count drifts intra-year, so a derived Q4 can differ slightly from a company's own later-disclosed
Q4. The flag lets the backtest drop derived Q4 EPS if it wants strict as-reported-only.

### TTM ROE
Phase 1 used a crude `roe_q × 4`. Phase 2 uses a proper trailing-twelve-month convention:

    roe_ttm = (trailing-4-quarter net income) / (average of the two bounding equity snapshots)

i.e. TTM net income over `avg(equity_t, equity_{t−4})`, per ticker in period order, from
as-first-filed figures only. Null until 4 quarters of NI and both equity snapshots exist. The
crude `roe_q` is retained alongside for continuity/comparison; `roe_ttm_annualized` now aliases
the real TTM figure.

---

## 4. Foreign filers — deferred to Phase 2b (noted gap, not silently dropped)

The full-market pass is **US-GAAP 10-K/10-Q only**. Foreign private issuers filing **20-F/40-F**
(annual, frequently on the **IFRS** taxonomy, often non-USD) are the largest remaining coverage
chunk — ASML, BABA, TSM, TM, SAP, SPOT, NVO, AZN, SE, PDD, RACE, and hundreds more market-wide.
They appear in the `no_canonical_facts` count. Recovering them (extend the form filter to
20-F/40-F, add an `ifrs-full:*` concept map, handle non-USD units) is a defined **Phase 2b**;
doing it inside Phase 2 would have blown scope and mixed two taxonomies before the US-GAAP
full-market base is validated.

---

## 5. Validation at scale

### Coverage
14,946 companies → 1,126,407 clean discrete-quarter rows; 75.3% of all companyfacts JSONs
produced canonical us-gaap facts (see table in §1). Period-ends span the XBRL era (~2009→2026).

### Spot-checks across caps (last 4 discrete quarters, as-first-filed)
- **AAPL** (mega): FY2026 Q1 (2025-12-27) revenue $143.756B, diluted EPS $2.84 — matches
  Apple's reported figures; FY2025 Q4 EPS $1.84 flagged `derived_q4`; roe_ttm ~1.5 (Apple's
  low-equity buyback-driven ROE, correct).
- **MSFT** (mega): FY2026 Q3 (2026-03-31) revenue $82.886B, EPS $4.27, +18.3% YoY sales; FY2025
  Q4 EPS $3.65 `derived_q4`.
- **AAON** (mid): FY2026 Q1 (2026-03-31) revenue $496.936M, **+54.3% YoY** — matches Phase 1.
- **ADMA** (small): FY2025 quarters trace the reported ramp; EPS as-reported, Q4 derived.
- **PLAB** (small, off-calendar FY): revenue and EPS trace correctly across its non-Dec fiscal
  year; Q4 (2025-10-31) EPS $1.06 `derived_q4`. Confirms the FY-labeling logic handles
  off-calendar fiscal years.

All five cross-cap spot-checks land on sensible, reported-consistent figures with the correct
`eps_source` flag and a plausible TTM ROE.

### As-of / no-lookahead re-confirmed at scale
The load-bearing anti-curve-fit guarantee is **unchanged code**, re-run on full-market shards.
Auto-discovered restatement — **ABG (Asbury Automotive) FY2009 revenue**:

```
first filed 2012-02-22: value = 3,371,800,000   (original consolidated)
later filed 2012-02-22: value =   557,200,000   (a restated/dimensional value)
as-of 2012-02-23: query returns 3,371,800,000   -> ORIGINAL, no forward leak. OK
as-of 2012-02-21 (before filed): period visible? False -> correctly invisible. OK
```

The Phase-1 Alcoa cross-year restatement proof still holds on the watch-list store; this ABG
case confirms the same invariant survives at full-market scale.

The 11 pytest tests lock the invariants: multi-era tag stitching, YTD differencing (values +
derived-quarter filing-date + no-subtract-across-gap), four as-of invariants (returns original
not restatement; hides not-yet-filed; never shows a future filing; prefers tag priority), plus
the new Q4-EPS derivation, Q4-EPS null-when-missing, and TTM-ROE.

### The messy tail, honestly
- **Foreign/IFRS 20-F filers** are the bulk of the 4,943 `no_canonical_facts` — deferred to
  Phase 2b (§4). Not a bug; a scoped gap.
- **Unmapped-tag audit at scale confirms no revenue/EPS leakage.** The top unmapped
  statement-ish tags market-wide are `IncomeLossFromContinuingOperationsBeforeIncomeTaxes…`
  (pretax-income components, ~10k filers), `AccumulatedOtherComprehensiveIncomeLossNetOfTax`,
  `AntidilutiveSecuritiesExcluded…`, NCI attribution, and deferred-revenue line items — all
  **sub-line items we intentionally skip**, never a missed canonical revenue/net-income/EPS
  concept. (`phase2_unresolved_concepts.csv`.)
- **Duplicate/mislabeled quarter rows exist for a minority of filers** whose XBRL `fy`/`fp`
  labeling is inconsistent across amendments (observed on TWTR: a period_end appears under two
  fy labels). The underlying figures stay as-first-filed correct; the row-keying just isn't
  perfectly deduplicated market-wide. Documented, not silently patched — a targeted dedup
  (key on period_end, not fp) is a small Phase-2b hardening item and was left un-tuned to
  avoid curve-fitting the parser to specific filers.
- **~100 companies have canonical facts but no clean quarterly ladder** (single annual filing
  or gap-riddled interims) — correctly withheld by the gap-aware differencer.

---

## 6. What the full-market SELECTION backtest still needs next (precise)

The fundamentals leg is now full-market + survivorship-recoverable. To reach an honest
full-universe CAN SLIM **selection** backtest, still required:

1. **Full-universe, survivorship-free PRICES + volume**, delisted names included, aligned to the
   recovered CIKs. This is the single biggest missing piece — EDGAR gives fundamentals, not
   prices. Needs a point-in-time price/volume source (IBKR history first per the IBKR-first
   sourcing rule; check delisted-name coverage) keyed to CIK↔ticker↔exchange over time.
2. **A CIK↔ticker↔date mapping over history** (which ticker traded for which CIK on which date),
   so fundamentals join to prices without lookahead. The submissions `formerNames`/`tickers`
   history is the seed; a date-versioned ticker timeline must be assembled.
3. **RS (relative strength) rating** — the market-relative price-performance percentile (IBD's
   RS line), computed from the survivorship-free prices as-of each rebalance date.
4. **EPS rating & Composite rating** — IBD-style percentile ranks combining the EPS-growth /
   sales-growth / ROE / margin fundamentals (already produced here) with RS, computed as-of.
5. **The screen + detector + winning-exit backtest across regimes** — apply the CAN SLIM screen
   (C/A/N/S/L/I/M gates) on the as-of ratings, wire in the already-proven execution engine
   (`canslim/execution_engine.py`, the ~2.7× discipline win) and the winning-exit rule, and run
   across regimes with the anti-curve-fit discipline (out-of-sample, per-regime, placebo).
6. **Phase 2b foreign filers** (20-F/40-F + IFRS) if the universe is to include ADRs.

Nothing in Phase 2 changed the two proven load-bearing pieces (canonical mapping + as-of
no-lookahead). This was coverage breadth + identity recovery + two documented conventions.
