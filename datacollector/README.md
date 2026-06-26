# Options Data Warehouse

A one-time bulk grab of historical EOD option chains (ThetaData, paid month), held
locally forever, then extended forward for free via our own IBKR collector. Powers
dealer-gamma / market-structure overlays (the "MSR" work) and any future options-aware
strategy — **buy the history once, never re-subscribe.**

## The decision (why this shape)

- **Source:** ThetaData **Options Standard** ($80/mo, 8 yr history, all OPRA, unlimited
  requests). Index options (SPX/SPXW) and ETF options are all included. The EOD greeks
  response returns `underlying_price` inline, so we do **not** need the separate Stocks,
  Indices, or Interest-Rates products.
- **Granularity:** **End-of-day only.** Compact and sufficient for daily/swing overlays.
  Full intraday/tick is petabyte-scale and speculative — skip it; the forward IBKR
  collector covers live needs.
- **Universe (~36 roots):** curated for our themes (see `config.py UNIVERSE`) —
  index+vol (SPX, SPXW, VIX, NDX, RUT), broad equity, 11 sectors, credit (HYG/LQD/JNK),
  rates (TLT/IEF/SHY), gold/commodities. SPX/SPXW dominate storage; the rest is tiny.
- **Storage:** raw parquet **local** at `C:\TradingDesk-Local\warehouse` (~tens of GB; never synced to
  Drive). Small derived feature tables get copied back to Drive for backup. Code lives
  in Drive (this folder) and is version-backed.
- **Estimated size:** ~10–40 GB for the full grab (C: has ~186 GB free).

## Prerequisites (one-time, your side)

1. **Java 21+** — Terminal requires it. `winget install EclipseAdoptium.Temurin.21.JDK`
   then reopen the shell. (`start_terminal.py` auto-downloads the Terminal jar itself.)
2. ThetaData key — in `C:\TradingDesk-Local\secrets\.env` as `THETADATA_API_KEY` (outside Drive). ✅

## Run order

```
# 1. Start the local Terminal (REST gateway on 127.0.0.1:25503). Leave it running.
python start_terminal.py

# 2. In a second shell, pull data. Start small to validate, then go wide.
python download.py SPX            # smoke test one root
python download.py                # full universe (resumable; safe to stop/restart)
```

Everything below `C:\TradingDesk-Local\warehouse` is the warehouse. Query it ad-hoc via DuckDB:
`duckdb C:\TradingDesk-Local\warehouse\catalog.duckdb` then `SELECT * FROM options_eod LIMIT 5;`

## Files

- `config.py` — universe, paths, grab window, Terminal connection. Single source of truth.
- `start_terminal.py` — launches the Java Terminal with the key from `.env` (never printed).
- `thetadata_client.py` — REST client: `eod_greeks()`, `eod_open_interest()`.
- `storage.py` — parquet partitions + manifest + DuckDB catalog (resumable).
- `download.py` — the bulk orchestrator (per root, per year; greeks ⨝ open_interest).

## Status / next

- [x] Acquisition pipeline (this folder) — ready to run once Java is installed.
- [ ] **Verify response column names** against the live Terminal (join keys in
      `download.py _KEY_CANDIDATES`, date field) on the first `download.py SPX` run.
- [ ] GEX feature engine — net dealer gamma, gamma flip, distance-to-flip, expected
      move, skew — computed from the stored chains. (Runs anytime; no subscription clock.)
- [ ] Calibration harness — tune the computed features to match the 281-day Tier 1 Alpha
      newsletter set (`..\..\Tier 1 Alpha\Backtester Handoff\msr.db`) within margin.
- [ ] IBKR forward collector — append the same EOD features daily, for free.
