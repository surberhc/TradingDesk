# PRE-REGISTRATION — S7: SPX 45-DTE Managed Premium-Income Condor

**Registered:** 2026-07-04 (written and committed BEFORE any backtest is run — the
timestamp is the point of this document).
**Author:** desk research (Claude), on Andrew's instruction.
**Status at registration:** hypothesis only. No S7 result exists yet.

> A refutation is a **valid and expected outcome.** This document commits the exact
> chassis, grid, fill band, headline, and pass criteria **in advance** so the verdict
> cannot be back-fit to whatever the data happens to show. If S7 fails any pre-registered
> bar below, it is reported as REFUTED, not quietly re-specified until it passes.

---

## 1. Hypothesis

The **volatility risk premium** (VRP) — SPX option-implied volatility runs persistently
above subsequently-realized volatility (the long-run ratio is roughly **implied ≈ 1.43×
realized**) — can be **harvested at the portfolio level, net of honest fills, across a
full market cycle including 2018-Q4, the 2020 COVID crash, and the 2022 bear**, by a
**defined-risk, 45-DTE, weekly-laddered iron condor that is actively managed** (take
profit at a target fraction of credit, and time-stop at 21 DTE).

This is the monthly-style, defined-risk premium seller that income traders actually run —
the version **never yet honestly tested on this desk**. All prior condor work (S2/S3/S6)
was **0DTE** and is now **fully refuted four ways** (see memory `s2s3-intraday-condor-refuted`,
`s6-spx-cashflow-0dte`, `ddoi-gamma-refuted`). S7 is a distinct strategy number (Andrew's
call), lineage S2/S3 income but a genuinely different chassis (multi-week tenor, held-and-
managed, laddered book), so the 0DTE refutation does **not** pre-decide it.

**Directional prior / honest doubt:** the VRP is real and well-documented, but it is *not*
free money — it is compensation for bearing crash risk. The open empirical question is
whether, **after realistic half-spread fills on a 4-leg combo** and **after the fat-tail
losses of 2018/2020/2022**, a mechanical managed condor keeps a positive, robust,
out-of-sample net edge — or whether the fills + tail losses eat the whole premium and it
is another cosmetic-high-win-rate mirage (win most weeks, give it all back in the crashes).

---

## 2. Data

- **Source (READ-ONLY):** warehouse EOD SPX option chains
  `C:\TradingDesk-Local\warehouse\raw\options\SPX\{YYYYMMDD}.parquet`, 2018-01 → 2026-07.
  Columns include `date, expiration, strike, right, bid, ask, delta, implied_vol,
  underlying_price, open_interest`. Confirmed at registration: all real trading days carry
  the full 41-column greek schema; Jan-1 style holiday files are empty and skipped.
- **Pricing** uses **real bid/ask** (clean in all years) — honest fills, never mid.
- **Benchmark:** 3-month T-bill / cash (risk-free) return over the same window for the
  annualized-return and Sharpe comparison.

### 2.1 KNOWN DATA CORRUPTION — delta & implied_vol (2020 partial, 2021 total)

Verified empirically at registration (memory `warehouse-iv-corrupt-2020-2021`):

| sample day | delta==0 share | implied_vol==0 share | median IV | verdict |
|---|---|---|---|---|
| 2018-06-01 | 0.6% | 25% (deep-OTM $0.025 legs, normal) | 0.137 | clean |
| 2019-10-01 | 0.3% | 22% | 0.177 | clean |
| 2020-04-01 | 0.1% | 5.6% | 0.393 | clean-ish |
| 2020-07-01 | 0.4% | 14% | 0.269 | mostly clean |
| 2020-10-01 | 2.3% | 13% | 0.269 | mostly clean |
| **2021-04-01** | **49.2%** | **50.1%** | **0.000** | **CORRUPT** |
| **2021-07-01** | **49.4%** | **50.1%** | **0.000** | **CORRUPT** |
| **2021-10-01** | **49.4%** | **50.0%** | **0.000** | **CORRUPT** |
| 2022-04-01 | 0.6% | 14% | 0.215 | clean |

The 2021 pattern (≈half the rows with delta exactly 0 or ±1 and IV exactly 0) means the
vendor greeks are **degenerate** — every OTM leg reads delta 0 / IV 0 and every ITM leg
reads delta ±1. **Strike selection by target delta is poisoned** in this window; **pricing
(bid/ask) is NOT affected.**

**Handling (committed in advance):**
- **Never** select strikes off the corrupt `delta` column.
- **Per-day validation:** flag a day's `delta` column as degenerate when the share of
  rows with `|delta|` exactly ∈ {0, 1} exceeds a fixed threshold (**35%**, chosen to sit
  well below the ~49% corrupt days and well above the ~2% clean days — a wide margin, not
  a fitted knob). On a flagged day (and as a belt-and-suspenders on any individual leg
  whose vendor delta is missing/degenerate), **re-invert a clean delta** from
  `mid = (bid+ask)/2`, `underlying_price`, a fixed rate/dividend assumption, and
  `T = (expiration − date)` via BSM (reusing the audited `s6_recon` BSM: `implied_vol_from_mid`
  → `bs_delta`). This is the same put-call-parity-free, per-strike inversion already used
  and trusted elsewhere on the desk.
- **Report** the count and date-range of days that required clean re-inversion.
- **Test guard:** a pytest asserts that on a known-corrupt 2021 day the engine does NOT
  use the vendor delta (it takes the re-inversion path).

---

## 3. Chassis (exact, frozen for this study)

- **Structure:** symmetric **iron condor** — short put ≈ target delta, short call ≈ target
  delta, long protective wings a **fixed 25-point width** further OTM on each side (SPX is
  $100/pt; 25 pts = defined risk of `$2,500 − net credit` per condor per side). *(A fixed
  point width, not a delta-selected wing, is chosen so the defined-risk box is constant and
  interpretable; stated here rather than discovered later.)*
- **Entry cadence:** **weekly ladder** — one new condor opened per calendar week, at the
  first available trading day of that week, on the listed expiration nearest the target DTE
  (30 or 45). Positions are held **concurrently** and managed **independently** (a laddered
  book, the way income traders actually run it).
- **Marking:** each subsequent trading day, mark every open condor from that day's EOD
  bid/ask at the applied fill fraction.
- **Management (per condor, checked daily, causal — first rule to fire wins):**
  - **Managed arms:** close when open profit ≥ `target%` × entry credit **OR** when
    `DTE ≤ 21`, whichever comes first; else the position runs to its expiration and is
    **cash-settled** at intrinsic (European index, defined risk, **no assignment**).
  - **Control arm:** hold every condor to expiry / settlement (no profit-take, no time-stop).
- **Settlement:** cash-settled index. At expiry, P&L = net credit − intrinsic value of the
  short strikes breached (capped by the long wings). No assignment / pin modeling needed.
- **Sizing:** **1 lot per weekly entry.** Portfolio equity = cumulative realized P&L across
  the laddered book. No compounding, no vol-sizing (kept deliberately plain).

### 3.1 Fills — HONEST NET-COMBO (never mid)

- Fill fractions of the **net** bid-ask spread of the 4-leg combo:
  **{0.0 = mid, 0.25, 0.50 = HEADLINE, 1.0 = full cross}**.
- Applied on **both** entry (credit received) **and** every management close (debit paid).
- The fraction **propagates through the profit-target trigger**: a friendlier fill →
  more entry credit + cheaper close → the target% touches on a *different* day. P&L is
  computed **per arm per fill fraction** — the fill is not bolted on after the fact.
- Concretely: `credit_received = mid_credit − f × half_spread_credit`;
  `debit_paid = mid_debit + f × half_spread_debit`, where each leg is filled to the
  disadvantageous side by fraction `f` (sell shorts toward bid, buy longs toward ask, and
  vice-versa on close). At `f=0.5` this is a realistic half-spread fill.

---

## 4. Grid (pre-registered — for a PLATEAU, not a peak)

| axis | values |
|---|---|
| DTE (target) | **{30, 45}** |
| short delta (target) | **{0.10, 0.16}** |
| management | **{hold-to-expiry (control), 25% target, 50% target}** (21-DTE time-stop applies to the two managed arms) |
| fill fraction | **{0.0, 0.25, 0.50, 1.0}** |

**HEADLINE config:** **45 DTE / 0.16 delta / 50%-target-or-21-DTE / f = 0.50.**

The verdict is judged on the **plateau across the DTE × delta × management grid** and
across the **fill band**, NOT on any single winning cell.

---

## 5. Evaluation (committed in advance)

Window: **2018-06 → 2026-07** (full available). For each arm/config, at the headline
**f = 0.50**:
- total P&L and portfolio equity curve (1 lot / weekly entry);
- win rate (per condor);
- P&L distribution (mean, sd, skew, worst single condor, tail);
- max drawdown of the equity curve;
- annualized return, **Sharpe & Sortino**, vs the cash / T-bill benchmark.

**OOS split:** train **2018-06 → 2021-12**, test **2022-01 → 2026-07**. Both halves
reported; the test half must not collapse.

**Per-crisis breakout:** **2018-Q4**, **2020-02 → 2020-04 (COVID)**, **2022 (bear)** —
reported separately (these are where a short-vol book is supposed to bleed; the question is
whether the full-cycle ledger still nets positive).

**Plateau check:** report the full grid × fill band; a genuine edge is a broad region of
net-positive cells, not one island.

**PLACEBO (mandatory for any managed arm net-positive at f=0.50):** the
**random-exit-matched-holding placebo** — for each managed condor, replace the rule-based
exit with an exit on a random day drawn to match the *same holding-period distribution*,
repeated over many seeds. The managed arm must beat this placebo on **TOTAL P&L** (not just
win rate). If a random exit with the same average holding period earns the same money, the
*timing* is worthless and the "management" is cosmetic.

---

## 6. Pass criteria (ALL required — else REFUTED)

S7 is declared a **genuine income edge** only if it clears **every** bar:

1. **Net-positive at realistic fills:** total P&L > 0 at the headline config **and holds
   net-positive across the mid → 50% fill band** (f = 0.0 through 0.50). A strategy that is
   only positive at mid (f=0) is refuted — mid is not a real fill.
2. **OOS survival:** net-positive in **both** the train and test halves (not carried by
   one regime).
3. **Plateau, not peak:** net-positive across a **broad contiguous region** of the
   DTE × delta × management grid — not a single cell.
4. **Management earns its keep:** the managed arms (25%, 50%) must beat **plain
   hold-to-expiry on TOTAL P&L** (not merely on win rate) **AND** beat the
   **random-exit-matched placebo** on TOTAL P&L. If management does not beat hold-to-expiry
   on total dollars, the managed layer is refuted (even if S7-hold survives on its own).
5. **Crisis survivability:** the full-cycle ledger (incl. 2018-Q4 / 2020 / 2022) is still
   net-positive — the crashes do not erase the calm-period premium.

If S7 clears 1-3 & 5 but fails 4, the verdict is: "hold-to-expiry condor may have an edge,
but the *managed* overlay is refuted." If it fails 1, 2, 3, or 5, S7 is **REFUTED** as an
income strategy. **A clean refutation is a full and valid result** and will be reported as
the headline.

---

## 7. Deliverables

- Engine: `backtester/s7_income_condor.py` (new; reuses `s6_recon` BSM + `s6_control`
  honest-fill concepts on EOD chains).
- Report: `backtester/output/s7_income_condor_20260704.md` (all tables, data-cleaning
  note, explicit VERDICT).
- Tests: `backtester/tests/test_s7_income_condor.py` — no-lookahead (a future day cannot
  change a past entry/close), cost-charged, clean-delta guard for 2020/2021.
- Roster entry in `datacollector/STRATEGIES.md`.

Frozen S0–S6 / regime config untouched. Warehouse read-only. New research files only.
