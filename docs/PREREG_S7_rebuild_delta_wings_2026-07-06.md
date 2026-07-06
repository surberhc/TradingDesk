# PRE-REGISTRATION ADDENDUM — S7 REBUILD: delta-based wings

**Registered:** 2026-07-06 (written and committed BEFORE the rebuild run — the timestamp is
the point of this document). Amends `PREREG_S7_income_condor_2026-07-04.md`.
**Author:** desk research (Claude), on Andrew's explicit instruction ("go", 2026-07-06).
**Status at registration:** hypothesis only. No delta-wing S7 result exists yet.

> This addendum changes ONE frozen chassis dial from the original S7 prereg — the wing
> construction — and states the new grid, benchmark arms, headline, and pass criteria IN
> ADVANCE. Everything else (data, honest net-combo fills, corruption handling, no-lookahead,
> discipline) carries over unchanged. A refutation remains a **valid and expected outcome.**

---

## 1. Why this amendment exists (the mechanism)

The original S7 (fixed 25-point wings) **lost in every grid cell, at every fill including
mid.** The root cause was diagnosed as a **frozen dial, not a strategy failure:**

- Fixed 25-pt wings → ~\$635 credit vs ~\$1,865 max loss ≈ **1:3 reward:risk** → break-even
  needs ≈ **75% win rate**; realized win rate was ≈ **63%** → structural loss by construction.
- **Mechanism (decoupled dials):** the **short-strike delta** sets the *win rate* (how often
  price stays inside the shorts); the **wing width** sets the *break-even win-rate threshold*
  (credit/max-loss ratio). These are independent. The original study swept the short delta
  but **froze the wing at 25 pts** — so it never tested whether a construction whose credit
  is a larger fraction of its max loss clears break-even at the realized ~63% win rate.
- The documented CBOE CNDR index buys **≈5-delta long wings** (delta-based, not fixed
  points). That is the construction this rebuild tests as its primary hypothesis.

Moving this dial is **not** curve-fitting: it is mechanism-first, swept WIDE, judged on a
PLATEAU not a peak, and confirmed OOS + per-regime + against placebos (all below). The
original 25-pt setting is **retained as a control arm** so the comparison is explicit.

---

## 2. What changes vs. the 2026-07-04 prereg

**CHANGED — wing construction (the only chassis edit):**
- Long protective wings are selected by **target delta ≈ 0.05** (the CBOE 5-delta wing),
  using the **same clean-delta selection path** already used for the short strikes (vendor
  delta when the day is not degenerate; BSM re-inversion on degenerate 2020/2021 days and on
  any degenerate individual leg). The defined-risk box becomes `(short_strike − long_strike)`
  in points, which now **varies with the vol surface** instead of being a constant 25 pts —
  that is the intended effect (credit becomes a larger, delta-consistent fraction of max loss).
- **Control wings retained:** fixed **25-pt** (the original, refuted setting — the anchor)
  and fixed **50-pt** (a wider control) run alongside, so the plateau spans the wing axis.

**ADDED — two first-class benchmark arms** (not afterthoughts; judged head-to-head on TOTAL
P&L at honest fills):
- **CBOE CNDR replica:** 20-delta shorts / 5-delta wings / 30 DTE / **hold to expiry** /
  weekly ladder. The documented index construction.
- **ATM cash-secured put (the strongest documented edge):** sell the ≈50-delta (nearest-ATM)
  put at the target DTE, **hold to expiry**, cash-settled at intrinsic, weekly ladder, honest
  fill band. Defined-risk floor = strike (cash-secured). This is the reference bar the condor
  must be judged against — CBOE PUT index Sharpe 0.65 vs SPX 0.49 over 32 yr.

**ADDED — IV-rank entry filter as a VARIANT arm (explicitly not assumed to be the edge):**
- `enter-always` (baseline) vs `high-IVR-only`. IV-rank = percentile rank of the VIX close
  over a trailing **252-trading-day** window; `high` = **IVR ≥ 50** (top half). This is a
  **coarse, un-tuned** split, stated in advance. Research REFUTED every specific IVR
  threshold and the profitable CBOE CNDR uses NO IV filter — so this arm exists to *test* the
  directional VRP claim, and a null/negative result on it is expected and fine. We do **not**
  sweep the IVR threshold hunting for a winner (that would be curve-fit by construction).

**UNCHANGED (carried over verbatim from the 2026-07-04 prereg):** data source & window
(warehouse EOD SPX chains 2018-06 → 2026-07), honest net-combo fill band {mid, 0.25, 0.50,
1.0}, corruption handling (never select strikes off corrupt vendor delta; BSM re-inversion),
weekly-laddered concurrent book, cash-settlement, 1-lot sizing, no-lookahead guarantee, and
the known **2020-08-13 → 2021-12-31 quote blackout** (all-zero NBBO → those entry-weeks are
unquotable and skipped; the honest data window is reported).

---

## 3. Rebuild grid (pre-registered — for a PLATEAU, not a peak)

| axis | values |
|---|---|
| DTE (target) | **{30, 45}** |
| short delta (target) | **{0.16, 0.20}** |
| **wing construction** | **{5-delta (primary), 25-pt control, 50-pt control}** |
| management | **{50%-target-or-21-DTE (managed), hold-to-expiry (control)}** |
| IV-rank filter | **{enter-always, high-IVR-only (IVR ≥ 50)}** |
| fill fraction | **{0.0 = mid, 0.25, 0.50 = HEADLINE, 1.0 = full cross}** |

**Benchmark arms (run separately, across the same fill band):** CBOE-CNDR replica;
ATM cash-secured put (DTE {30, 45}).

**HEADLINE config:** **45 DTE / 0.16 short delta / 5-delta wings / 50%-target-or-21-DTE /
enter-always / f = 0.50.**

The verdict is judged on the **plateau across the DTE × short-delta × wing × management ×
IVR grid** and across the **fill band** — never on a single winning cell.

---

## 4. Data source for fills (this run vs. the suite)

- **This SPX rebuild:** **warehouse EOD chains** (ready now; clean bid/ask in all non-blackout
  years). Headline fill f = 0.50 (realistic half-spread on the 4-leg net combo).
- **The diversified premium suite (later):** keys off the **15:45 intraday QUOTE SNAPSHOT**
  (all legs one timestamp — fixes the EOD single-inconsistent-time problem). Single-name
  snapshots are still downloading (ETA mid-to-late July); SPX EOD is sufficient and honest
  for the base-strategy go/no-go now.

---

## 5. Pass criteria (ALL required — else REFUTED; unchanged in spirit from §6 of the base)

S7 (delta-wing) is a **genuine income edge** only if it clears **every** bar:

1. **Net-positive at realistic fills** at the headline config **and across the mid → 50% fill
   band.** Positive only at mid is refuted.
2. **OOS survival:** net-positive in **both** halves — train **2018-06 → 2021-12**, test
   **2022-01 → 2026-07** (blackout-adjusted coverage noted).
3. **Plateau, not peak:** net-positive across a **broad contiguous region** of the grid,
   including the primary 5-delta-wing sub-grid — not one island.
4. **Management earns its keep:** the managed arm beats **hold-to-expiry on TOTAL P&L** AND
   beats the **random-exit-matched-holding placebo** on TOTAL P&L (not merely win rate).
5. **Beats the reference bar:** the winning condor construction must be competitive with the
   **ATM cash-secured put** on risk-adjusted TOTAL return (Sharpe/Sortino) — if the documented
   simplest edge dominates the condor everywhere, that is the honest finding.
6. **Crisis survivability:** the full-cycle ledger (incl. 2018-Q4 / 2020 COVID / 2022 bear)
   stays net-positive.

Fail 1, 2, 3, or 6 → **REFUTED.** Clear 1-3 & 6 but fail 4 → "hold condor may have an edge,
managed overlay refuted." Clear all but fail 5 → "condor works but the cash-secured put is
the better documented vehicle." **A clean refutation is a full, valid, headline result.**

---

## 6. Deliverables

- Engine: `backtester/s7_income_condor.py` (delta-wing construction + benchmark arms; the
  25-pt path retained as a control). No tuning to data.
- Runner + report: `backtester/output/s7_income_condor_rebuild_2026-07-06.md` (full grid ×
  fill band, OOS split, per-crisis, placebo, benchmark comparison, explicit VERDICT).
- Tests: extend `backtester/tests/test_s7_income_condor.py` — no-lookahead, cost-charged,
  clean-delta guard, and a delta-wing-selection guard (wings are ≈5-delta, further OTM than
  the shorts, on the correct side).
- Frozen S0–S6 / regime config untouched. Warehouse read-only. New research files only.
