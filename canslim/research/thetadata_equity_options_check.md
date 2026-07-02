# ThetaData equity (single-stock) options — availability + pull size/time check

_Verification for the CAN SLIM options-overlay backtest (2026-07-02). Purpose: the
options_overlay backtest currently uses **modeled Black-Scholes prices**, not real
fills (see options_overlay.md "Hard limits"). This checks whether we can replace
those with REAL historical equity-option quotes from ThetaData, and what it costs
in disk + time. All numbers below are measured through the live local Terminal, not
estimated from memory._

## Access verdict — YES, available now, no upgrade needed

- **Our tier already includes single-stock equity options.** Config comment
  (`datacollector/config.py`) documents the subscription as **ThetaData "Options
  Standard" — $80/month, 8 years of history, unlimited requests.** The warehouse
  universe already lists single-name roots (NVDA, AAPL, TSLA, …), and the live test
  below pulled real equity-option EOD data for NVDA / AXON / TSLA / PLTR / ELF /
  etc. through the running Terminal — **no entitlement error.**
- **How we talk to it:** local ThetaData **v3** Terminal (Java jar) serving REST on
  `http://127.0.0.1:25503/v3`. Client = `datacollector/thetadata_client.py`
  (`eod_greeks`, `eod_open_interest`). The API key lives only in the Terminal
  (off-Drive secrets); our code just makes localhost HTTP GETs and parses CSV.
- **Endpoints that give us everything the overlay needs, per option per day:**
  - `/option/history/greeks/eod` → **bid, ask, close/OHLC, delta, gamma, all
    greeks, implied_vol, AND underlying_price (spot)** — verified 43 columns.
  - `/option/history/open_interest` → per-strike **open interest**.
  - Both keyed on (symbol, expiration, strike, right); joined per day.
- **This directly retires the spec's biggest caveat.** The overlay's "friendly
  upper bound" disclaimer (modeled prices, no bid/ask, one flat IV, no skew) can be
  replaced with real quotes: real bid/ask spread + real per-name IV surface. This
  is the honest validation the spec explicitly asks for before trusting the result.

## Test query result — WORKED (real data returned)

Measured live through the existing Terminal (tiny, read-only; collector untouched):

| Query | Status | Rows | Wire bytes | Wall |
|---|---|---:|---:|---:|
| NVDA greeks EOD, 1 day, all-exp, CALLS | 200 | 2,570 | 818 KB | 1.24 s |
| NVDA open_interest, 1 day, all-exp, CALLS | 200 | 2,607 | 163 KB | 2.45 s |
| NVDA greeks EOD, 1 day, all-exp, BOTH (C+P) | 200 | 5,140 | 1.63 MB | 0.91 s |
| NVDA greeks, **2023-01-03** (history depth) | 200 | 1,139 | 355 KB | 0.71 s |
| NVDA greeks, **2022-01-03** (extra depth) | 200 | 1,513 | 478 KB | 0.37 s |
| AXON greeks, 2024-01-02 | 200 | 269 | 84 KB | 0.18 s |

- **History depth covers the need.** 2023 present (2022 too) — the 2023-2026 window
  is fully available. (Config notes single-name history generally starts ~2020 on
  the CTA tape; our 55 names should mostly be covered, but a couple of the newest
  IPOs/ADRs may start later — verify per name at pull time, not a blocker.)
- **Constraint:** when `expiration=*`, the API **requires one day at a time** (400
  otherwise). That's exactly the existing `download.py` pull shape (per root, per
  business day, two GETs) — so we reuse it as-is.
- **The API returns the FULL chain regardless of strike/expiry filter.** Our
  ATM±20% / ≤9mo band is applied client-side after download. For NVDA one day the
  band is only ~22% of rows — but we still download 100% over the wire, so the band
  reduces *stored* size, not *download* size or time.

## Full-pull size estimate (~55 names, EOD, ~3 yr 2023-2026)

Measured average payload per name-day across a 10-name spread (NVDA/TSLA/AAPL/PLTR/
AXON/ELF/CROX/VKTX/IONQ/MOD), CALLS only: **greeks ≈ 317 KB + OI ≈ 63 KB ≈ 380 KB
per name-day.** × 55 names × ~756 business days (3 yr) = **41,580 name-days.**

| Scope | Raw CSV over wire | On-disk parquet (zstd ~4–5×) |
|---|---:|---:|
| **CALLS only** | **~16 GB** | **~3–4 GB** |
| Calls + puts (~2× rows) | ~32 GB | ~6–8 GB |

- **Puts are cheap to add relative to the effort:** roughly **doubles** the pull
  (they come in the same request with `right=both`, so no extra requests — just
  ~2× bytes and ~2× stored size). Worth grabbing puts in the same pass so we never
  re-subscribe to add them (same logic the warehouse already uses).
- Disk lands comfortably on `C:\TradingDesk-Local\warehouse` (the existing SPXW
  1-min feed alone is ~29 GB; this is a fraction of that).

## Download-time estimate + parallelization

- **~83,000 requests** total (2 per name-day). Measured avg ≈ **0.45 s/request**.
- **Serial: ~10 hours.** Not viable to babysit in one sitting, but it's resumable
  (one file per name-day; re-run skips what's on disk), so serial is *safe* even if
  slow.
- **Parallelizing HELPS — the Terminal serves concurrent requests.** Measured probe
  (4 identical requests): serial 1.52 s vs 4-way parallel 0.43 s = **3.5× speedup.**

| Workers | Effective speedup (measured/extrapolated) | Est. wall time |
|---:|---|---:|
| 1 (serial) | 1× | ~10 h |
| 4 | ~3.5× (measured) | **~3 h** |
| 8 | ~5.5× (extrapolated) | ~1.9 h |
| 12 | ~7× (extrapolated) | ~1.5 h |

- **Recommendation: 4–6 workers.** 4 is measured-safe and lands the whole pull in
  ~3 h. Beyond ~8 you hit diminishing returns (localhost CSV serialization + the
  collector sharing the box) and rising contention risk — not worth it. The tier is
  "unlimited requests," so there's no per-account request cap forcing us serial; the
  ceiling is the single local Terminal's throughput, which is shared with the SPX
  collector.

## EOD (what we'd pull) vs intraday (reference only — do NOT pull)

- **EOD is the light product and all the overlay needs** — one row per option per
  day. Whole 55-name calls+puts pull ≈ **6–8 GB on disk, ~2–3 h at 4 workers.**
- **Intraday 1-min would be ~390× the rows** → on the order of **1.5–2.5 TB** for
  the same universe/window, plus far longer download and real contention with the
  live collector. Not needed: the overlay is a daily/swing model (entries at pivot,
  delta-trigger checked EOD). **Do not pull intraday.**

## Operational caveats (collector safety)

- The verification here was a handful of tiny read-only GETs through the **existing**
  Terminal — nothing restarted, reconfigured, or stopped; collector and scheduled
  tasks untouched.
- **The real pull shares the one local Terminal with the self-healing SPX collector.**
  Run the equity pull **off-hours** (outside the collector's active EOD/forward
  windows) and keep workers modest (≤4–6) so it can't starve the collector. Do NOT
  raise Terminal concurrency settings or touch the collector/scheduled tasks to go
  faster.
- Pull is **idempotent + resumable** (reuse `download.py`'s per-name-day skip), so a
  stop/restart mid-pull is safe.
- Standard grab discipline applies: this is a **one-time bulk pull during the paid
  month**, held locally forever and extended forward for free via IBKR — same model
  as the index/SPX warehouse. No re-subscription.

## Bottom line

Equity options are **available on our current $80 Standard tier — no upgrade, no
extra cost.** Test query returned real NVDA/AXON EOD greeks+IV+OI back to 2023. The
55-name EOD pull is **~3–4 GB (calls) / ~6–8 GB (calls+puts) on disk**, downloadable
in **~3 h at 4 parallel workers** (~10 h serial). Parallelizing helps (~3.5× at 4
workers, measured); 4–6 workers is the sweet spot. This lets us re-run the
options-overlay backtest on **real historical quotes** instead of modeled BS prices —
the exact honest-validation step the spec flags as required.
