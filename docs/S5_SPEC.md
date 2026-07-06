# S5 — Standalone Account-Level Hedge Sleeve  ([always-on tail] + [short-premium financing])

**STATUS: RESEARCH COMPLETE / VALIDATED IN SHAPE / SHELVED pending paper deployment (2026-07-05).**
The research question is closed and the design is settled. This is the final deployable spec. No further
research is planned; the next step is paper wiring when resumed. Do not re-open the research (see §7).

**Type:** Final strategy specification. PAPER / research only — nothing is armed or transmitted; the frozen
S0/regime config is untouched.
**Author of concept:** strategy owner (Andrew) + Claude — a formalization of the owner's design, adversarially
pressure-tested and validated on real warehouse data.

---

## 0. Net verdict (lead with this)

**S5 is a self-carrying, fast-crash hedge — validated in shape.** The unit is a standalone sleeve a client
bolts onto their *other* (hotter, less-hedged) strategies to keep the whole account alive through a crash. It
is built as ONE combined position — an always-on protective tail plus a short-premium financing overlay —
and judged on ONE combined P&L. Measured on real warehouse skew with honest bid/ask fills across both clean
SPXW windows:

- **8 of 8 usable years POSITIVE.**
- **Calm-year carry +0.22%/yr** — the financing overlay flips the naked tail's ~−1%/yr bleed to positive.
  It pays the hedge's rent, so the sleeve costs almost nothing to carry in quiet markets.
- **Crash payoff intact:** COVID-2020 **+5.39%**, 2018-Q4 (a −20% quarter) **+1.10%**, the 2022 slow-grind
  **+0.04%**. **Net convexity stays LONG through both crash bottoms** — verified honest (the financing leg
  genuinely lost ~−$10.9k/contract in Mar-2020; the year still nets positive because the *tail* explodes).

**Character:** strong in a fast crash (its job), merely flat — not losing — in a slow grind (its one
documented soft spot). The known limitation is *sample size*, not a design flaw: ~7 usable years and 2–3 real
stress episodes mean the design directions and the *shape* are validated, but the exact 15% / 0.10-delta /
1.0x decimals are shape-validated, not nailed — confirm the ratio with forward OOS before trusting precise
sizing. That is a proportionate caveat on a validated result, not a reason to withhold it.

> **On the earlier "comprehensive refutation."** An earlier pass tested each financing *leg* in *isolation*
> against a bar that demanded a standalone win in every window including crashes. That answered the wrong
> question — a financing overlay need not win the crash its paired hedge exists to cover. Judged in its real
> role (the combined sleeve, on net merit), S5 works. This is the concrete case that drove the CLAUDE.md
> "counterweight — judge on net merit" section.

---

## 1. Purpose & role

**S5 = the total-account hedge, in its own lane.** It is one sleeve among several a client runs. Its single
job is to keep the whole book alive in a crash so the client's *other*, less-hedged strategies can run hotter.
Crash insurance is the product and it must stay alive — this is **not** a strategy trying to get rich off a
crash. The tail is sacred; the financing serves it and never compromises it.

Key reframe from all prior versions of this spec: **there is no core in the unit.** S5 is not core + tail +
financing. The client's own book *is* the thing being protected; S5 is the bolt-on protective sleeve. The unit
is exactly two legs, evaluated as one P&L:

1. **The TAIL** — an always-on protective long-put position (the catastrophe insurance + the convexity).
2. **The FINANCING** — a short-premium overlay whose only job is to pay the tail's rent while keeping the
   combined book net-long convexity.

Goal ladder (as blessed): **step 1 = the overlay pays its own tail carry** (validated — it does, and then
some in calm years). Stretch/home-run (+5–10%/yr on top) is explicitly *secondary* and was not chased; fund
the hedge first.

---

## 2. The two-leg design + validated config

The whole unit lives in the **SPXW / SPX cash-settled, European, Section-1256 (60/40)** options family, so the
legs net against each other for margin and tax, there is no assignment / pin risk, and the short leg can be
managed cleanly. Both legs are priced on **real warehouse skew** with **honest bid/ask fills** and commissions.

### 2.1 TAIL — always-on protective puts (the sacred leg)
- **~15% OTM SPX puts, ~63 DTE, ~0.50 notional, rolled at ~21 DTE.** Continuously rolled so the book is never
  without protection. Own convexity before you need it; never chase it after the spike.
- The tail's put delta is the de-risking engine: as spot falls, the delta marches toward −1 and auto-de-risks
  the protected book; on the recovery it auto-re-risks — no signal, no discrete re-entry decision to lag on.

### 2.2 FINANCING — short-premium overlay (the rent-payer)
- **Short put-write, ~0.10 delta, ~45 DTE, sized 1.0x the tail notional**, honest fills.
- Its only job is to finance the tail's theta. It is **financing, never the main bet.** In calm markets it
  collects enough to flip the tail's carry positive; in a crash it is *allowed* to lose (and did, honestly)
  because the tail's payoff dwarfs it.

### 2.3 The validated best-balance config (the starting point for paper)

| Leg | Strike | DTE | Size | Roll / mgmt |
|---|---|---|---|---|
| **Tail** (long puts) | ~15% OTM | ~63 | ~0.50 notional | roll at ~21 DTE |
| **Financing** (put-write) | ~0.10 delta | ~45 | 1.0x tail notional | honest bid/ask fills |

This is the *starting point* for paper, not a frozen truth — the decimals are shape-validated (see §5).

---

## 3. Measured performance

Both clean SPXW windows (**2018-01-02 .. 2020-08-12** and **2022-01-03 .. 2026-07-02**; **2021 is a dead-quote
data hole**, excluded). Real skew, honest fills, combined-sleeve P&L.

| Year / episode | Combined sleeve P&L | Character |
|---|---|---|
| Calm years (mean carry) | **+0.22%/yr** | financing flips the tail's ~−1%/yr bleed positive |
| **COVID-2020** (fast crash) | **+5.39%** | the tail exploding — the sleeve's whole reason to exist |
| **2018-Q4** (−20% quarter) | **+1.10%** | fast-ish decline; convexity pays |
| **2022** (slow grind bear) | **+0.04%** | flat-but-positive; the documented soft spot |
| **All usable years** | **8 / 8 POSITIVE** | — |

**Crash-payoff honesty check:** in Mar-2020 the financing leg genuinely lost ~−$10.9k/contract. The year still
nets positive because the tail's gain dominated and post-crash high-IV premium repaired the financing leg. The
positive 2020 is the tail, not the financing — exactly as designed. **Net convexity stayed LONG through both
crash bottoms.**

---

## 4. The convexity guardrail + the load-bearing design principle

**Design principle (load-bearing):** a **CLOSER (15%) tail is what unlocks running FULL 1.0x financing while
keeping net convexity long.** This is the single most important design finding.

- Deeper (20–25%) tails post the best-*looking* raw carry (up to **+1.46%/yr**) — but at 1.0x financing they
  flip the combined book **net-delta positive**. That means they **finance the hedge away**: the convexity is
  lost, the sleeve stops being a hedge, and those cells are **disqualified.** The prettiest raw-carry cells are
  traps; the honest winner keeps its convexity.

**HARD GUARDRAIL (invariant, non-negotiable):**
> **Net convexity must stay LONG at all times.** The binding constraint is the **net-delta check at the crash
> bottoms** — if the combined book's net delta is not comfortably negative-tilted (long-convexity) at a crash
> low, the config is disqualified regardless of how good its carry looks. **Financing is FINANCING, never the
> main bet.** Any cell that flips net-delta positive at a bottom is out.

**Character, stated plainly:** this is a **fast-crash hedge** (strong — COVID, 2018-Q4). It merely **stays
flat** (does not lose) in a **slow grind** (2022) — that is the known limitation, not a failure. A closer /
shallower tail helps the slow grind but costs carry; the 15% config is the balance point that keeps the fast-
crash payoff, runs full financing, and stays net-long convexity.

---

## 5. Honest limitations (proportional)

The result is validated in shape; these are the real caveats, weighted to what they actually change:

- **Sample size is the main limit.** ~7 usable years and 2–3 real stress episodes (fast COVID, slow 2022,
  the 2018-Q4 quarter), plus the 2021 data hole. The **shape** and the **design directions** are validated;
  the **exact 15% / 0.10-delta / 1.0x decimals are not nailed.** Confirm the ratio with forward OOS / more
  data before trusting precise sizing. This is a data limit, not a design flaw.
- **Slow-grind soft spot.** The sleeve is flat-to-slightly-positive in a slow grind (2022 +0.04%), not
  strongly positive. A closer/shallower tail improves the grind but costs carry — carry this as a documented
  known, not a defect.
- **No core in the unit.** The sleeve is priced and validated as a standalone bolt-on; sizing it to a specific
  client book is a paper-deployment step (§8), not something this research resolved.
- **Real-skew / honest-fill basis.** Priced on real warehouse skew with honest bid/ask fills — the numbers are
  as honest as the windows allow, but they inherit the two-window sample.

---

## 6. What was refuted (the honest trail — do not re-run)

The isolation question is closed and negative; keep the trail so it is not re-litigated:

- **No financing leg self-funds a ≥1.56%/yr bar STANDALONE.** The full pre-registered sweep —
  put-write, put-credit-spread, iron condor (neutral + call-income arms), put calendar/diagonal, and
  sell-against-owned-tail — over TENOR{7,14,30,45} × DELTA{.10,.15,.20,.30} × MGMT{hold, profit_50, dte_21,
  profit_50_or_dte_21, stop_2x} × REGIME{ungated, calm_only} × windows A/B = **N=1,920 per-window cells (960
  configs)**, honest bid/ask fills, matched random-sit-out placebo, OOS across both windows, cross-structure
  Deflated Sharpe. **Zero cells clear the standalone bar.** Best-cell **DSR = 0.000** vs E[max SR under N=1920]
  ≈ 22. The 7 superficial both-window passers were naked short-vol that blow up −95% to −122% of core in the
  2022 bear.
- **Capital-efficiency float ≈ 0** (max ~15bp) — the T-bill/basis lever contributes essentially nothing to
  the hurdle.
- **The COMBINED SLEEVE is the unit that works.** The isolation refutation answered the *wrong* (standalone-
  per-leg) question. A financing overlay need not win the crash its paired hedge exists to cover; judged as the
  combined sleeve on net merit (§0, §3), S5 works in shape. **Do not re-test the harvest / financing-leg
  category in isolation.**

---

## 7. Code & data pointers

All under `backtester/`. Reports are gitignored under `backtester/output/s5_financing/`.

| Piece | File |
|---|---|
| Tail model | `s5_convexity_overlay.py` |
| Honest-fill EOD multi-DTE engine | `s5_financing_harness.py` |
| Sweep + eval driver | `s5_financing_sweep.py` |
| Combined-sleeve P&L | `s5_sleeve_pnl.py` · `s5_sleeve_run.py` · `s5_sleeve_depth_size.py` |
| Reports (gitignored) | `backtester/output/s5_financing/SLEEVE_YEAR_BY_YEAR_20260705.md`, `SLEEVE_DEPTH_SIZE_20260705.md` |

**Commits:** `1a78a6f`, `2ede52c`, `1e57652`, `06faee2`, `50569fd`, `4c7839f`.

**Warehouse:** real EOD SPXW skew chains (`C:\TradingDesk-Local\warehouse`). Windows: 2018-01-02..2020-08-12
and 2022-01-03..2026-07-02; 2021 is a dead-quote hole (excluded).

---

## 8. Open items for paper deployment (when resumed)

Research is done; these are the *build/operate* items, not research questions:

1. **Execution mechanics** — continuously rolling the tail (~63 DTE, roll at ~21 DTE) and the financing puts
   (~45 DTE) with honest fills.
2. **Paperbot wiring** — bolt the sleeve into paperbot on the PAPER account (DU…141, port 4002). This is an
   order-affecting change → bump `paperbot/version.py` + CHANGELOG when built. If paperbot ever computes
   targets itself, that is an architecture change (pull-and-clarify) requiring a real parity test first.
3. **The review → arm → transmit gate** — enforced live; nothing transmits without a deliberate, gated, armed
   action. The net-convexity-long invariant (§4) enforced at every point.
4. **Sizing the sleeve to a client's book** — the sleeve is standalone; scale it to the specific book it
   protects.
5. **Confirm the ratio with forward data** — accumulate forward OOS to firm up the 15% / 0.10 / 1.0x decimals
   (§5).
6. **Carry the slow-grind soft spot** as a documented known (§4).

---

*Research trail preserved in memory `s5-financed-convexity-overlay.md`. Prior spec versions (synthetic-core
framing, self-funding waterfall, active monetization) are superseded by this standalone-sleeve final design.*
