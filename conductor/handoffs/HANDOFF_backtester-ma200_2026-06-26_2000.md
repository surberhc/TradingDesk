# HANDOFF — S0 200-day MA fragility fix (2026-06-26)

**For:** a fresh session (e.g. dispatch) continuing work that cannot see the originating chat.
**Read this whole file first.** It is self-contained. Everything important is on disk.

---

## 0. TL;DR (one paragraph)

The Adaptive All-Weather Core (S0) backtester had ONE fragile, load-bearing parameter — the 200-day
moving average (`MA_LONG_DAYS=200`), which VALIDATION.md §4.1 flagged as the strategy's main
parameter-risk concentration. This session **diagnosed, fixed, validated, and ADOPTED** a fix: a
**3% one-sided "early-exit margin" scoped to the regime engine only** (`config.REGIME_TREND_MARGIN =
0.03`). It flattens the fragility (Calmar spread across lookbacks 150→250 fell from **42% to 11%**),
**improves all three client versions**, and **holds out-of-sample**. The code change is already
committed (in the baseline snapshot commit). Three follow-ups remain (below). NOTE a live environment
hazard: the price data under `data/` is on Google Drive and was being overwritten by Drive sync
mid-session, and a PARALLEL session was committing to this repo at the same time.

---

## 1. Environment / paths (Windows, cmd or Git Bash)

- **Repo root (Drive):** `C:\Users\andre\My Drive (andrew@surberhc.com)\TradingDesk`
- **Backtester:** `…\TradingDesk\backtester`  · **Shared strategy brain:** `…\TradingDesk\strategies`
- **Python (LOCAL venv, off Drive):** `C:\TradingDesk-Local\venv\Scripts\python.exe`
- **Run the report:** from `backtester\`, `"C:\TradingDesk-Local\venv\Scripts\python.exe" -m src.run`
- **Run tests:** from `backtester\`, `"C:\TradingDesk-Local\venv\Scripts\python.exe" -m pytest -q`
  (expected: **89 passed, 0 skipped**)
- The `strategies` package is importable as `from strategies import config` (editable install).

---

## 2. What was changed (all on disk; code already committed)

### 2a. The fix (production default, ADOPTED)
`strategies/strategies/config.py`:
- `REGIME_TREND_MARGIN = 0.03`  ← **the adopted fix.** Regime-engine trend gates require price to
  clear its MA by 3% to read "in trend" (early de-risk; removes the knife-edge whipsaw at the MA).
- Other per-engine margins left `None` (off): `DURATION_TREND_MARGIN`, `REALASSET_TREND_MARGIN`,
  `SECTOR_TREND_MARGIN`. **Do not turn these on** — testing showed duration margin does nothing and
  real-asset/sector margins are harmful.
- Also added (research/structural, default-neutral): `TREND_MA_DAYS=None`, `STRESS_MA_DAYS=None`
  (role-split of the old overloaded `MA_LONG_DAYS`), `MA_GATE_MODE="sma"`, `MA_ENSEMBLE_LOOKBACKS`,
  `MA_GATE_BUFFER_PCT=0.0`, plus resolver fns `trend_ma_days()`, `stress_ma_days()`, `trend_margin(scope)`.

### 2b. New shared helper
`strategies/strategies/parts/_gates.py` (NEW) — all trend gates route through here. Modes
`sma`/`ensemble`/`ema` + per-engine early-exit margin. Functions: `membership`, `membership_frame`,
`is_above`, `distance`, `is_above_asof`. At default margins it is **byte-identical** to the prior
inline `price > rolling(N).mean()` logic (verified: 89 tests pass; baseline metrics unchanged before
the margin was switched on).

### 2c. Engines re-routed to `_gates` (trend role only)
`parts/regime.py` (trend/breadth/RS-leadership), `parts/duration.py` (TLT/SPY/commodity trend & ban
gates — but these read `DURATION_TREND_MARGIN`, currently off), `parts/defensive.py` (abs_trend via
`gates.distance`), `parts/sector.py`, `parts/real_assets.py`. STRESS baselines (VIX/credit/realized-vol/
yield-vs-own-MA) were split out to `stress_ma_days()` and are NOT given a margin (they were already robust).

### 2d. Robustness harness
`backtester/src/robustness.py` — added `REGIME_TREND_MARGIN: [0.0,0.01,…,0.05]` to the sweep grid.

### 2e. Research scripts (scratch, in `backtester/`)
`ma_experiment.py` (baseline + diagnostic + ensemble/EMA/buffer sweeps), `ma_experiment2.py`,
`ma_experiment3.py` (symmetric-hysteresis test), `ma_experiment4.py` (per-engine localization),
`ma_experiment5.py` (final guardrails). Run e.g. `"C:\TradingDesk-Local\venv\Scripts\python.exe" -m ma_experiment4`.
These are research scratch — keep or delete at will; not part of production.

### 2f. Docs
`backtester/VALIDATION.md` (§1 headline table, §3 versions, §4.1 fragility-now-resolved narrative,
§8 limitation #4, §2 GFC caveat) and `backtester/README.md` (status-log entry) updated.
**These two doc files are the only UNCOMMITTED changes** (see §5).

---

## 3. The evidence (why we trust the fix) — stable 2015-26 window

| Check | Result |
|---|---|
| MA-lookback Calmar sweep 150→250 (Balanced) | **42% spread → 11%** (FRAGILE → robust plateau) |
| Margin-size robustness (regime scope) | flat at healthy level for 3–5% (the margin is itself a plateau) |
| Base case, all 3 versions | all improve: Conservative Calmar 0.67→0.73, Balanced 0.71→0.74, Growth 0.73→0.76; maxDD shallower |
| Balanced 2015-26 (margin on) | CAGR ~7.5%, maxDD **−10.2%** (was −10.7%), Sortino 0.90, Calmar 0.73 |
| Walk-forward (NAV split) | OOS Calmar improved (0.76→0.81) — not an in-sample artifact |
| 2008 GFC tail (when extended data present) | preserved (≈ Calmar 0.82 / maxDD −10% vs pre-fix 0.84/−10.7%) |

**Diagnostic chain:** (1) split the overloaded 200d knob into TREND vs STRESS roles → fragility is
entirely in the trend role; (2) per-engine localization → it lives ONLY in the regime engine.
**Rejected (all tested):** multi-lookback ensemble (no help), EMA gate (cratered the level), symmetric
hold-in-band deadband (worse + non-robust — de-risks late; the one-sided asymmetry is the
load-bearing feature for a drawdown-first mandate), duration margin (no effect), real-asset/sector
margins (harmful).

---

## 4. OPEN ITEMS (do these one at a time; ask the user before each)

1. **Commit the two doc edits** — `backtester/README.md` + `backtester/VALIDATION.md` (uncommitted).
2. **Move `backtester/data/` OFF Google Drive** (e.g. to `C:\TradingDesk-Local\bt_data`) and update
   the data-path config, THEN **regenerate the 2008 GFC tables** in VALIDATION.md §2/§5. Reason: the
   2005-extended GFC price history was reverted to a 2010-start snapshot by Drive sync this session,
   so the GFC numbers are currently NOT reproducible. The 2015-26 numbers ARE stable.
3. **Re-prove paperbot byte-parity** before any paper use — the shared brain changed. Even though
   defaults were byte-identical, the production default is now margin-ON, so paperbot must be
   re-verified to produce identical targets to the backtester (per the "shared brain" discipline).

---

## 5. Git / environment hazards (READ before touching git)

- Repo HAS a single commit: `3b29616` "Baseline snapshot: TradingDesk + Option B multi-account
  paperbot (v0.3.0)" (2026-06-26 14:28, author Andrew Surber). This was an initial `git init` +
  add-all that **already captured this session's code changes** (config.py, `_gates.py`, regime.py,
  robustness.py, etc.). So `git diff` shows them as unchanged — that is expected.
- **Only uncommitted files:** `backtester/README.md`, `backtester/VALIDATION.md` (this session's doc
  edits, made after that commit).
- ⚠️ **A PARALLEL session was active in this repo this session** — it reverted the `data/` parquet
  (~14:23), created the baseline commit (~14:28), and edited the auto-memory index. Do NOT assume you
  are the only writer. Check `git status` and `git log` before committing; coordinate.
- ⚠️ **`data/` instability:** price parquet lives under Google Drive and was observed being
  overwritten mid-session (2005-extended → 2010-start; successive backtests drifted at the 3rd
  decimal). Until `data/` is moved off Drive (open item #2), treat any freshly-measured number as
  potentially non-deterministic. 2015-26 results reproduced stably; GFC results did not.

---

## 6. How to re-verify the fix quickly (sanity check for the next session)

```
cd "C:\Users\andre\My Drive (andrew@surberhc.com)\TradingDesk\backtester"
"C:\TradingDesk-Local\venv\Scripts\python.exe" -m pytest -q            # expect 89 passed
"C:\TradingDesk-Local\venv\Scripts\python.exe" -m ma_experiment        # baseline + diagnostic sweeps
"C:\TradingDesk-Local\venv\Scripts\python.exe" -m ma_experiment4       # per-engine localization
```
To confirm the margin is on: `grep "REGIME_TREND_MARGIN = 0.03" strategies/strategies/config.py`.
To revert the fix (if ever needed): set `REGIME_TREND_MARGIN = None` in config.py (returns to plain SMA).

---

*Origin session id: 81e874a2-c632-44b3-b086-83a00812ee35. Auto-memory note saved as
`ma200-fragility-fix` in the project memory index.*
