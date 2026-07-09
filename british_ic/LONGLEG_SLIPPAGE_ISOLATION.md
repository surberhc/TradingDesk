# Long-leg exit slippage — isolated from the blended measurement

## Open item this closes

An earlier ad-hoc measurement of real 0DTE exit slippage (fill vs. quoted NBBO,
1-min quotes joined to real IBKR fills) blended the SHORT leg's stop-out closes
and the LONG leg's auto-closes together into one number: **~13x quoted
half-spread, ~$700k aggregate over 2025-07-09..2026-07-07**. That script no
longer exists in the repo, and the figure was never split into long-only vs.
short-only. Since S8's entire mechanism *is* the automated long-leg close (see
`S8_DESIGNATION.md`, `EXIT_RULE_ANALYSIS.md`), whether the long leg specifically
suffers this much slippage — or far less — is load-bearing for whether S8's
paper-validated edge (+108.8% vs. actual +33.5%) survives realistic execution.

This closes that gap: long-only and short-only slippage, measured on the exact
same 1,617-combo population, side by side.

## Methodology

1. Start from `decoupled_long_legs.csv` (1,617 rows, one long leg per combo)
   joined to `combo_ledger.csv` for `short_close_qty`.
2. Derive each leg's implied per-contract close price from FIFO realized P&L
   (no close-price column exists directly):
   - Long: `implied_long_close = long_open_price + long_fifo_pnl / (long_open_qty * 100)`
     (long_open_qty is always positive; sanity-checked against the CSV's own
     `long_entry_cost`/`long_pnl_multiple` columns — exact match, max abs diff
     5.68e-14, floating point noise only). 814/1617 back out to a tiny negative
     number (as low as -$0.10) from fee residuals in FIFO P&L on legs that
     expired worthless — these are clipped to $0.00, not a derivation error.
   - Short: `implied_short_close = short_open_price - short_fifo_pnl / (short_close_qty * 100)`
     (short_close_qty is always positive = qty bought back). Verified sane:
     mean ~$5.86, range $0.01-$18.46, zero negatives, for single-batch shorts.
   - **Excluded from the short-side measurement**: shorts with
     `short_n_open_batches > 1` (606/1617) — their FIFO P&L is computed across
     multiple accumulated opens at different prices, so a single per-contract
     close price can't be cleanly backed out without fabricating a FIFO-lot
     allocation. Flagged explicitly (`short_multi_batch_fifo_not_disaggregable`),
     not silently included. This does not affect long-leg coverage (longs are
     always single-lifecycle).
3. For each leg's own close timestamp, look up the real 1-min SPXW NBBO quote
   (bid/ask) for that exact contract (strike/right/expiration=TradeDate) at the
   containing minute, reading warehouse parquet files date-by-date (not all at
   once) via pyarrow dataset column+filter pushdown to stay memory-safe.
   Timestamps are exact-minute keyed; exact match used, falling back to nearest
   quote within +/-2 min if the exact minute is missing.
4. Slippage = `abs(fill - mid)` in dollars/contract, and as an
   `x half-spread = slippage / ((ask-bid)/2)` multiple (zero-spread rows flagged
   and excluded from the x-half-spread stat, not divided by zero).
5. Aggregate $ = per-leg slippage x actual traded qty x $100 SPX multiplier,
   summed across measured legs.

## Measured numbers

| Metric | LONG | SHORT |
|---|---:|---:|
| median $/contract | $0.0535 | $0.9282 |
| mean $/contract | $0.1610 | $1.1615 |
| median x half-spread | 2.00x | 13.64x |
| mean x half-spread | 3.95x | 21.65x |
| n measured | 589 | 948 |
| aggregate $ (measured legs) | **$27,298** | **$253,828** |

Blended (long+short pooled, equal-weighted per leg-side): median 7.59x, mean
14.86x, aggregate $281,126 across 1,537 measured leg-sides.

## Coverage

- Total decoupled legs: 1,617.
- **Long leg measured: 589/1,617 (36.4%)**. Excluded: 995 close at/after the
  16:00 ET market-quote cutoff (`close_after_market_quote_cutoff`) — these are
  overwhelmingly options that ran to 16:20 expiration settlement rather than a
  live short-stop close, so there is no real market quote to compare against
  (not a data gap, a structural fact: no short stop → no long auto-close → the
  long rides to settlement). 33 fall in the known final-3-trading-days warehouse
  backfill gap (`no_quote_file_for_date`, matches the 98.0%-covered figure noted
  in `STRATEGY_RECONSTRUCTION.md` Part 2 for the same population).
- **Short leg measured: 948/1,617 (58.6%)**. Excluded: 606 multi-batch shorts
  (FIFO not cleanly disaggregable, see above), 42 after the quote cutoff, 21 in
  the same backfill gap.

## Comparison to the prior blended ~13x / ~$700k figure

The short-only median (**13.64x half-spread**) lands almost exactly on the
prior reported "~13x" figure — strong evidence the earlier blended measurement
was, in practice, dominated by short-leg stop-outs (short legs both outnumber
measurable long legs here and carry ~4-11x the per-leg slippage). This
measurement's directly-comparable blended aggregate ($281,126 across 1,537
measured leg-sides) is meaningfully below the prior ~$700k, most plausibly
because: (a) the prior run likely measured against the full short-stop
population (not gated to this decoupled-legs subsample, and not excluding
multi-batch shorts), and (b) this run's long-side aggregate is thin by
construction (995/1,617 long legs never get a slippage estimate at all, because
they settle rather than stop out). Both are population/scope differences, not
signs of a bug in the derivation — the per-leg $ and x-half-spread numbers
here are independently sane (SPXW nickel/dime spreads, sub-$0.20 median long
slippage, mid-single-digit-dollar short closes, no negative/absurd prices),
and the short-only figure alone reproduces the prior "~13x" almost exactly.

## Verdict

**Long-leg slippage is dramatically SMALLER than the blended number implied —
not larger.** Long-only median slippage is $0.05/contract (2.0x half-spread);
short-only median is $0.93/contract (13.6x half-spread), roughly **17x higher
in dollar terms and ~7x higher in half-spread multiples**. This makes sense
mechanically: SPXW quoted spreads are roughly constant in dollars (~$0.10-0.20
one-way) regardless of the option's price, but the long leg is almost always a
cheap, deep-OTM option near expiration (median implied close near $0, since
these are the protective wings that mostly expire worthless) — so the
half-spread "multiple" on a near-zero fill barely moves the needle in dollars,
while the short leg (the option actually being defended, priced meaningfully
higher when it stops out) eats a much bigger absolute and relative bite.

For S8: the strategy's core mechanism — the automated long-leg close — is
**not** the expensive part of this strategy's execution. The already-existing
blended ~13x/~$700k estimate, if anything, OVERSTATES the risk specific to the
long-leg auto-close rule; it was effectively measuring short-stop slippage
(which S8 inherits from the underlying British IC short leg regardless of the
long-leg rule, and which is not new risk introduced by S8's automation). This
does not mean S8's edge is fully validated net of costs — short-leg stop-out
slippage is real, large, and shared by every version of this strategy
(vanilla British IC included) — but it means the *incremental* execution risk
attributable to S8's specific automated-long-close mechanism is small: median
$0.05-$0.16/contract, a rounding error relative to the strategy's per-combo
P&L swings (hundreds to low-thousands of dollars, per `combo_ledger.csv`).
S8's paper-validated edge should be evaluated against the (unchanged, larger)
short-leg slippage risk it shares with the base strategy, not against a
long-leg-specific risk this measurement shows to be modest.

## Files

- `british_ic/longleg_slippage_isolation.py` — script (rerunnable end to end)
- `british_ic/longleg_slippage_isolation_results.csv` — 1,617 rows, per-leg
  long vs. short slippage detail with coverage/exclusion columns
- This file
