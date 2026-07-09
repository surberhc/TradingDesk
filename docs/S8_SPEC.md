# S8 — SPX 0DTE Credit Spread Pair, Scheduled Entries + Stop-Triggered Long-Leg Close

**STATUS: STANDALONE STRATEGY DESIGNATED (2026-07-09). Research/paper only — no live capital, no
paperbot wiring yet.** This document is the canonical spec — S8's rules, performance, and limitations
are now defined and evaluated on their own terms, not perpetually re-derived as "the B2 correction vs.
what actually happened" in the account it came from. That does NOT mean the live account stops being
useful: it remains an active, ongoing out-of-sample data source (see §6) — S8 will continue to be
tested against its real forward fills as they accrue, and Andrew is separately working on an additional
historical export to extend the analysis window further back (see §7 item 3). Standalone means S8 has
its own identity and spec; it does not mean the live comparison is retired.

**Type:** Strategy specification, entries + exits fully mechanical. PAPER / research only — nothing is
armed or transmitted; the frozen S0/regime config is untouched; no backtester/paperbot code exists for
S8 yet (see §7).

---

## 0. Net verdict (lead with this)

**S8 is a scheduled, template-driven pair of SPX 0DTE credit spreads (a short leg at a target credit
price, a long leg protecting it) with one mechanical exit rule: close the long leg the instant its
paired short leg stops out.** Backtested against 236 trading days (2025-07-09 to 2026-07-07) of real
execution-level fills for the entry side and real intraday price paths for the exit side:

- **+108.8% over the 1-year window** (vs. a $127,710 reference starting balance), positive in 9 of 13
  months.
- The long-leg exit rule beats a do-nothing/discretionary baseline on **~83–87% of individual long
  legs**, holding up on a chronological train/test split and after removing the single largest
  contributing day in each split — a broad tilt, not a couple of lucky days.
- **The core trade-off is real and must travel with every future evaluation of this strategy**: the
  long leg gives up 100% of its upside past the moment the short stops. S8 is a variance-reduction
  design, not a free lunch — it converts an occasional lottery-ticket payoff into a modest, high-hit-
  rate risk reducer.

**Known limitations, unresolved, do not treat as closed:** ~1 year of data with exactly one true
crash-magnitude event (2025-10-10); exit fills modeled at 1-minute OHLC close rather than a real bid/ask
fill (see §5); no live implementation exists anywhere.

---

## 1. Purpose & role

S8 is a **short-vol, defined-schedule income strategy on 0DTE SPX options**, sized and managed by fixed
mechanical rules rather than discretion. Its role in the strategy lineup is distinct from the
already-refuted SPX premium-selling family (condor/CSP/strangle, see `spx-premium-selling-refuted`):
those were tested as *managed, discretionless-entry* structures and found to be equity beta, not alpha.
S8 differs on both axes that mattered there — **entries are scheduled/template-driven** (not a single
static rule swept for a parameter fit) and **the long leg carries a real, mechanically-triggered exit**,
not a static hold-to-expiry or profit-take rule. Whether S8 clears the same alpha-vs-beta bar the prior
family failed is an open question for forward evaluation (§7), not yet answered — this spec formalizes
the ruleset so that question can be asked cleanly.

---

## 2. Entry rules (fully specified, empirically derived — see `british_ic/STRATEGY_MECHANICS.md`)

S8 trades **11 template configurations**, each an SPX 0DTE credit spread (puts or calls) defined by two
independent dials:

### 2.1 Entry schedule
Each template fires on a **fixed clock-time grid**, not on a market-condition trigger:
- `{Puts|Calls} - 80 - $4` — morning grid, ~08:45–11:00 CT (30-min steps: 08:45, 09:15, 09:45, periodically
  10:15/10:45).
- `{Puts|Calls} - 80 - $3` — late-morning/early-afternoon, concentrated 12:00–13:00 CT.
- `{Puts|Calls} - 50 - $4` — afternoon grid, ~12:15–13:45 CT (12:15, 12:45, 13:00, 13:30 account for ~97%).
- `Puts - 80 - $2` — smaller/secondary configuration, spread 10:00–14:15 CT, no dominant slot.

Re-entries after a stop-out follow the **same fixed clock grid**, not a reactive "wait N minutes after
stop, then re-enter" rule (median gap from stop-out to next same-template entry is only loosely centered
near zero, IQR −22 to +53 min; only 4.8% of same-template overlapping entries land within ±5 min of the
prior close).

### 2.2 Strike / credit selection
- **Target entry credit** (`PriceOpen`) is the primary dial the "$2/$3/$4" label encodes — medians land
  almost exactly on the labeled figure ($2.05–2.15 / $3.00–3.08 / $3.95–4.20). This is NOT a strike-width
  label (realized widths range 5–85 points).
- **Strike selection is credit-driven, not delta-driven or fixed-point**: the algorithm selects short
  strike + width to hit the target credit; realized short-leg delta lands in a narrow ~0.22–0.29 band
  across every template regardless of the "80"/"50" label, confirming strike placement is vol-adaptive
  (same delta target across vol regimes) rather than a fixed point-offset from spot.
- **The "80"/"50" label** is best read as a stop-aggressiveness / target-win-rate setpoint (see 2.3), not
  a delta or width value.

### 2.3 Stop formula (verified exact, 99.98% match on 4,610 real rows)
```
PriceStopTarget = floor(10 x (PriceOpen + StopMultiple)) / 10
```
i.e., the spread is stopped when its mark-to-market cost to close rises to the entry credit plus a fixed
per-template `StopMultiple` (constant-dollar stop, not constant-percentage), rounded down to the nearest
$0.10 tick. `StopMultiple` is deterministic per template:

| Template family | StopMultiple | Implied breakeven win rate |
|---|---|---|
| `-80-$4` | 3.3 | 76.7% |
| `-80-$3` | 2.4 | 70.6% |
| `-80-$2` | 2.0 | 66.7% |
| `-50-$4` | 3.2 | 76.2% |
| `-50-$3` | 2.4 | 70.6% |
| `-50-$2` | 2.0 | 66.7% |

---

## 3. Exit rule (the "B2" long-leg rule — the one deliberate design addition)

**Short leg:** exits exactly at the stop formula in §2.3 (or expires worthless/ITM at 16:20 if never
stopped) — unchanged from how the strategy is scheduled to trade.

**Long leg:** **close it the instant its paired short leg stops out.** No profit target, no timer, no
discretion — a single mechanical trigger. If the short is never stopped (expires at settlement), the
long leg also just runs to settlement (there is no "stop event" to act on).

This was arrived at by testing it against two alternative families on real intraday price paths for
1,584 decoupled long legs (98% coverage of the reconstruction window):
- **Simple profit-take** (close at Nx entry cost) and **partial ladder** (sell half at N1x, rest at N2x):
  **refuted** — apparent edge is >90% attributable to one or two single days (a crash day, a large
  one-directional day); once those are excluded, both families are flat-to-negative at every swept
  threshold, in-sample and out-of-sample alike. Do not revisit these without clearing the same bar
  (chronological split + explicit single-day-removed check) applied here.
- **Time-boxed forced close after stop** (close within Z minutes of the stop if already ITM by some
  margin): weak positive edge, only at short Z (≤15 min), degrading to negative as Z grows — directionally
  consistent with, but strictly worse than, closing immediately. The rule collapses toward B2 as its
  best-performing limiting case.
- **B2 (close immediately)** is the one rule that survives: no free parameter to have been fit, positive
  in every tested segment even after removing the largest single day, and beats the discretionary
  baseline on 83–87% of individual legs. See `british_ic/EXIT_RULE_ANALYSIS.md` / `STRATEGY_RECONSTRUCTION.md`
  Part 2 for the full train/test methodology and swept parameter tables.

---

## 4. Backtested performance (as its own strategy — see §6 for provenance)

2025-07-09 to 2026-07-07, 236 trading dates, real execution-level fills for entries/stops, real 1-minute
intraday price paths for the long-leg exit trigger:

| | S8 |
|---|---|
| Total P&L | **+$138,982** |
| Return on $127,710 reference balance | **+108.8%** |
| Months positive | 9 / 13 |
| Long-leg win rate vs. discretionary baseline | 83–87% (holds train and test) |

Two individual days are unusually large single-day contributors on top of a broader real day-to-day
tilt: 2025-10-10 (a market crash) and 2026-05-18 (a large one-directional move) — excluding both, S8
still wins on 83% of the remaining 58 days and adds +$87,583 over that period alone, so the headline
is not two-lucky-days-dependent.

---

## 5. Known limitations (carry forward, do not drop)

1. **Thin regime coverage.** ~1 year of data, exactly one true crash-magnitude event (2025-10-10). A
   single tail event cannot validate a tail-dependent design's true expectancy.
2. **Exit fill realism not yet modeled.** Exits in this backtest are marked at 1-minute OHLC close, not a
   real bid/ask fill. A dedicated measurement isolating long-leg-specific exit slippage (as opposed to
   the blended short-stop + long-close ~13x-half-spread / ~$700k aggregate figure previously measured)
   is tracked separately — see `british_ic/LONGLEG_SLIPPAGE_ISOLATION.md` when available.
3. **No live/paper implementation exists.** This is a backtested ruleset, not a running system anywhere.
4. **Entry-side edge itself is not yet independently stress-tested** the way the exit rule was (train/test
   split, single-day-removed check) — §2's entry mechanics were derived to describe what the strategy
   *does*, not yet re-validated as a source of edge on its own out-of-sample terms. That is exactly the
   forward work in §7.

---

## 6. Provenance (history, not the ongoing scoreboard)

S8's ruleset was reverse-engineered from an external, live-traded account ("British IC," IBKR account
U***9156, run via TAT/NinjaTrader) whose own trade log understated its true P&L (it marked the long leg
worthless the instant the short stopped, when a human actually held it longer). The reconstruction
against that account's real fills (`british_ic/RECONSTRUCTION_NOTES.md`) is where the entry mechanics
(§2) and the candidate exit rule (§3) were both discovered and validated.

That account remains a live, ongoing source of real out-of-sample evidence — it keeps trading, and
every new day of its fills is a genuine forward test of whether S8's rules continue to hold up, not a
one-time backward-looking comparison that's now closed. The distinction that changed 2026-07-09 is
identity, not data access: S8 is no longer *defined* as "a delta against this account's actual P&L," it
is defined by its own rules (§2, §3) — but those rules should keep being checked against this account's
real forward fills as they accrue, exactly the way any strategy gets walk-forward re-validated (§7 item
5). Full derivation detail remains in `british_ic/` for anyone who wants to audit the methodology; it is
not required reading to work on S8 itself.

---

## 7. Forward work (what "developing S8 as its own strategy" means next)

Not started unless noted:
1. **Wire S8's full ruleset (§2 + §3) as a real strategy module**, runnable through the backtester's own
   `run_backtest()` path the way S0/S4/S5 are, so it can be evaluated with this project's standard
   walk-forward / out-of-sample tooling instead of a one-off reconstruction script.
2. **Fill-cost realism pass** on both legs (§5.2) before any paper deployment is considered.
3. **Extend history** back to 2024-09-16 (TAT log coverage) — **actively IN PROGRESS, owned by Andrew**:
   he is pursuing an additional Flex Query export to add regime variety beyond the single 2025-10-10
   crash event. Re-run the §2/§3/§4 numbers once that data lands.
4. **Independent entry-side validation** — test whether the scheduled-entry/credit-target structure
   itself (not just the B2 exit correction) clears an honest out-of-sample bar, the same rigor already
   applied to the exit rule.
5. **Walk-forward re-check cadence — starts now, not just "once paper/live exists."** The British IC
   account keeps generating real forward fills today; treat each new batch as an out-of-sample check on
   S8's rules (§2, §3) as it comes in, rather than waiting for a paper/live S8 implementation to exist
   before re-validating. Do not bless S8 once and leave it unmonitored.
