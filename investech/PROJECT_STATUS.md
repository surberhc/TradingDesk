# InvesTech Research Project — Status & Resume Guide

**Last updated:** 2026-07-10
**Status:** SHELVED 2026-07-10 — Andrew's call, not being pursued further. Kept for reference only; do not resume without explicit direction.

## STATUS UPDATE 2026-07-10 — SHELVED, not just on hold
Andrew decided to abandon this project entirely, not merely pause it. Decision + reason, verbatim intent: "We don't need it." Everything below this section is the historical record as of the 2026-06-30 on-hold snapshot, preserved as-is; this section documents the final day of work that preceded the shelving call.

What happened today before the shelve decision:
1. Andrew asked for an honest time estimate to finish Phase 2's calibration backfill (RESUME HERE step 3 below: >=2yr EOD history for the universe).
2. Found the ThetaData Terminal actually running (port 25503) serves **API v3 only** — the v2 API `phase2_feed\thetadata.py` was written against is fully deprecated/rejected ("upgraded to API v3" errors). Not a port mismatch — the whole client needed porting.
3. Ported `phase2_feed\thetadata.py` and `config.py`'s `THETADATA_BASE_URL` default from v2 to v3 — committed `3441701`. Verified: health check now `/v3/system/mdds/status`; ticker list now `/v3/stock/list/symbols` (CSV, 26,225 tickers in ThetaData's full unfiltered universe, incl. lots of delisted/garbage tickers); EOD history now `/v3/stock/history/eod?symbol=X&start_date&end_date` (CSV, date taken from the `last_trade` column — no more plain `date` field).
4. Ran a REAL measured timing test (not a guess): 50 tickers, 360-day window, sequential/no threading = **155.8s total, 3.12 sec/ticker avg**, 32/50 returned data, 18/50 genuinely empty (obscure/delisted), zero errors/crashes.
5. Extrapolated from that sample (not independently measured at full scale): ~500 tickers (S&P-500 scope) ≈ 26 min for one 360-day window (~52 min for a full 2yr history, 2 chunked requests/ticker); full ~26,225-ticker unfiltered universe ≈ ~22.7hr for one window (~45hr for 2yr).
6. Found (did not fix) a real bug: `config.TIINGO_LOOKBACK_DAYS = 420` exceeds the new v3 365-calendar-day-per-request cap — a default-window ThetaData call would silently 400 and fall back to Tiingo, quietly defeating the point of using ThetaData. Left unfixed; project is shelved.
7. Given those numbers, Andrew decided to shelve the whole project rather than run the backfill. Stated reason: "We don't need it."

## TL;DR — where we are
InvesTech newsletter analysis for the TFR Trading desk. The **dataset** and the **Phase 1 feed** are DONE and live. **Phase 2** (breadth proxy) is built and scheduled, but its signal is **not yet calibrated/validated** — blocked on a bulk market-data source (ThetaData). To resume: start the ThetaData Terminal and follow "RESUME HERE" below.

## What InvesTech is
James B. Stack's risk-first, valuation-capped, technically-triggered defensive market-timing newsletter (Whitefish, MT). Risk dial = published Model Fund Portfolio **net equity exposure** (held a tight **45–61%** band across 2023–2026; never risk-on) plus **cash %**. Master trigger = **Negative Leadership Composite (NLC)**: Selling Vacuum (bullish, needs >+20 to confirm) vs Distribution (bearish, −100 = max danger). Trades execute via intra-month "Hotlines". Tracks but refuses to own mega-cap leadership (proprietary Gorilla & AI indices).

## Deliverables & locations (under C:\TradingDesk\investech\)

### _dataset\ — DONE
- `InvesTech_Dataset.xlsx` — 5 sheets: Signals, Holdings_Matrix, Changes_Log, Symbol_Legend, ReadMe
- `InvesTech_Signals.csv` — 42 monthly rows (net equity %, cash %, NLC value/regime, S&P last, P/E, Fed Funds, 30yr, gold, stance, theme)
- `InvesTech_Holdings_long.csv` — tidy/long holdings
- `InvesTech_Backtest.csv` + `InvesTech_Backtest_Summary.txt`
- All 42 monthly holdings validated to sum to 100%. Source = 42 newsletter PDFs in the 2023/ 2024/ 2025/ 2026/ folders.

### phase1_feed\ — LIVE + SCHEDULED
Public valuation/concentration daily feed; 5 metrics, all live:
- S&P trailing P/E (multpl.com), CAPE/Shiller (multpl.com), Buffett Indicator (FRED `NCBEILQ027S`/`GDP`, quarterly), Household equity allocation (FRED `BOGZ1LM193064005Q`/`TFAABSHNO`, quarterly), Top-10 concentration (SPY holdings .xlsx).
- `env_loader.py` auto-loads FRED_API_KEY from C:\TradingDesk-Local\secrets\.env.
- Output: `data\metrics_daily.csv` (1 row/day, de-duped).
- Scheduled: Windows Task **"InvesTech Phase1 Feed"**, daily **22:30** local, runs `run_feed.cmd` (batch; NO PowerShell — user denies PowerShell).
- NOTE: the old Wilshire 5000 FRED series are discontinued; correct Buffett numerator is `NCBEILQ027S`.

### phase2_feed\ — BUILT + SCHEDULED, signal NOT yet validated
Breadth-based "NLC-like" leadership proxy — a transparent APPROXIMATION, not a clone of the proprietary NLC.
- Metrics: % above 50/200-day MA, net new 52wk highs/lows, advance/decline + cumulative A/D line → 0–100 composite + regime.
- Data: Tiingo EOD with on-disk cache (`data\cache\<TICKER>.csv`). `config.UNIVERSE_LIMIT=None` (full ~503 S&P 500).
- Scheduled: Windows Task **"InvesTech Phase2 Breadth"**, daily **23:00** local, `run_feed.cmd` (batch).
- ThetaData integration: `thetadata.py` prepared (live verification PENDING the Terminal — see RESUME). Exact verify command is in `phase2_feed\README.md`.
- Calibration: `calibrate.py` + `calibration_report.md` / `calibration_results.csv` — PRELIMINARY/INCONCLUSIVE (see Findings).

## Key findings
- **Backtest verdict:** InvesTech's stance is a CONTRARIAN, coincident-to-lagging sentiment overlay — NOT a forward predictor. Net Equity vs forward S&P r≈−0.3; NLC vs forward-6mo r≈−0.64; after "Distribution" signals the S&P rose MORE (+6.0% vs +3.6% fwd 3mo). Caveat: 42 overlapping monthly points in one bull regime — not statistically significant. Treat as a fade/sentiment input, not timing.
- **No InvesTech API:** subscriber-login chart-only; ToS + robots.txt prohibit scraping; license-only. Daily indicators post ~45 min after close (preliminary), final by 8 PM Mountain Time.
- **Tiingo key is effectively FREE-TIER (~50 req/hr)** — too thin for 503-name daily breadth or 2yr history.
- **Exchange breadth (true NYSE/NASDAQ A-D + new highs/lows) WIRED BUT STUBBED** — needs the ThetaData Terminal (no cloud REST API; Stooq endpoints behind a bot-wall).
- **Phase 2 calibration inconclusive:** only 5/42 NLC dates computable from the 1yr cache, 1/5 agreement, no honest tuning possible → `config.py` left UNTUNED. Root cause: the proxy uses S&P-500 large-cap breadth, but InvesTech's NLC uses full NYSE+NASDAQ exchange breadth — a different universe. Also needs a smoothed A/D-line slope (1-day A/D swings ±45 pts) and >=2yr history.

## RESUME HERE (when the ThetaData Terminal is free)
**NOTE 2026-07-10: project is SHELVED — do not resume any of the steps below without explicit new direction from Andrew.** Steps retained as-is in case this is ever revisited.
1. Start the ThetaData Terminal and log in (serves http://127.0.0.1:25510). Confirm reachable: `curl http://127.0.0.1:25510/v2/system/mdds/status`.
2. Verify the ThetaData integration using the command in `phase2_feed\README.md` — confirm `is_terminal_up()` is True and a sample EOD pull works.

```cmd
cd /d "C:\TradingDesk\investech\phase2_feed"
set PHASE2_DATA_SOURCE=thetadata
set PHASE2_UNIVERSE_SOURCE=thetadata
set PHASE2_THETADATA_UNIVERSE_LIMIT=50
"C:\Python314\python.exe" main.py
```
3. Backfill >=2 years of EOD history for the universe into `data\cache\` (ThetaData has no Tiingo-style hourly cap). Consider expanding from the S&P 500 to a broad NYSE/NASDAQ universe to match InvesTech's NLC.
4. Light up true exchange breadth (`fetch_exchange_breadth`) across the broad universe.
5. Re-run calibration: `python calibrate.py` against `_dataset\InvesTech_Signals.csv` across all 42 dates. Tune regime cutoffs + sub-score weights; replace the 1-day A/D term with a smoothed A/D-line slope. Apply tuned params to `config.py` only if they beat a naive baseline out-of-sample.
6. Optionally confirm/upgrade the Tiingo plan as a fallback bulk source.

## Scheduled tasks (Windows Task Scheduler; run as andre, logged-on, no admin)
- **"InvesTech Phase1 Feed"** — daily 22:30 — `phase1_feed\run_feed.cmd`  (KEEP RUNNING — accumulates clean valuation/concentration history)
- **"InvesTech Phase2 Breadth"** — daily 23:00 — `phase2_feed\run_feed.cmd`  (runs on S&P-500 breadth only, Tiingo-throttled, signal not yet calibrated)
- To pause either while on hold: `schtasks /change /tn "<task name>" /disable` (re-enable with /enable).

## Secrets
C:\TradingDesk-Local\secrets\.env — FRED_API_KEY, TIINGO_API_KEY, THETADATA_API_KEY. Loaded via `env_loader`; never printed or committed.

## Claude memory (session continuity)
C:\Users\andre\.claude\projects\H--My-Drive-TFR-Ops-Research-InvesTech\memory\ — `investech-research-project.md`, `investech-data-caveats.md`, `MEMORY.md`.

## Constraints to remember
- User DENIES PowerShell — use batch/.cmd only for any Windows automation.
- Persistent/scheduled tasks require explicit user authorization each time.

---

After writing the file, confirm it exists and report back its path + byte size, and the line count. Do not alter the content.
