# Full-Market CAN SLIM SELECTION backtest — PLAN & progress

Re-scoped 2026-07-02 per Andrew's locked decisions. This supersedes the earlier
"optionable large/mid-cap first" scoping, which was WRONG (small caps are the CORE of
CAN SLIM and must not be filtered out).

---

## SCOPE (locked)

- **UNIVERSE = the FULL survivorship-free US universe INCLUDING SMALL CAPS.** Every
  EDGAR-covered company with clean point-in-time fundamentals over the window, PLUS delisted /
  renamed names recovered via `edgar_resolver`. **No market-cap floor. No dollar-volume floor.
  No optionable filter.** The optionable large/mid-cap transplant + options-de-risk overlay is
  a **SEPARATE ADDITIVE experiment for later** — reported as an ADD-ON, never used here as a
  filter or gate.
- **WINDOW = 2010–2026.** (EDGAR XBRL small-cap coverage is complete from ~2011–2012; 2010 is
  the practical start. Fundamentals exist to 2009 but are large-filer-only before ~2011.)
- **NO external timing / cash dial anywhere.** Whether the strategy sits in cash or invested
  must **EMERGE** from (valid setups passing the screen) + (position stops) — never from any
  market-level signal. This is a structural property, enforced by design (the universe/join
  layer decides only WHO is eligible, never HOW MUCH to hold).

### SUCCESS BAR (LOCKED — verbatim)

> "over 2010-2026 on the full universe incl. small caps, the mechanical picks beat
> buy-and-hold (SPY/QQQ) on a risk-adjusted basis and hold up through choppy/sideways tapes,
> with ZERO parameter tuning; reported per-year/per-regime. Bear = a sanity-check that it
> correctly goes to ~cash (structural defense), NOT a hurdle. Optionable large/mid-cap
> transplant + options-de-risk reported as an ADD-ON, never a filter/gate."

---

## GUARDS (non-negotiable, per CLAUDE.md rule #1)

- **No curve-fitting.** All screen/universe knobs are FROZEN and public. The three universe
  inclusion criteria below are principled and untuned; changing any is a parameter change
  requiring Andrew's blessing.
- **Point-in-time / no-lookahead throughout.** Prices never use a bar after the decision date;
  fundamentals never use a filing FILED after the decision date (keyed on `filed`, so later
  restatements are invisible until their actual filing date). Enforced by
  `tests/test_full_market_join.py` (leak-bait restatement fixture).
- **Honest coverage reporting.** Every symbol the free sources cannot cover is counted in a
  miss ledger, never silently dropped.

---

## THE FROZEN UNIVERSE RULE (minimal, principled, untuned)

A `(CIK, ticker)` is a universe MEMBER for calendar year Y if, using ONLY data known as-of
the membership date `Y-01-01`, ALL of:

1. **Has clean EDGAR fundamentals whose FILING DATE ≤ Y-01-01** — it was a live, reporting US
   filer as of the membership date. (This is what makes membership survivorship-free and
   point-in-time: a name is a member exactly for the years it was live-and-reporting, then
   drops out when its filings end at delisting.)
2. **Has tradable daily price/volume history ending near Y-01-01** (≥ 20 of the trailing ~63
   trading days present) — it was actually trading.
3. **Median price ≥ $1** over that trailing window — a nominal defunct-shell floor (the
   universal exchange minimum-bid rule), NOT a cap or quality gate.

Deliberately **NO** market-cap floor, **NO** dollar-volume floor, **NO** optionable filter —
small and micro caps stay IN. `canslim/full_market_universe.py` holds the frozen constants.

---

## BUILD PHASES

### Phase 1 — Universe + survivorship-free price/volume leg + leak-free join  ← THIS PHASE

| Leg | Module | Status |
|---|---|---|
| Full survivorship-free universe (CIK-keyed, incl. small caps + delisted) | `full_market_universe.py` | **DONE** — 14,946 CIKs, 16,725 candidate symbols |
| CIK↔ticker↔date timeline (leak-free join key, point-in-time ticker) | `full_market_universe.py :: build_timeline` | **DONE** — 20,273 rows, 0 symbol gaps |
| Point-in-time membership by (CIK, ticker, year) | `full_market_universe.py :: build_membership` | **DONE** (recomputes as prices land) |
| Survivorship-free daily OHLCV+volume pull (2010-2026), resumable | `full_market_prices.py` | **IN PROGRESS** — ~49/16,725 symbols; resumable |
| Leak-free CIK↔ticker↔date JOIN accessor | `full_market_join.py` | **DONE** — leak-bait tests green |

### Phase 2 — Ratings (self-computed, $0, approximations — NOT licensed IBD)  ← NEXT

- **RS Rating (1–99)** from prices: `2*(C/C₆₃) + (C/C₁₂₆) + (C/C₁₈₉) + (C/C₂₅₂)`, then
  percentile-rank across the **survivorship-inclusive universe on each historical date** (this
  is why the delisted price leg matters — ranking against survivors only biases percentiles).
- **EPS Rating (1–99)** from EDGAR: YoY quarterly EPS growth (recent-weighted) + stability,
  percentile-ranked. (Already have `eps_growth_yoy` in the fundamentals table.)
- **Composite (IBD-inspired, explicitly not IBD parity)**: EPS + RS heaviest; SMR from
  fundamentals; 52-wk-high distance from prices.

### Phase 3 — The selection backtest itself

- Reuse the proven detector + execution engine (`base_detector.py`, `execution_engine.py`,
  `selection_backtest.py`) but scan the FULL universe each date instead of the advisor watch
  list. Exposure emerges from setups + stops (no timing dial). Report per-year and per-regime
  vs SPY/QQQ on a risk-adjusted basis; bear = sanity-check it goes to ~cash.

### Later (ADD-ON, never a filter) — optionable large/mid-cap transplant

- Restrict a SEPARATE run to optionable large/mid-cap names and layer the options-de-risk
  overlay. Reported alongside the full-universe result as an add-on, never gating it.

---

## DATA SOURCES (free-first; minimize Gateway contention)

Source order, as locked:

1. **Stooq bulk** (delisted backbone) — the intended whole-universe path IF a bulk archive is
   on disk. **VERIFIED 2026-07-02: Stooq is no longer scriptable headless** — the per-ticker
   CSV endpoint returns a JS/anti-bot interstitial and the bulk-download page returns HTTP 401
   (login-gated). `full_market_prices.py` will USE a manually-placed Stooq bulk drop
   (`C:\TradingDesk-Local\canslim\stooq_bulk\`) if present, else SKIPS Stooq cleanly. **Open
   item:** obtain a Stooq bulk archive (account/manual) to backfill delisted-before-2010 and
   obscure micro-caps Tiingo misses.
2. **Tiingo free tier** — the working headless source (full OHLCV + adjClose/adjVolume + split/
   div factors; retains delisted permaTickers). **This is the primary puller.**
3. **Alpha Vantage** — no key on this machine; `LISTING_STATUS` (delisted universe list) is a
   future nicety, not required (EDGAR already gives us the survivorship-free universe).
4. **IBKR gap-fill** — READ-ONLY `reqHistoricalData` for free-source misses only. clientId
   **43** (`canslim_price_gapfill`), takes the Gateway mutex (`paperbot.gateway_lock`,
   `on_busy='skip'`) so it YIELDS to `AccountMonitorDaily`/rebalance. **Contract stubbed** in
   `full_market_prices.py :: ibkr_gapfill_stub`; run deliberately off-hours, not from the pull.

### The binding constraint (honest)

Tiingo free tier caps at ~50 req/hr, ~1,000 req/day, and **500 unique symbols/month**.
Observed 2026-07-02: a run got HTTP 429 after ~90 requests in ~2 min, so the effective
sustained rate is well below the nominal cap. A 16,725-symbol universe therefore spans
**many weeks** of daily runs. The puller is built for exactly this: run daily, it skips
what's done and resumes; a 429 stops it cleanly with state saved.

**To materially speed this up:** a one-month paid Tiingo tier (~$30–100) lifts the symbol cap
and would let the whole universe pull in days instead of weeks — a deliberate, cheap
time-buy Andrew can green-light. Or obtain a Stooq bulk archive (one file, whole universe).

---

## WAREHOUSE LAYOUT (local only, never on Drive)

```
C:\TradingDesk-Local\canslim\
  edgar\quarterly_fundamentals_full\shard=*.parquet   # point-in-time fundamentals (DONE)
  universe\cik_ticker_timeline.csv                     # CIK<->ticker<->date join key (DONE)
  universe\candidate_tickers.csv                       # 16,725 symbols to price (DONE)
  universe\universe_membership.csv                     # point-in-time membership (recomputed)
  universe\universe_counts_by_year.csv                 # membership counts by year
  prices\<SYMBOL>.parquet                              # daily OHLCV+adj (IN PROGRESS)
  prices\_state\heartbeat.json | misses.csv | pull_log.txt
  stooq_bulk\                                          # optional manual Stooq bulk drop
```

Only CODE + this plan live in the Drive repo. The universe `.csv`s are force-added to git
(they are small, principled reference data / the compliance trail for what was scanned).

---

## PROGRESS LOG

- **2026-07-02** — Re-scoped from optionable-large/mid to FULL universe incl. small caps.
  Rewrote `full_market_universe.py` (dropped $2B cap + $10M $-vol filters → $1 price floor +
  tradability only). Built full universe: **14,946 CIKs with clean fundamentals, ALL
  resolvable to a ticker (0 symbol gap), 16,725 candidate symbols.** Wrote resumable
  `full_market_prices.py` (Tiingo primary, Stooq-bulk-if-present, IBKR-gapfill stub) and
  leak-free `full_market_join.py`. Added `test_full_market_join.py` (restatement leak-bait
  fixture) — green. Full canslim suite green (55 passed). Price pull started; **~49/16,725
  symbols on disk**, resumable (Tiingo rate-limited fast — many days to complete).

### Universe size by fundamentals-live year (full, incl. small caps)

Distinct CIKs with a clean quarterly filing whose period ends in each year:

| Year | CIKs | Year | CIKs | Year | CIKs |
|---|---|---|---|---|---|
| 2010 | 8,032 | 2016 | 7,115 | 2022 | 7,064 |
| 2011 | 7,856 | 2017 | 6,795 | 2023 | 6,551 |
| 2012 | 8,253 | 2018 | 6,509 | 2024 | 6,162 |
| 2013 | 8,152 | 2019 | 6,423 | 2025 | 5,834 |
| 2014 | 7,828 | 2020 | 6,620 | 2026 | 5,133 |
| 2015 | 7,542 | 2021 | 7,047 | | |

(These are the survivorship-free reporting-filer counts; the price-gated membership numbers
will be a subset once prices for all 16,725 symbols land.)

---

## NEXT CHUNK (precise)

1. **Finish the price pull** — run `full_market_prices.py` daily (resumable). Consider a
   one-month paid Tiingo or a Stooq bulk drop to compress weeks → days.
2. **Phase 2 — ratings**: build RS Rating (percentile-ranked across the survivorship-inclusive
   universe per date) + EPS/Composite ratings off the join layer.
3. **Phase 3 — the selection backtest** on the full universe, per-year/per-regime vs SPY/QQQ,
   zero tuning; then the optionable+options ADD-ON run.
