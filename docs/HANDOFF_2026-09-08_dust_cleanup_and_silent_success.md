# HANDOFF — 2026-09-08 — The dust cleanup, and the silent-success problem behind it

**Read this first if you are picking the desk back up cold.**
Written at the close of Tuesday 2026-09-08. Everything below is measured from the desk's own
data, not assumed. It is written in plain English for Andrew, not for an engineer.

The one-sentence version: there are 612 tiny leftover positions across 158 accounts that the
trading desk physically cannot sell, we know exactly what they are, and the whole cleanup is
waiting on a single free click in the Interactive Brokers Advisor Portal that nobody has made yet.

---

## PART A — The dust cleanup: what it is, and why it is stuck

### What we found

There are **612 sell lines across 158 of Andrew's accounts**, worth roughly **$24,000** measured
at the closing prices of Monday 2026-09-07, or about **$25,600** at live prices during the day.

These were produced through the desk's ordinary trade rail — the same code path the desk uses to
execute for real. Nothing here is a side calculation or a one-off script. The chain is:
the account roster scan, then the plan builder for a group of accounts, then the per-account
rebalance planner.

### Every single line is a complete exit

This is the most important fact in Part A, and it was verified rather than assumed: **all 612
lines say "sell the entire position and hold none of it."** Not one of them is a partial trim.
Checked directly: every line has a target of zero shares. 611 are marked as fractional leftovers
and 1 is marked as a position being rotated out of the model.

That means the instruction to whoever executes this is simple and uniform. There is no
per-account arithmetic to get right. It is always: sell all of it.

### The twelve holdings involved

| Ticker | Accounts | Value |
|---|---|---|
| BIL | 149 | $7,042 |
| SIL | 148 | $7,099 |
| BUCK | 64 | $692 |
| XLP | 63 | $2,028 |
| GDXJ | 56 | $2,864 |
| GDX | 22 | $649 |
| XLE | 22 | $799 |
| XLI | 22 | $636 |
| XLU | 22 | $388 |
| XLV | 22 | $1,430 |
| SILJ | 21 | $331 |
| RSPT | 1 | $0.35 |

**BIL, SIL and RSPT do not appear in any published model at all.** Selling those is
unambiguously correct — they are not supposed to be held anywhere. The other nine holdings do
belong to models, just not to the models these particular accounts are running, so for these
accounts they are still leftovers to be cleared.

### One account is different from all the others

Every position on the list is smaller than a single share — the largest is 0.9985 of a share —
with exactly one exception:

> **Account U7333246, Jackson, Abby and John, holds 68.8616 shares of BUCK.**

For that one account the desk itself will place the whole-share part — 68 shares — and the
remaining 0.8616 of a share has to be sold by hand. It is the only account on the list with
that shape, so it is the only place where a human is selling a leftover fragment rather than a
full sub-one-share position.

### Four whole-share BUCK trades that are NOT part of this cleanup

These must not be traded by hand. **The desk places them itself.** Listing them here so nobody
sees BUCK on the cleanup list, sees BUCK in an account, and sells the wrong thing:

| Account | Client | Value |
|---|---|---|
| U7552751 | Whittaker | $607,871 |
| U7349974 | Stallbaumer | $233,081 |
| U7349657 | Walter | $4,325 |
| U7333246 | Jackson, Abby and John | (the exit described above) |

### Why the desk cannot do this itself

Two hard facts stack up:

1. Interactive Brokers' trading interface that the desk connects through **rejects fractional
   orders outright**. It returns errors numbered 10243 and 10244 and refuses the order.
2. Because of that, the desk's block order executor **rounds every quantity down to a whole
   number of shares** before sending.

Put those together and for all 612 of these lines the rounded quantity is zero. The desk sends
nothing, and — this is the part that actually matters — **it does not tell anyone it sent
nothing.** The line simply disappears from the run without complaint.

That silent dropping is the real defect. It is why this dust accumulated in the first place
instead of being noticed the first time it happened. Part C below is the fix for it.

---

## PART B — The blocker: this is exactly where work stopped

The plan was to hand the cleanup to **Ted Perez** as a task in the client system, called
"Dust Cleanup". Ted's access was checked and is not a problem: he has full access to both
entities, APS Ventures, LLC and APS Insurance, LLC, with the same grants Andrew has.

### The route we intend to use

The **Allocation Order Tool in the Advisor Portal**, run **once per ticker across all the
affected accounts at the same time** — so twelve passes through the tool, not 158 separate
account visits.

The reason to prefer that tool over uploading a file of orders is that it works out the
quantities from the accounts' live holdings at the moment it runs. It does not need anyone to
assert a number that might already be out of date.

### Two questions that are still unanswered

Both of them are answered by the same single click, and the click costs nothing.

**Question one — will the orders actually do anything?**
Interactive Brokers documents that an account which is *not* enabled for fractional trading will
**silently receive a whole-share order instead** of the fractional one it was sent. For 611 lines
that are all smaller than one share, a whole-share order means selling nothing at all — while the
screen reports that everything succeeded. That is the worst possible outcome: it looks like the
cleanup was done when nothing moved.

**Question two — does the tool even accept fractional quantities?**
Interactive Brokers has an error numbered **10251** whose text reads "Fractional shares are not
supported for allocation orders." That directly contradicts the Allocation Order Tool's own
documentation, which says enabled accounts receive fractional orders. It may be a distinction
between the point-and-click tool and the programmatic interface. We do not know, and we should
not guess.

### The check that settles both — and it transmits nothing

> Open the Allocation Order Tool. Select the accounts. Choose BIL. Click **Calculate Proposed
> Orders**. Read the quantity it shows.
>
> - A quantity with a decimal in it means **both answers are yes** — the accounts are enabled and
>   the tool accepts fractions. Proceed.
> - A quantity of zero means **stop**. The route does not work and we need a different one.

**Nothing is transmitted by clicking Calculate Proposed Orders.** It only shows what it would do.

This preview stands in for a live test because a live test is not available to us: Interactive
Brokers does not support fractional trading in paper accounts at all, so there is no way to
rehearse this safely first.

### What our own trade history already tells us about the permission

A fractional fill on an ordinary stock is only possible in an account that has fractional trading
enabled — so past fills are proof of the permission, one account at a time.

| Book | Accounts proven enabled |
|---|---|
| Andrew's | 3 of 185 |
| Ted's | 79 of 98 |
| Doug's | 11 of 17 |

**The caveat matters:** our trade history only reaches back to 2026-07-17. An account that is
enabled but simply has not traded a fraction since that date looks exactly the same as an account
that is disabled. So these numbers are a floor, not a count.

One thing this history did settle: **dividend reinvestment is switched off on all 185 of Andrew's
accounts**, which rules out reinvested dividends as the source of the fractional positions. Where
611 fractional positions actually came from is still unexplained.

### Do not try to answer the permission question from data — it is not there

This was checked properly and the answer is no:

- The fractional-trading permission **does not exist as a field** in Interactive Brokers' account
  information reference.
- There is a field named `tradingPermissions` that Interactive Brokers does not document anywhere.
- **No account tag in the programmatic interface exposes it either.**

The only two ways to learn whether an account is enabled are to look in the Advisor Portal screen,
or to send an order and read the rejection (error 10248) after the fact.

### Turning the permission on is Andrew's decision, not Ted's

The setting is **Advisor Portal → Settings → Client Settings → Trade in Fractions**. Two things
about it:

- It is **all-or-none across the entire client book.** There is no per-account switch.
- It **requires a signed disclosure.**

That makes it a firm-level decision for Andrew, and it should not be delegated with the cleanup task.

---

## PART C — The permanent fix, agreed but not built

Andrew's decision: this stops being a one-off cleanup and becomes a standing capability of the desk.

**What to build:** every Trade Execution run should finish by naming **what it could not place**,
and should drop a worklist into the client system with a **"Dust cleanup needed"** alert — the same
shape as the task being handed to Ted.

**Scope, stated tightly:** only the lines the rail could not place. It must never produce a file
that duplicates orders the desk already sent successfully.

**Worklist, not an executable basket file.** The worklist says which accounts, which tickers, and
"sell the whole position." That was chosen deliberately over generating an order file, because:

- A file **asserts quantities**, and a quantity can go stale between writing and executing — which
  risks selling more than the account actually holds.
- A worklist that has gone stale merely tells someone to clear something that is already clear.
  That is a wasted minute, not a bad trade.

**This is blocked on Part B. Do not build it until the preview check resolves.** If the answer to
the preview is "stop," the shape of the worklist may need to change.

---

## PART D — What shipped today, for the record

All of the following is already written, tested and committed:

- The kill switch now covers **every** rail that can place a live order, not just some of them.
- The Control Plane page was retired.
- The desk's own Action Center was retired, and its alerts now go to the client system instead.
- The menu was cut from thirteen items down to nine.
- Trade Execution was renamed, and given a thirty-minute freshness gate so it cannot act on
  stale figures.
- Strategy Models was merged into a single page, and the manual re-read control was put back.
- Strategy 8's pure strategy definition was folded into the shared strategies folder.
- **The out-of-spec scanner was fixed.** It had been scanning zero of 301 accounts and reporting
  that everything was in spec. It is now scoped correctly to Andrew's book.
- Tiingo was unstuck after 121 consecutive bad runs.
- The withdrawal cash-raise check was fixed **before its first real run on 2026-09-20**.
- The withdrawal schedule now reads live from the client system instead of a hand-typed list.
- The desk dashboard no longer shows failed jobs as green.
- The nightly data-quality check no longer re-flags historic crash days forever.
- The heartbeat alarm now writes an overnight failure down, so the morning digest actually
  reports it instead of forgetting it.

---

## PART E — Still open, not started

**1. The desk-launch button silently throws away a tap.**
In `livebot/s8_desk_launch_link.py`, the function that reads pending launch requests returns
"nothing" when the client system cannot be read — and its own comment says that must never be
treated as "no requests pending." The caller does exactly that anyway. The result: Andrew taps the
button, the client system is briefly unreadable, the tap is discarded, and **the gateway never
starts** with no error anywhere. This is the same silent-success failure as the dust problem, in a
different place.

**2. The `paperbot/execution/` folder reorganisation.** Deliberately deferred — low value, high risk.

**3. Thirty accounts are still genuinely out of spec, totalling $2.11 million.** This is dominated
by four accounts with cash parked in BUCK. Tasks already exist in the client system's task list for
Whittaker, Stallbaumer and Walter, plus one to meet with Lance Kellner, whose account U24240367 is
now flagged as no-trade.

**4. Three alerts are open in the client system:**

| Alert | What to do |
|---|---|
| The trading-desk test alert | Housekeeping; can be cleared. |
| "115 of 301 accounts were not checked" | **Stale false alarm.** Already fixed at the source; safe to dismiss. |
| "30 of 186 accounts out of spec" | **Genuine.** This is item 3 above. |

---

## THE SINGLE NEXT ACTION

Open the Advisor Portal's Allocation Order Tool, select the affected accounts, choose BIL, and
click **Calculate Proposed Orders**. Read the number.

A decimal means go. A zero means stop and rethink the route. Nothing transmits either way.
