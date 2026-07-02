# CAN SLIM replica - STOCK-side ADD (pyramiding) backtest

_Does ADDING TO POSITIONS improve the stock-only replica? Selection, entries, starter sizing, and the EXIT rule (E3: -7% stop -> rising-50-day handover -> decisive-break sell) are ALL held fixed to isolate the ADD effect. Add rules are anchored to Doug's MEASURED behavior (research/doug_add_behavior.md) + O'Neil's raise-the-stop discipline -- NOT tuned. The options version is separate and waits for real quotes._

- Start capital **$650,000**; his 118 built positions; entries + starter $ = his revealed cost; exit = E3 for every arm.
- Add anchors (from his data, frozen): trigger within **14 days** of entry while price is in the buy-zone **[-3.6% .. +9.6%]** off entry; add size **60% of starter** (ONEIL_HALF = 50%); **one add max**; built name capped at 25% of equity. Each add RAISES the stop so the blended position's worst-case dollar loss stays == the starter's original -7%.

## Arm-by-arm vs baseline

| Arm | Total ret | CAGR | Max DD | Win% | Median trade | Mean trade | Losing-tail (worst decile) | 10th-pctile | #adds |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| BASELINE | +32.2% | +9.7% | -9.2% | 31% | -6.1% | +6.3% | -18.9% | -15.0% | 0 |
| DOUG_MEASURED | +40.1% | +11.9% | -14.8% | 29% | -5.0% | +5.7% | -18.8% | -14.8% | 95 |
| ONEIL_UPTREND | +37.7% | +11.2% | -15.5% | 30% | -6.0% | +5.7% | -18.8% | -14.8% | 89 |
| ONEIL_HALF | +37.6% | +11.2% | -14.1% | 29% | -5.3% | +5.4% | -18.8% | -14.9% | 93 |

_Same picks / entries / starter sizing / E3 exit across all rows. The ONLY difference is the add. #adds = how many of the 118 positions received a top-up._

## Deltas vs BASELINE (the isolated add effect)

| Arm | dTotal ret | Max DD (base -> arm) | dWin% | dMedian | dMean |
|---|---:|---:|---:|---:|---:|
| DOUG_MEASURED | +7.8% | -9.2% -> -14.8% (deeper) | -2.7pp | +1.1% | -0.6% |
| ONEIL_UPTREND | +5.4% | -9.2% -> -15.5% (deeper) | -1.8pp | +0.1% | -0.6% |
| ONEIL_HALF | +5.4% | -9.2% -> -14.1% (deeper) | -2.5pp | +0.7% | -0.9% |
_Max DD is more negative = deeper = worse. Adding DEEPENS portfolio drawdown here (more capital committed), even though each add's per-position loss is capped at the starter's -7%._

## Added-to vs single-entry cohorts (does Doug's §5 direction hold in-sim?)

_For DOUG_MEASURED: split the built positions into those that got an add vs those that did not, and compare -- the mirror of doug_add_behavior.md §5. His data: added-to won 48% (mean 3.1%, median -0.2%) vs single-entry 28% (mean 6.1%, median -6.5%) -- adding was a consistency lever, not a mean booster._

| Cohort | n | Win% | Mean | Median |
|---|---:|---:|---:|---:|
| added-to | 95 | 28% | +6.6% | -4.7% |
| single-entry | 20 | 30% | +1.3% | -7.0% |

## Verdict (this sample)

- **Adding RAISES total return** by +7.8% (baseline +32.2% -> DOUG_MEASURED +40.1%) -- but at the cost of a DEEPER drawdown, and it is NOT the consistency lever his raw data suggested (see below).
- **It does NOT act as a consistency lever in-sim** -- the opposite of what his raw §5 table showed. Win rate -2.7pp (baseline 31% -> 29%), median trade +1.1%, and max DD got DEEPER (-9.2% -> -14.8%, i.e. 5.6pp WORSE). Once the raise-the-stop discipline is enforced AND the extra dollars are marked in a real portfolio, adding levers RETURN (more capital in working names) rather than smoothing the ride.
- **Why the flip from his §5 data?** His raw table found adding improved win rate / median because he added DISCRETIONARILY only to names already working ('only-if-working' selection). This mechanical rule adds to nearly every name that dips into the buy-zone early (95/118), so it does NOT inherit his selection edge -- it just deploys ~60% more dollars into the same E3 outcome. Return scales up; the win-rate/median smoothing does not, because that smoothing was his PICKING, not the add mechanic.
- **Mechanism check (raise-the-stop holds):** an add can never deepen the *per-position* dollar loss vs the starter's -7% (verified in code). The DEEPER PORTFOLIO drawdown comes from concentration -- ~60% more capital committed across many names that dip together in the 2025-2026 air-pockets -- not from any single add blowing through its stop.

### Hard limits (curve-fit + honesty guards, rule #1)
- **Anchored, not tuned.** Window (14d), zone (-3.6%..+9.6%), size (60%/50%), one-add cap, 25%-of-book cap all come from his measured behavior or O'Neil's playbook. The grid is DESCRIPTIVE; no cell was selected to win.
- **Fires more often than he did.** He added to ~25% of positions (discretionary, 'only-if-working'); the mechanical rule adds to EVERY position that enters the zone in the window (95/118 here). Re-creating his ~25% selectivity would be lookahead, so this OVER-adds vs him -- a conservative stress of the mechanic, not a replica of his hand.
- **Bull-heavy 2023-2026 universe, small N of adds, his-entries-only** (EXECUTION not selection). Paths begin ~entry, so the 50-day filter (ONEIL_UPTREND) is rarely available inside the 14-day add window -> that arm adds very seldom by construction; read it as 'add only when an early uptrend is already confirmable,' which is strict here.
- **Add funded at entry** (single cash event) since it lands within ~2 weeks; if cash is short the add is cancelled and the position runs starter-only. No lookahead: the add fires on the first qualifying bar using only bars up to that day.
