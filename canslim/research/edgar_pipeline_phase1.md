# EDGAR point-in-time fundamentals pipeline — Phase 1 (proof-of-concept)

Build date: 2026-07-01. Scope: a **bounded, validated proof-of-concept** on the advisor's
~800-name watch list — prove the hard parts (canonical tag mapping, YTD→quarterly
differencing, and the point-in-time as-of / no-lookahead layer) actually work before
expanding to the full US market. This is NOT the full-market build.

**Code:** `canslim/edgar_pipeline.py` (owned, purpose-built, no framework dependency).
**Tests:** `canslim/tests/test_edgar_pipeline.py` (8 tests, all green).
**Data (local warehouse, never on Drive):** `C:\TradingDesk-Local\canslim\edgar\`
  - `companyfacts.zip` (1.39 GB, 19,989 company JSONs — SEC bulk grab)
  - `company_tickers.json` (CIK↔ticker map)
  - `pit_facts.parquet` — raw point-in-time fact store (1,184,976 facts, every filing of
    every period, each carrying its filing date)
  - `quarterly_fundamentals.parquet` / `.csv` — the clean output table
  - `phase1_coverage.csv` — per-ticker resolution status (honest tail)
  - `unresolved_concepts.csv` — unmapped statement tags, counted

---

## What's built

Four owned stages (`python edgar_pipeline.py {ingest|build|table|validate|all}`):

1. **INGEST** — downloads SEC's consolidated `companyfacts.zip` + the CIK↔ticker map into
   the local warehouse. Declares a descriptive `User-Agent` per SEC fair-access; the bulk
   grab is one request (well under the 10 req/sec cap). *Note: the working bulk URL is the
   `/Archives/edgar/daily-index/xbrl/companyfacts.zip` path — the `/bulkdata/` path in the
   prior research now 403s (fixed and documented in code).*

2. **CANONICAL CONCEPT MAPPING** — the fiddly core. Each canonical field (revenue,
   net_income, eps_diluted, eps_basic, shares_diluted, equity, assets, gross_profit,
   operating_income, cost_of_revenue) maps to a **priority-ordered list of us-gaap tag
   variants** (e.g. revenue = `RevenueFromContractWithCustomerExcludingAssessedTax` →
   `Revenues` → `SalesRevenueNet` → `SalesRevenueGoodsNet` → …). Critically, we do **not**
   pick one variant per company: a single filer routinely switches revenue tags across eras
   (e.g. `SalesRevenueNet` pre-2018 → the ASC606 tag post-2018). We keep ALL variants and
   resolve the winner **per period** by priority, which stitches the eras into one clean
   series without double-counting. Unmapped statement tags are logged and counted.

3. **YTD→QUARTERLY** — 10-Q income-statement figures are cumulative year-to-date. We recover
   discrete quarters by differencing consecutive YTD periods within a fiscal year
   (Q2 = 6mo − Q1; Q4 = FY − 9mo). A missing intermediate YTD does **not** silently produce
   a mislabeled 6-month "quarter" — the gap is withheld. Each derived quarter inherits the
   filing date of the YTD figure it was derived *from* (the discrete quarter wasn't "known"
   until that YTD was filed — no lookahead).

4. **POINT-IN-TIME AS-OF LAYER** (the whole point) — every fact carries its SEC **filing
   date**. `asof_quarterly(facts, ticker, concept, as_of)` returns, for any historical date,
   only facts actually filed by then, and for each period the value **as first filed** among
   the visible set. Two consequences, both proven below: (a) nothing filed after `as_of` can
   influence the answer; (b) a period restated later returns its **original** value for an
   as-of-then query. No restatement leaks backward into a backtest.

5. **OUTPUT** — a clean quarterly PIT table (parquet + CSV) for the resolved names, with
   derived quarterly & YoY sales growth, EPS growth, ROE, and gross/operating/net margins,
   all computed from as-first-filed figures.

---

## Coverage (honest count of the messy tail)

Of the **834** watch-list tickers:

| Status | Count | Meaning |
|---|---:|---|
| **Produced a clean quarterly table** | **644** | resolved + ≥1 canonical fact |
| Resolved to a CIK | 728 | direct 635 + rename-mapped 9 (renames still parse) |
| ETFs/indices (excluded by nature) | 9 | ARKK, IBIT, IWM, RSP, SIL, SMH, TQQQ, UFO, XLV |
| `unresolved` — not in current SEC map | 97 | delisted/renamed/foreign old tickers |
| `no_canonical_facts` — resolved but no 10-K/10-Q us-gaap | 81 | **foreign private issuers (20-F/IFRS)** |
| `no_facts_file` | 3 | BYDDF, IBN, MDA — ADR/foreign, no us-gaap facts file |

**644 / 825 non-ETF names = 78% produced clean US-GAAP quarterly fundamentals** out of the
box. The remaining tail is **understood, not fabricated**, and splits cleanly:

- **81 `no_canonical_facts` are almost entirely foreign private issuers** filing 20-F/40-F
  (annual, often IFRS taxonomy) rather than 10-K/10-Q: ASML, BABA, TSM, TM, SAP, SPOT, NVO,
  AZN, SE, PDD, NTES, RACE, UBS, and ~68 others. Confirmed firsthand (e.g. ASML/BABA/ARM all
  file only 20-F/6-K). Our pipeline deliberately filters to 10-K/10-Q; extending to 20-F
  (annual-only) is a defined Phase 2 item.
- **97 `unresolved` are the delisted/renamed survivorship tail** (TWTR, XLNX, VMW, SPLK, SGEN,
  DNKN, HZNP, ATVI-adjacent, etc.). Their fundamentals **still exist on EDGAR under the old
  CIK** — this is a CIK-resolution task (walk `formerNames`/historical `sub.txt`), not a data
  gap. Phase 1 shipped only a small hand rename map (9 names); the rest is a Phase-2
  auto-resolver.
- **3 `no_facts_file`** (BYDDF, IBN, MDA): no us-gaap companyfacts JSON at all (foreign/ADR).

**Quality of what resolved** (644 tickers, 76,524 quarterly rows, period-ends 1997→2026-05,
median 114 quarters/ticker): revenue populated **77.1%** of rows, EPS **76.5%**, equity
**90.1%**, YoY sales growth **53.2%** (YoY inherently needs a matched year-ago quarter, so the
first year of any company's data and any gap quarter can't have one). The unmapped-tag tally
confirms we are **not** silently dropping revenue/EPS — the top unmapped tags are sub-line
items we intentionally skip (pretax-income components, OCI, antidilutive-share counts, NCI),
not missed canonical concepts.

---

## Validation

### Spot-checks (discrete-quarter revenue & EPS, as-first-filed)

- **AAPL (large-cap):** FY2026 Q1 (quarter ending 2025-12-27) revenue **$143.756B**, diluted
  EPS **$2.84**; FY2024 Q1 (2023-12-30) revenue **$119.575B** — both match Apple's reported
  figures. YTD-differencing recovers Q2/Q3/Q4 correctly.
- **AAON (advisor small-cap):** FY2026 Q1 (2026-03-31) revenue **$496.936M**, +54.3% YoY;
  discrete quarters trace AAON's reported ramp.
- **ADMA (advisor small-cap):** FY2025 Q1 revenue **$114.802M** — cross-checked directly
  against the source 10-Q's native 3-month `RevenueFromContractWithCustomer…` fact
  ($114,802,000, filed 2025-05-07): **byte-exact match** to our table. EPS $0.11.

### As-of / no-lookahead proof (concrete)

Auto-discovered real restatement in the store — **Alcoa (AA), FY2015 revenue**:

```
first  filed 2017-03-15: value = 11,199,000,000   (as originally reported)
later  filed 2018-02-26: value = 10,121,000,000   (later restatement)

as-of 2017-03-16 (after original, before restatement):
    query returns 11,199,000,000   -> ORIGINAL. Restatement does NOT leak backward. ✓
as-of 2017-03-14 (before it was ever filed):
    period visible? False           -> correctly invisible. ✓
```

Independent large-cap check — **AAPL revenue as-of 2022-06-30:** 97 periods visible, latest
filing date among them 2022-04-29 (≤ as-of ✓), latest visible period-end 2022-03-26. Nothing
filed after the as-of date appears.

These guarantees are locked in by **8 pytest tests** (`tests/test_edgar_pipeline.py`, all
green) using synthetic fixtures: multi-era tag stitching, YTD differencing (values + the
derived-quarter filing-date rule + the no-subtract-across-a-gap rule), and four as-of
invariants (returns original not restatement; hides not-yet-filed periods; never shows a
future filing; prefers tag priority within the visible set).

---

## Honest state of the hard parts

- **Canonical mapping:** solid for US-GAAP 10-K/10-Q domestic filers. The per-period,
  multi-variant priority resolution is the right design and measurably lifted revenue
  coverage from 53% → 77% once multi-era stitching was added. Remaining gaps are sparse
  small-cap quarters and pre-XBRL-era holes, not systematic tag misses.
- **YTD→quarterly:** correct and defensive (gap-aware, filing-date-aware). Verified against
  both native 3-month facts and known reported quarters.
- **As-of / no-lookahead:** works and is demonstrated on a real restatement. This is the
  load-bearing anti-curve-fit guarantee and it holds.
- **Known limitation carried into the table:** discrete **Q4 EPS is null** where the filer
  reports only a full-year EPS in the 10-K (EPS isn't cleanly additive across quarters due to
  share-count drift, so we do NOT synthesize it — kept strictly as-reported). Revenue Q4 is
  recovered via FY−9mo differencing; EPS Q4 recovery is a deliberate Phase-2 decision.
- **Foreign filers (20-F/IFRS) are out** by design in Phase 1 — the single biggest coverage
  chunk to add next.

---

## Phase 2 — what remains to go full US market

1. **Universe from a survivorship-free source, not the current ticker map.** The current
   `company_tickers.json` lists only active filers. Full-market + delisted coverage needs an
   **old-ticker→CIK auto-resolver**: walk `formerNames` in `submissions.zip`, and/or build a
   historical ticker map from past `sub.txt` files. This alone recovers the 97 `unresolved`
   survivorship names (TWTR, XLNX, VMW, SGEN, SPLK…) whose facts already sit on EDGAR.
2. **Scale ingestion to all ~20k companyfacts JSONs** (the zip already contains them; Phase 1
   only parsed the 728 resolved watch-list CIKs). Straightforward — same parser, iterate the
   whole zip. Expect ~30–60M raw facts and a larger parquet; partition by CIK or year.
3. **Foreign private issuers:** extend the form filter to **20-F/40-F** and add an **IFRS
   taxonomy** map (`ifrs-full:Revenue`, etc.) alongside us-gaap, with non-USD unit handling.
   Recovers the 81 `no_canonical_facts` (ASML, BABA, TSM, TM, SAP, SPOT, NVO…).
4. **Discrete Q4 EPS** — decide the convention (leave null vs. FY−9mo-EPS differenced with a
   documented caveat) and the ROE window (currently crude 4×quarterly; move to TTM net income
   over average equity).
5. **Restatement/amendment coverage hardening** — we already key on `filed` and keep every
   version; add explicit handling of 10-K/A, dimensional/segment member filtering at scale,
   and unit/scale sanity checks (the top unmapped-tag audit already shows no revenue/EPS
   leakage, but full-market will surface more custom small-cap extension tags to triage).
6. **Freshness** — Phase 1 is a one-time bulk snapshot. Full-market operation needs an
   incremental refresh (daily `xbrl` index or per-CIK companyconcept polls) if this becomes a
   live feed rather than a historical backtest input.

Nothing here changes the two proven load-bearing pieces (canonical mapping + as-of
no-lookahead); Phase 2 is coverage breadth, not a redesign.
