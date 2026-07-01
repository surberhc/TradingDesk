# CAN SLIM options-overlay HYBRID — approved spec (v1, for the record)

_The strategy exactly as approved. The backtest (`options_overlay_backtest.py`) implements
this and NOTHING beyond it; any ambiguity is flagged back, not silently resolved._

## Strategy

**ENTRY.** Same breakout entry as the stock system (his actual pivot: entry date + entry
price, held fixed). Buy a ~AT-THE-MONEY call (strike ≈ pivot/entry price). Tenor is a TEST
knob: **3 months and 6 months**. Size: premium budget = **7% of the intended stock
allocation** (also test **14%**); `contracts = floor(budget / call_premium)`. The premium is
the MAX LOSS.

**INSURANCE MODE.** Hold the OPTION, not the stock. Capital at risk = premium only; the
position cannot be shaken out. Do NOT convert on the first 50-day cross. No management during
this phase except the conversion trigger below.

**CONVERSION TRIGGER** ("no longer insurance" = the call has become a stock proxy): convert to
stock — exercise, take delivery at the STRIKE, deploy the strike capital — when the modeled
call **DELTA** crosses a threshold (TEST **0.80 / 0.85 / 0.90**). Delta is computed from the
BS model along the path. (Extrinsic-value-remaining is the theta-equivalent cross-check;
reported, but the trigger is DELTA.)

**MANAGEMENT MODE.** After conversion, cost basis = **strike + premium paid**; hand the
resulting STOCK to the core winning exit (imported from `execution_backtest.py`, rule E3):
hold above the rising 50-day SMA, exit on a decisive close below it, NO profit cap.

**EXPIRATION / FAILURE.** If delta never hits the trigger — at expiry, if the option is
meaningfully ITM (here: S > strike by a margin, ITM_MARGIN), take delivery (never discard
intrinsic value) and hand to the core exit; if it's ~ATM/OTM (stock chopped sideways), let it
EXPIRE WORTHLESS, book the premium loss, and do NOT take delivery. NEVER roll.

## Test grid (report the FULL grid; do not cherry-pick)

tenor {2mo, 3mo, 4mo, 6mo, 9mo} × strike {ATM, ~5% ITM, ~5% OTM} × delta-trigger
{0.80, 0.85, 0.90} × premium-budget {7%, 14%}. Priced with Black–Scholes; IV sensitivity
{40%, 60%, 80%} with proper theta decay and (if feasible) an earnings-date IV bump +
post-earnings crush. (Tenor was WIDENED from the original {3mo,6mo} because his winners are
slow — median hold ~84d, big winners ~113d, only ~8% resolve in <=1mo. These remain
longer-dated ~ATM calls, NOT deep-ITM LEAPS — the instrument is unchanged.)

## Time-to-conversion analysis (added)

For eventual-WINNER names, measure DAYS-FROM-ENTRY until the modeled call reaches the delta
trigger (the take-delivery point), reported as 25/50/75/90th percentiles. Then per tenor,
report the CROSSOVER: fraction of eventual-winners that CONVERT before expiry (captured) vs
EXPIRE first (lost to too-short a tenor), against the premium cost and notional that tenor
buys at the 7% budget. This exposes the time-vs-premium tradeoff so a ROBUST tenor is visible
from the data (not curve-fit to the single best P&L cell).

## Universe

Liquid-option names only (larger/optionable filter; small-caps excluded). The exact IN/OUT
lists are reported in `options_overlay.md`.

## Compare

Head-to-head vs the STOCK-outright book (buy at pivot, core E3 exit). Metrics: total return,
max DD, win rate, per-year (bull 2024/2025 vs choppy stretches). DECOMPOSE in dollars:
(a) shakeout-survival wins — the −7% stop ejected the stock but the option survived and
converted to a winner; (b) theta/stall losses — the stock went flat/small and the option bled
to worthless.

## Guards (rule #1)

Every rule from this spec + real option mechanics, nothing tuned to a favorable number; the
full grid + IV sweep are reported. Limits stated plainly: modeled BS prices (no real
spreads/surface/skew), liquid-option subset only, small sample, bull-heavy 2023–2026 period,
and the exercise/delivery capital assumption (delivery deploys the full strike dollars).

## Flags to main (ambiguities resolved conservatively, stated here for the record)

- **Delta computed on the SAME modeled IV as the price** (per run of the sweep). The spec says
  "compute delta from your BS model" — a single-IV BS delta is the faithful reading.
- **"Meaningfully ITM" at expiry** = S > strike × (1 + ITM_MARGIN), ITM_MARGIN = 0.05. Chosen
  to match the "~ATM/OTM → let expire" language; reported, not tuned.
- **Earnings IV bump/crush:** per-trade earnings dates are not present in the ledger, so a
  per-trade bump is not feasible without inventing dates. Base runs use a flat IV; the code
  exposes an `EARN_BUMP`/`CRUSH` hook set to 0 so the modeling choice is explicit and the
  absence is disclosed rather than faked.
- **Delivery capital** is modeled as a cash round-trip from the same start capital (deploy
  strike at conversion, recover share value at the core exit).
