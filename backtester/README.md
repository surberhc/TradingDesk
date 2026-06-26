# Adaptive All-Weather Core Backtester

In-house research tooling for a rules-based, multi-engine tactical asset-allocation
strategy. Reads local historical ETF prices, runs the strategy month by month, and
produces an HTML results report. Not connected to any brokerage; places no live trades.

See **CLAUDE.md** (session rules), **SPEC.md** (strategy logic — source of truth),
and **DATA.md** (data download spec).

## Where things live (relocated 2026-06-24 for backup)
- **Project (this folder) lives in Google Drive** → backed up / synced:
  `C:\Users\andre\My Drive (andrew@surberhc.com)\TradingDesk\backtester`
- **Virtual environment is LOCAL, outside Drive** (avoids syncing 320 MB):
  `C:\TradingDesk-Local\venv` — run with `C:\TradingDesk-Local\venv\Scripts\python.exe`
- **API key** = `TIINGO_API_KEY` Windows user env var (secret kept off Drive). No `.env` in Drive.
- Run the report: `cd` here, then `"C:\TradingDesk-Local\venv\Scripts\python.exe" -m src.run`
- **PENDING CLEANUP:** delete the stale old local copy `C:\Users\andre\backtester` once the
  `TIINGO_API_KEY` env var is set and this Drive copy is confirmed working. (Left in place
  for now as a safety net — it still holds the original `.env`.)

## Status log

_Update this section after each meaningful step so a fresh session can pick up where the last left off._

### 2026-06-24 — Project skeleton scaffolded
- ✅ Planning docs written: CLAUDE.md, SPEC.md, DATA.md.
- ✅ `.venv` virtual environment created (Python 3.14).
- ✅ `requirements.txt` with pinned versions (pandas, numpy, requests, plotly, jinja2, python-dotenv, pyarrow, pytest).
- ✅ `.gitignore` excludes `.env`, `data/`, `.venv/` (+ Python/editor cruft; keeps `output/` folder).
- ✅ `config.py` — central home for every tunable parameter from SPEC.md.
- ✅ Folder layout created with skeleton/placeholder files: `src/`, `src/engines/`, `tests/`, `data/`, `output/`.
- ⛔ No engine logic written yet — all `src/` functions raise `NotImplementedError`; all tests are `@pytest.mark.skip`.

### 2026-06-24 — Environment rebuilt + data layer live
- ✅ Old Python 3.14 `.venv` (no wheels for pinned deps) deleted; rebuilt on **Python 3.12.9** (`py -3.12 -m venv .venv`).
- ✅ `requirements.txt` installed clean — all cp312 wheels, no source builds. Versions match the pins (pandas 2.2.3, numpy 2.1.3, plotly 5.24.1, pyarrow 18.1.0, etc.).
- ✅ `src/download_data.py` built to DATA.md and **first data pull done**: 28 universe tickers + HYG (credit proxy) as Parquet in `data/`, plus `_manifest.json`. Reads `TIINGO_API_KEY` via env var only (never printed). Console forced to UTF-8 so cmd's cp1252 can't crash it.
- ✅ **10y Treasury yield = REAL source** (US Treasury daily par-yield, 4120 rows) — not the IEF proxy. Downloader falls back to a labeled IEF-trend proxy only if that public source fails.
- ✅ `src/data_loader.py` built: wide adjusted-close frame (4142 × 28), inception-aware (pre-inception cells are NaN, never forward-filled). Helpers: `load_prices`, `load_treasury_10y`, `load_manifest`, `inception_dates`.
- ✅ `tests/test_data_loader.py` — 6 tests pass (inception-awareness, positive prices, sorted/unique dates, real yield source).
- ⚠️ QC flagged 5 cash-like ETFs (SGOV, BIL, VGSH, USFR, TFLO) for "stale price runs" — **expected and benign** for ultra-short T-bill/floating-rate funds (flat adjClose). No critical errors (no zero/negative prices, no bad splits). Safe to backtest.
- Inception dates of note: XLC 2018-06-19, GLDM 2018-06-26, SGOV 2020-05-28, XLRE 2015-10-08, PDBC 2014-11-07, USFR/TFLO 2014-02-04.

### 2026-06-24 — Regime engine built (engine 1 of 6)
- ✅ `src/engines/regime.py` (SPEC §4) — **Market Health Score (0-100)**, three equal-weight components:
  - **Trend** (SPY): above 200d MA, above 10-month MA, positive 6m return, positive 200d OLS slope.
  - **Breadth**: % of sectors above 200d MA (inception-aware — absent sectors excluded, never counted "below") + RSP/SPY leadership trend.
  - **Stress** (LABELED PROXIES per DATA.md): credit = HYG/IEF ratio vs its 200d trend; volatility = SPY 63d realized vol vs its 200d trend (VIX proxy).
  - `classify_regime` → bands from `config.REGIME_BANDS`; `equity_band`; `apply_hysteresis` (confirmation buffer, 3-pt dead-zone, >10-pt immediate de-risk, no instant re-risk).
  - All windows are TRAILING → causal by construction. Rate/inflation inputs deliberately excluded (those go to duration.py, §6).
  - All tunables read from existing `config.py`; only `TRADING_DAYS_PER_MONTH=21` (a units conversion) is a module constant.
- ✅ `tests/test_regime.py` — **20 tests pass**, including the no-look-ahead truncation test (SPEC §16: score at T recomputed on data truncated at T matches).
- ✅ Real-data sanity check: correctly de-risked to CapitalPreservation in 2018-Q4 (score 10), 2020 COVID (8), 2022 bear (20); Risk-On/Narrowing through calm bulls. Full suite: 26 passed, 8 skipped.

### 2026-06-24 — Duration engine built (engine 2 of 6)
- ✅ `src/engines/duration.py` (SPEC §6, "the edge") — `duration_signals()` (daily, causal) + `duration_decision(row, regime)`:
  - **5 permission rules** (TLT trend, +3m return, beats T-bills 3m, yield flat/falling, drawdown ok); `long_allowed` = passes ≥ `config.LONG_TSY_PERMISSION_MIN_PASSES` AND not banned.
  - **6 ban rules** (broken trend, yield up+rising, T-bills winning 3/6m, stocks+bonds both down, inflationary-bear, drawdown beyond −10%).
  - **Inflationary-bear filter** (2022 guard, majority of 5) → bans long, caps intermediate at `config.INFLATIONARY_INTERMEDIATE_CAP`. **Deflationary-panic filter** (majority of 5) → allows intermediate + long-if-permitted.
  - Per-bucket caps from `config.DURATION_CAPS` by regime, with filter overrides; safe defensive default for unknown regime. "Deflationary character" confirmation enforced via the bans (documented).
  - Uses the real 10y par yield from `data/`; falls back to a labeled negated-IEF proxy if absent.
- ✅ `tests/test_duration.py` — **7 tests pass** (permission earned, ban on broken trend, inflationary-bear caps, regime cap table, no-look-ahead truncation).
- ✅ Real-data sanity: **2022 inflationary-bear active all 12 months → long banned** (the key case); long allowed 75% of 2019 and 58% of 2020 (deflationary bond bulls). Full suite: 33 passed, 7 skipped.

### 2026-06-24 — Defensive engine built (engine 3 of 6)
- ✅ `src/engines/defensive.py` (SPEC §7) — `defensive_scores()` (daily, causal) + `rank_defensives(prices, asof)`:
  - **Cross-sectional percentile ranking** on the 6 `config.DEFENSIVE_SCORE_WEIGHTS` factors (return_3m 25, return_6m 20, abs_trend 20, rel_vs_tbill 15, volatility_penalty 10, drawdown_penalty 10). Penalties negated to "higher = better"; weighted sum of percentiles → 0-100 score.
  - Robust across mixed units (returns vs vol vs drawdown); inception-aware (a not-yet-trading candidate drops out of that day's ranking). T-bills always included as eligible/fallback. No forced diversification — weak assets just rank low.
- ✅ `tests/test_defensive.py` — **6 tests pass** (weights sum 100, superior>weak, T-bill always eligible, bounds, inception-aware, no-look-ahead).
- ✅ Real-data sanity: 2022-09 best = SGOV/TFLO/USFR/BIL, worst = IEF/TLT (shuns duration in rising rates); 2019-08 & 2020-03 best = TLT/IEF (owns duration in bond bulls). Full suite: 39 passed, 6 skipped.

### 2026-06-24 — Volatility, Sector, Real-Asset engines built (engines 4-6 of 6) — ALL ENGINES DONE
- ✅ `src/engines/volatility.py` (SPEC §8) — subordinate TRIM. `volatility_multiplier()` buckets realized 63d vol vs the version's target-vol range (`config.VOL_BUCKET_MULTIPLIERS` 100/85/70%); `equity_target(band, vol, version)` = max(band_low, mult × band_high). Hard floor at the band bottom — never an independent de-risk. **7 tests.**
- ✅ `src/engines/sector.py` (SPEC §5, default OFF) — `select_sectors()` returns equity-sleeve weights (sum 1): broad beta (members above 200d trend, SPY fallback) + optional tilt across top 3-4 sectors by 3m+6m RS vs SPY behind a 200d trend gate, capped at 15%. **6 tests.**
- ✅ `src/engines/real_assets.py` (SPEC §1/§6/§12) — `select_real_asset()` picks at most ONE confirmed hedge (gold/TIPS/commodities) passing an independent trend+momentum gate, held to its category cap (gold 25 / commodities 20 / TIPS 20); else None. **6 tests.**
- ✅ All engines causal (trailing windows, no-look-ahead tested). Full suite: **58 passed, 3 skipped** (only the integration/no-lookahead end-to-end placeholders remain).

### Engines complete — 6 of 6: regime (§4), duration (§6), defensive (§7), volatility (§8), sector (§5), real_assets (§6 slot).

### 2026-06-24 — Integration + output built; FIRST END-TO-END RUN WORKS
- ✅ `src/portfolio.py` (SPEC §11/§12) — pure assembly: equity sleeve + defense fill by defensive rank, per-bucket duration caps, T-bill floor (regime + version), real-asset slot, whipsaw incumbent bonus (10-pt). `tests/test_integration.py` — 7 tests.
- ✅ `src/backtest.py` (SPEC §3/§13) — precomputes causal engine series once; month-end signal → **T+1 execution**; per-trade cost on turnover; benchmark NAVs (SPY, 60/40, T-bills). `execution_lag_days` toggle for the no-look-ahead test.
- ✅ `tests/test_no_lookahead.py` — 2 integrated tests pass: T+1 ≠ same-day execution; **truncating future data leaves every past rebalance byte-identical** (the §16 property, end-to-end).
- ✅ `src/metrics.py` (SPEC §14) — CAGR, vol, max DD, worst rolling 3m/12m/3y, downside dev, Sharpe/Sortino/Calmar, beta, up/down capture (monthly geometric-mean), longest underperf vs SPY. `tests/test_metrics.py` — 6 tests.
- ✅ `src/report.py` (SPEC §15) — standalone HTML (plotly.js inlined): equity curve, underwater DD, regime timeline, allocation stacked-area, metrics table, proxy labels.
- ✅ `src/run.py` — one command: `python -m src.run` → loads data, backtests, writes `output/backtest_report_<version>.html`, prints a plain-English headline.
- ✅ **Full suite: 73 passed, 0 skipped.**

### 2026-06-24 — Re-entry ladder built and wired in (SPEC §9)
- ✅ `src/reentry.py` — staged re-entry state machine: caps equity at 25/50/75/100% (stages 1-4). Fast de-risk (defensive regime only lowers the stage), slow rebuild (≤1 stage/month), rollback one stage on credit/vol deterioration, MAX-LAG override on a sharp V-recovery. Pure + causal. `tests/test_reentry.py` — 6 tests.
- ✅ Wired into `backtest.py`: monthly stage-gate conditions built from the causal engine frames (`_reentry_conditions`); equity target = min(vol-trim-in-band, ladder cap). Added `config.REENTRY_STAGE4_SCORE` (by version) + `REENTRY_BREADTH_IMPROVE`.
- ✅ 2020 COVID trace is textbook: held 0% equity through the defensive regime, then rebuilt 25%→50%→60%→80% one stage/month after exiting defensive.
- ✅ No look-ahead preserved (integrated truncation test still passes). **Full suite: 79 passed, 0 skipped.**

### Current results (Balanced, 2015-02 to 2026-06, 137 rebalances)
Mandate delivered: **max drawdown −12.7% vs SPY −33.7%**; worst 12m −7.5% vs −19.7%; vol ~10% vs 17.7%; Sortino 1.09 vs 1.01; beta 0.42; down capture 43% / up capture ~59%. CAGR 9.5% vs SPY 13.9% — lagging in the bull, as intended. Duration engine banned long Treasuries all of 2022, owned them in 2019/2020. Report: `output/backtest_report_Balanced.html`.

### 2026-06-24 — All refinements done (§11/§16 + macro data upgrades)
- ✅ **Taxable turnover bands (§11 step 6)** — `backtest.run_backtest(taxable_mode=True, turnover_band=…)`; a no-trade band suppresses small rebalances (config `TAXABLE_MODE`, `TURNOVER_BAND`). Lowers turnover; verified by test.
- ✅ **Walk-forward switch (§16)** — `metrics.split_walk_forward()` + `run.py` prints in-sample vs out-of-sample metrics when `config.WALK_FORWARD_ENABLED`.
- ✅ **Parameter-sensitivity sweeps (§16)** — `src/sweep.py::run_sweep("PARAM", [values])` backtests a grid and tabulates metrics; always restores config (tested, incl. on error).
- ✅ **Real macro data** — `download_data.py` now pulls **real VIX (CBOE, no key, 4173 rows)** and, if a free `FRED_API_KEY` is in `.env`, **real HY OAS (FRED)**; loaders `load_vix()`/`load_hy_oas()`. Regime stress (vol+credit) and duration (credit) use the real series when present, else the labeled proxies. Report labels each macro input real/PROXY.
- ✅ `tests/test_refinements.py` (5) + VIX stress test in `tests/test_regime.py`. **Full suite: 85 passed, 0 skipped.**
- Real VIX is more cautious than the realized-vol proxy → deeper de-risking: drawdowns improved, CAGR/Sortino lower (honest trade-off; favors the smoothness mandate). HY credit still on the proxy until a FRED key is added.

### Current results — all three versions, with REAL VIX (2015-02 to 2026-06)
Risk ladders correctly (Conservative smoothest → Growth most equity); every version cuts SPY's −33.7% drawdown to roughly a third:

| Metric | Conservative | Balanced | Growth | SPY |
|---|---|---|---|---|
| CAGR | 8.0% | 7.9% | 8.1% | 13.9% |
| Max drawdown | −9.1% | −11.1% | −11.1% | −33.7% |
| Worst 12m | −6.2% | −8.3% | −8.6% | −19.7% |
| Volatility | 8.2% | 9.5% | 9.8% | 17.7% |
| Sortino | 1.02 | 0.89 | 0.89 | 1.01 |
| Calmar | 0.88 | 0.71 | 0.73 | 0.41 |
| Down capture | 32% | 43% | 47% | 100% |

### 2026-06-24 — Rebalance-frequency switch + experiment
- ✅ `backtest.run_backtest(rebalance_frequency="monthly"|"biweekly"|"weekly")` — signal cadence is now a knob (default `config.REBALANCE_FREQUENCY`). Engine lookbacks are time-based and unchanged; only sampling/execution cadence changes. Ladder MAX-LAG scaled by cadence. Test added.
- **Finding (Balanced, 2015-2026):** more frequent rebalancing did NOT improve results — monthly Calmar 0.71 / maxDD −11.1% / cost 1.3% beat weekly (0.69 / −11.3% / 3.7%) and biweekly (0.63 / −12.6%). CAGR/Sortino flat (~7.9% / 0.89). Faster cadence re-entered the Mar-2026 whipsaw ~2 weeks sooner but added more noise-whipsaws + cost elsewhere. Monthly remains the sweet spot. (Classic trend-following result.)

### 2026-06-24 — Whipsaw experiments (idea 1: hysteresis tuning; idea 2: adaptive cadence)
- **Idea 1 — tune whipsaw controls (sweep): ADOPTED.** The workhorse is the immediate-drop knob, NOT confirmation days (raising confirm 2→4 hurt) and NOT the dead-zone (raising it to 6 re-suppressed the whipsaw fix). Applied `REGIME_IMMEDIATE_DROP_POINTS` 10→20 (dead-zone left at 3): CAGR 7.9→8.2%, Sortino 0.89→0.91, Calmar 0.71→0.74, SAME maxDD (−11.1%) and cost (1.3%); softens the Mar-2026 whipsaw (42% not 24%, recaptures the April rally). **Walk-forward OK** (test-era Calmar 0.87 vs 0.86) — modest, robust, not in-sample-only. Reports regenerated.
- **Idea 2 — regime-adaptive cadence:** new `backtest.run_backtest(adaptive_fast_regimes={...})` = monthly base + weekly only while in the named de-risked regimes. Result: smoother (maxDD −10.3% vs −11.1%, worst-12m −7.5%) but LOWER return (CAGR 6.9% vs 7.9%, Sortino 0.77 vs 0.89, cost 2.4%) — net risk-adjusted worse, and did NOT catch the Mar-2026 bounce (regime exits "Defensive" before the rally, so weekly fires during the churn, not the recovery). Kept as an available option; not a default.

### 2026-06-24 — Robustness + Monte Carlo validation (anti-curve-fit, real-account framing)
- **`src/robustness.py`** — one-knob-at-a-time sweeps of 8 key params. Verdict: 7/8 are broad PLATEAUS (incl. the tuned immediate-drop knob — Calmar 0.71→0.74 flat across 8-30, NOT a spike). maxDD ~−11% across nearly all settings. Only `MA_LONG_DAYS` (the 200d MA) shows moderate sensitivity — fine 150-225, degrades at 250 (Calmar 0.54). 200 is the industry-standard value, not fished. → strategy is not curve-fit.
- **`src/montecarlo.py`** — block-bootstrap (63d blocks, same blocks across all series; full-universe 2018+ window, SGOV dropped), 200 synthetic pasts. KEY FINDING: **our actual −11% maxDD was a FAVORABLE draw.** Distribution: median maxDD −18.5%, p5 −33%, worst path −45%; median Calmar 0.48 (actual 0.90 ≈ p80). BUT the edge is robust: **strategy maxDD beats SPY on 94% of paths**, median −18.5% vs SPY −33.7% (roughly half). Per-path CSV: `output/montecarlo_Balanced.csv`.
- **Caveat (disclosed):** our data starts 2010, so the bootstrap can reshuffle 2020/2022-type stress but CANNOT create a 2008/GFC-style systemic bear — true tail risk is likely worse than the −45% shown. Set client expectations to ~−18% typical / −33% bad-case, not −11%.
- Tests: `tests/test_validation.py` (2 smoke tests). Pending follow-ups: cost stress at ~10 bps (real accounts), null/random-signal tests, block-size sensitivity.

### 2026-06-24 — Weekly comparison deliverable
- **`src/weekly_comparison.py`** — `build_weekly_comparison(version, years=5)` → standalone HTML: cumulative-return chart (weekly) of Strategy vs SPY/60-40/T-bills over the trailing N years, rebased to 0%, with a supporting table (per week: cumulative, weekly, daily return per series) + CSV. Output: `output/weekly_comparison_<version>.html` / `.csv`.
- 5-yr (2021-06→2026-06) cumulative: Strategy +40.9%, SPY +85.8%, 60/40 +44.4%, T-bills +18.5% (lagged the bull by design; smoothing shows in the 2022 leg).

### 2026-06-24 — Monte Carlo vs 60/40 (key finding) + gold insight + bug fix
- **MC vs 60/40 (200 paths, block 63):** strategy drawdown shallower than 60/40 on only **50% of paths**; median maxDD −18.5% vs −19.1%, median CAGR 8.8% vs 8.9% — essentially TIED. Robust to block size (63/126/252 → 53/45/44% beat-rate). The clean −11% vs −21% edge in the single 2015-26 history did NOT generalize. Strategy's dependable edge is over **all-equity SPY** (94% of paths), NOT over a balanced 60/40. Honest reframing: vs 60/40 the strategy is **regime insurance** (it can fully exit duration in an inflationary bond bear like 2022 — lost −6% vs 60/40 −16%) that the 2018-26 bootstrap window underweights; it is not a universal drawdown improvement.
- **Gold insight:** the 2023-25 version-ordering anomaly (Conservative > Growth) is driven by GOLD (+133% vs SPY +86%), held 27/36 months as the trend+momentum real-asset slot. The strategy uses gold as a tactical RETURN sleeve (SPEC §1 fills it on momentum), funded from the defense budget — so it commingles crisis ballast with a commodity-momentum bet (~15-19% for years). Worth surfacing as its own reported sleeve.
- **Bug fix:** monthly-log key collision — the real_asset sleeve FRACTION was overwritten by the held TICKER. Split into `real_asset` (fraction) + `real_asset_ticker`. Display-only (allocation chart's real-asset band); no effect on returns/metrics. Reports regenerated.

### 2026-06-24 — Real-asset sleeve broken out (3-sleeve design)
- Gold/real-assets is now its OWN leg (equity / real assets / defense), not a leftover in the defense budget. Sized by a deliberate, version-scaled, trend-gated target `config.REAL_ASSET_SLEEVE_TARGET` = Conservative 10% / Balanced 15% / Growth 20% (scaled UP as a risk asset); whatever it doesn't use flows to true defense (T-bills/Treasuries). Change is in `portfolio.py` (sizing) + config; allocation chart relabeled to 3 sleeves; `test_real_asset_sleeve_sized_by_version_target` added. 88 tests pass.
- **Effect (intended):** version ordering RESTORED to intuitive — gold now scales up toward Growth (avg 23-25: 8%/10%/12%), CAGR Conservative 6.9% < Balanced 7.8% < Growth 8.1%, maxDD −8.5% < −10.4% < −10.9%. The old Conservative-beats-Growth anomaly is gone (it was purely the accidental gold overweight). Cost: slightly lower returns (Balanced CAGR 8.2→7.8%) from holding less gold in gold's exceptional run — a deliberate trade of luck-driven return for an honest, controllable risk budget.
- **Re-validated on new design:** gold attribution (Balanced) — WITH gold CAGR 7.8% / Calmar 0.75, NO gold 6.6% / 0.52; gold added +29 pts full period (was +38). 60/40 MC (150 paired paths): vs SPY robust (96%); vs 60/40 a tie on BOTH axes (drawdown 52%, return-beat 47%, median CAGR 8.4% vs ~8.4%). Deliberate gold sizing shaved the 60/40 return-beat 57%→47% — confirms gold was what tipped it past 60/40. Gold OFF still loses to 60/40 (24% return-beat, 6.1% CAGR). Verdict unchanged & sharpened: robust edge vs all-equity SPY, honest tie vs 60/40, gold is the load-bearing return source.

### 2026-06-24 — Real-asset sleeve diversified into a gold + commodity basket
- `real_assets.select_real_basket()` replaces the single-best `select_real_asset`: the sleeve is now a basket of GOLD + BROAD COMMODITIES (PDBC, no K-1), each trend-gated independently, inverse-vol weighted (config.REAL_ASSET_BASKET, REAL_ASSET_VOL_LOOKBACK=252). TIPS excluded (corr 0.74 to IEF — it's defense). Diagnostics: gold/commod corr ~0.05, so basket vol ~13.5% < either leg (~16-18%). portfolio.py allocates the version-sized sleeve across the legs by weight; tests rewritten (real_assets + integration). 88 tests pass.
- **Effect (Balanced, commodity-diversification isolated):** maxDD −13.5%→−11.4%, Calmar 0.53→0.67, Sortino 0.83→0.87, CAGR 7.2%→7.6%, 2022 −10.5%→−9.1% — adding commodities REDUCED risk and helped return (validates the 0.05-correlation thesis). Basket now ~6% gold / ~6% commod avg; in 2022 it leaned into commodities (the trending hedge). Honest note: vs the prior single-gold-concentrated design it gives up a little gold-concentration return for robustness — the intended trade.
- Pending: re-run gold/real-asset attribution + 60/40 Monte Carlo on the basket design.

### 2026-06-24 — 60/40 benchmark made realistic (IEF → AGG)
- The "40" was pure intermediate Treasuries (IEF) — unrealistic. Swapped to **AGG** (iShares Core US Aggregate Bond: Treasuries + corporates + MBS, ~6yr duration) — what a real diversified 60/40 holds. `config.BENCHMARK_6040 = ("SPY", "AGG")`; `_benchmark_navs` now reads the config pair; AGG loaded as a benchmark-only ticker (in `run_backtest` and the MC panel), not traded. AGG downloaded to `data/` (4143 rows). 88 tests pass.
- Realistic 60/40: full-period CAGR 9.2%, maxDD −21.7%, 2022 −15.6% (was −16.4% w/ IEF — AGG's shorter duration helped slightly). Strategy still ~half the drawdown (−11.4% vs −21.7%).
- NOTE: AGG was fetched via a one-time bridge to the OLD local `.env` (key never displayed). The `TIINGO_API_KEY` user env var is still NOT set — set it (`setx`) before deleting `C:\Users\andre\backtester`, or future downloads from the Drive project will fail.

### 2026-06-24 — Final MC: current basket build vs SPY + realistic 60/40 (200 paths)
- vs SPY: drawdown shallower on **93%** of paths (median −18.2% vs −33.7%), median CAGR 8.3% vs 13.4%.
- vs realistic 60/40 (SPY/AGG): drawdown shallower on **55%** (was 50% with old gold-only + IEF), median maxDD **−18.2% vs −21.7%**, median CAGR 8.3% vs 8.9%. The basket (lower strategy drawdown) + AGG (higher 60/40 drawdown from credit) shifted it from a dead tie to a **modest but real drawdown edge** at ~0.6%/yr return give-up.
- Tail caveat persists: worst strategy path −45.5% vs 60/40 −42.1%; bootstrap can't fabricate a 2008/1970s regime. CSV: output/montecarlo_basket_vs_realistic6040.csv.

### 2026-06-24 — TAIL TEST: 2008 GFC (data extended to 2005) — strategy VINDICATED
- Re-downloaded universe + macro back to 2005 (bridge to old .env key); AGG too. 9 of 11 sectors + SPY/IEF/TLT/SHY/IAU/RSP/VTI/AGG from 2005, BIL/HYG/DBC from 2006-07 — enough to run the strategy through the GFC. (1970s NOT testable — no ETFs, no sector breadth; declined as low-fidelity.)
- **Result (Balanced, 2007-2026 incl. GFC):** strategy CAGR 9.0% / maxDD −11.4% / Calmar 0.79 vs SPY 10.8% / −55% / 0.19 vs 60/40 8.2% / −35% / 0.24. **GFC window (2007-10→2009-06): strategy −6.7% drawdown, +3.2% in calendar 2008** vs SPY −37% / 60/40 −20%.
- **Thesis CONFIRMED:** duration engine read 2008 as deflationary-panic (92% of months, long allowed 50%) and held ~21% Treasuries (rallied); read 2022 as inflationary-bear (100%, long allowed 0%) and held ~1%. A static 60/40 holds 40% bonds in both — saved in 2008, sunk in 2022. **Over a window including 2008 the strategy BEATS 60/40 on BOTH return and drawdown** — the edge the MC's 2018-26 bootstrap structurally couldn't see. The "unprovable tail insurance" is now proven for the deflationary tail (inflationary tail = 2022, already in data).
- Calibration: one episode (but the key one), same causal code as 2022 (not curve-fit); 2008 favored cash+Treasuries (held 68% cash); a stagflation shock remains untestable here.

### 2026-06-24 — Stagflation test (SYNTHETIC, low fidelity — honest logic check)
- Real 1970s multi-asset total-return data not gettable here (Stooq blocked, FRED needs key, no sector/breadth data exists). Built a SYNTHETIC 1973-81 scenario from documented 1970s magnitudes (stocks choppy/down, yields 6%→14% so bonds bleed, gold/commodities boom, cash yields 8-13%) and ran the REAL engine code on it. NOT a historical replay; weakest of the three tail tests by design (I built the scenario).
- **Result (nominal):** strategy +13% / −13% maxDD vs 60/40 −33% / −39% vs SPY −55%. Engine flagged inflationary-bear 64%, long allowed 7% (held ~1% bonds), ~56% cash + gold/commod. Correct stagflation playbook.
- **HONEST caveat:** these are NOMINAL. ~90% cumulative 1970s inflation → REAL returns ≈ strategy −41%, 60/40 −65%, SPY −76%. Nothing wins stagflation; the strategy just loses far less real wealth (by avoiding bonds, holding cash+reals). The ~15% real-asset cap limits how much it can lean into the one true inflation hedge — it defaults to cash (preserves nominal, not real).
- **Three-regime synthesis:** 2008 deflation (real, owned Treasuries, +3% vs 60/40 −20%), 2022 inflation (real, dumped Treasuries, −6% vs −16%), 1970s stagflation (synthetic, cash+reals, +13% vs −33% nominal). Correct regime call in all three; a static 60/40 survives only the first. Adaptivity across all three IS the edge.

### 2026-06-24 — DYNAMIC real-asset cap by macro regime
- The real-asset sleeve target is now scaled by the macro regime the duration engine already detects: `config.REAL_ASSET_REGIME_SCALE` = deflation 0.75× / neutral 1.0× / inflation 1.5× / stagflation 2.0×, clamped to `REAL_ASSET_SLEEVE_MAX=0.35`. Stagflation = sustained inflationary-bear (≥70% of trailing 126d). `duration.duration_signals` adds a `macro_regime` label (unambiguous-regime-only: one filter on, other off, else neutral); `portfolio.py` scales the sleeve target by it. Still trend-gated (cap raises the ceiling; the gate decides what fills it). Test added; 89 pass.
- **Validated across all 4 regimes (static vs dynamic):** deflation (2008) real-asset 6%→5% (down ✅), inflation (2022) 9%→12% (up ✅), stagflation (synth) real return −3%→+1% (✅), NORMAL 2015-26 CAGR 7.6%→7.5% / maxDD −11.4%→−10.9% (unchanged — anti-curve-fit bar MET ✅). Full real-data 2007-26: CAGR 9.0%→8.9%, maxDD −11.4%→−10.9%. Effect is a meaningful nudge, not dramatic (trend gate + budget moderate it); multipliers tunable if more lean wanted.

### 2026-06-24 — Pushed the dynamic cap harder, with anti-curve-fit sweep → adopted L1
- Swept aggressiveness L0→L3 (inflation 1.5→3.0×, stagflation 2.0→5.0×, ceiling 0.35→0.65). Findings: (1) base case FROZEN across all levels — 2015-21 CAGR 9.3% / maxDD −10.2% identical, 2008 deflation frozen at 5% real-asset (regime-gating confines change to inflation/stagflation months — structurally cannot curve-fit the base case); (2) response PLATEAUS at L1 (L1=L2=L3 dead flat) — robust, not a peak; (3) it plateaus because the strategy's OWN trend gate refuses non-trending real assets (gold was flat in 2022 → capped at ~14% regardless of multiplier) + per-leg §12 caps bind.
- **Adopted L1: inflation 2.0× / stagflation 3.0× / ceiling 0.45** — the saturation point. Captures all available benefit (2022 real-asset 12%→14%, stagflation REAL −3%→+1%→+3%), on a verified plateau, base case unchanged. Pushing past L1 provably does nothing. Per-leg commodity cap (20%) now binds at the higher ceiling (correct §12 behavior; test made cap-aware). 89 tests.

### 2026-06-24 — Tested equity→real-asset rotation (structural) — REJECTED (negative result)
- Prototyped (behind `config.EQUITY_ROTATION_ENABLED`, default OFF) a substitution model: in inflation/stagflation, real assets may rotate from up to 50% of the equity sleeve (total risk-asset preserved). Tested rotation ON vs OFF across regimes.
- **Result: does NOT pass.** (1) Doesn't help stagflation — synth REAL return +3%→−1%: the dynamic cap already maxes real assets at their §12 caps (~43%), so rotation has no room to add real assets and just shoves equity into CASH (missing recoveries). (2) Breaks the frozen base case — 2015-21 CAGR 9.31%→8.34%, because `inflationary_bear` (3 of 5 conditions) can fire in a RISING market (late-2021), so rotating out of equity costs bull-market return. Full-period CAGR 9.0%→8.4%. Only 2022 improved (−8.7%→−6.9%), not worth the give-up.
- **Conclusion: the equity/real-asset wall is fine; do NOT change the structure.** The binding constraint on leaning into stagflation is the §12 concentration caps (gold 25 / commod 20), NOT the wall. Leaning harder requires raising those caps = the concentration risk we're avoiding. Strategy is at a sensible frontier. Rotation code kept behind the OFF flag for future exploration (e.g., if cap structure ever revisited). 89 tests pass (flag off → production unchanged).

### 2026-06-25 — Investigated the credit signal: real OAS is a dead end, and "purer" proxy is WORSE — KEEP HYG/IEF
- **Tried to upgrade credit from proxy to real ICE BofA HY OAS via FRED** (`BAMLH0A0HYM2`). Confirmed the key parses and the call works, but FRED returns **only ~3 years** (795 obs, 2023-06+): ICE **restricted its indices to a rolling 3-year window in April 2026**. Full history is now commercial-only (ICE/Bloomberg/Refinitiv). So real OAS is NOT wireable here — reverted the partial parquet, manifest stays `proxy`. The IBKR API doesn't carry this index either.
- **Then tested a "sharper" proxy, HYG/LQD** (HY vs investment-grade corporate, which cancels the rate component to isolate credit) — generalized the credit denominator to `config.CREDIT_PROXY` so it's swappable. **Result: WORSE across the board** — full-period CAGR 7.6%→7.0%, maxDD −10.7%→−12.0%, Calmar 0.71→0.58, **2008 +3.4%→+1.4%**, 2022 −8.7%→−9.8%.
- **Conclusion: the deflation filter *wants* the rate component that HYG/LQD removes.** Credit stress arrives with a flight-to-quality; HYG/IEF (HY vs Treasury) captures both at once. In 2008 investment-grade blew out *too*, so HY-vs-IG barely widened while HY-vs-Treasury crashed cleanly. **Reverted to HYG/IEF.** Kept the configurable-denominator plumbing. 89 tests pass.

### Status: feature-complete + fully tail-tested + regime-adaptive real-asset cap (L1). Equity-rotation tested + rejected. Credit signal investigated — HYG/IEF proxy confirmed best available. 89 tests, 0 skipped.
Only optional future work remains: full per-engine return/drawdown attribution; expand parameter-sweep presets. (Credit-signal upgrade is closed: real OAS isn't freely available, and the proxy is the right choice on the merits.) To run: `python -m src.run` (set `config.ACTIVE_VERSION`).
