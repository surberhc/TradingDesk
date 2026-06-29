# The "regime sharp_recovery refinement" lead — in plain English

*For Andrew. No code. Written 2026-06-29. Nothing here is adopted — this is a research lead only.*

## The 30-second version
S0's crisis brakes work great and we're not touching them. The problem is the
*recovery* side: after a scare, the strategy is sometimes slow to get back in,
and it sometimes hits the brakes on a tiny dip that immediately bounces back —
selling low and buying back higher, for no protection. That bleeds a little
return every year. The "sharp_recovery" rule is a shortcut meant to fix the
slow-re-entry case, but it's firing in the wrong kind of market. This lead asks:
can we tighten that rule so it only fires when it should — and is it even worth
the effort, given how easy it is to fool ourselves here?

## 1. The big picture — what's actually broken
The "regime engine" is the part of S0 that decides how much to be in stocks
versus how much to be defensive (bonds, gold, cash). It reads the market and
shifts gears.

In a real crisis it gets defensive fast, and that's genuinely good — it dodged
the big drawdowns (COVID, 2022, late-2018, etc.). **Leave that alone.** We
proved over a full week of testing that you can't improve the crisis brakes
without making something else worse.

The leak is on the other side. Two specific weaknesses cost us roughly
~2.3%/year versus just holding the stock market:

- **Re-entry lag** — after the market bottoms and starts climbing, the engine
  rebuilds its stock position one notch per month. Off a fast bottom, that's too
  slow, so it sits in cash and misses part of the rebound.
- **Shallow-dip whipsaws** — the engine sometimes cuts stocks hard on a *small*
  (~5–9%) dip that then snaps right back. It sells near the low and rebuys
  higher. That round-trip is a pure loss — it bought no protection because there
  was no crisis. This is the main thing we're chasing.

## 2. What "sharp_recovery override" is today
"Override" just means *a rule that overrides the normal slow ladder.* The normal
re-entry is cautious and stepwise. The override is an escape hatch for one
situation: a **V-shaped recovery** — where the market crashes and then rockets
straight back up (think the spring 2020 snap-back). In a clean V, the cautious
ladder is *too* cautious and would strand us in cash while stocks run away.

So the override says, in plain terms: *"If we've been stuck below full
investment for a while AND the market has clearly recovered, stop laddering —
jump straight back to fully invested."*

Today the "has clearly recovered" test is loose: it just checks that the
market's health score is high and price is back above its long-term trend line.
That's the knob in question.

## 3. The actual lead — the rule fires in the wrong markets
The override is doing its job in real V-recoveries. The trouble is it *also*
fires in slow, choppy, sideways "grind" markets — the 2015–16 stretch is the
poster child. That's not a clean V; it's a drifting, range-bound market. Firing
the "jump all-in now" override there is exactly wrong, and it's the single case
that sank our last attempt to speed up re-entry (more on that below).

**The refinement idea:** sharpen the override's trigger so it only fires on a
genuine, clean V-shaped bounce, and stays quiet in sideways grinds. Then re-run
our full crisis-by-crisis safety check and see whether that finally lets us cut
the whipsaw bleed without hurting any historical episode.

(Context: last week we tried a blunter version of "get back in faster" — we
shortened the max wait before the override can fire, from 6 months to 3. It
looked like a free win at first, but the final per-crisis safety check failed
it: three episodes got meaningfully worse, and the worst was — you guessed it —
the 2015–16 sideways grind. So we *held* it. The wait stays at 6. This new lead
is the more surgical follow-up: instead of just firing sooner, make the trigger
*smarter* about what counts as a real recovery.)

## 4. The honest risk — this is easy to fool ourselves on
This lead carries **high "curve-fitting" risk**, and you should weigh that
heavily.

Curve-fitting means tuning a rule until it looks perfect on the *past* data
we've already seen — but all we've really done is memorize history, not learn
something that will hold up in the future. It's like writing an exam answer key
*after* seeing the exam, then bragging about your score.

The danger here is obvious: we already *know* the rule fails on 2015–16, so it's
tempting to just twist the trigger until 2015–16 passes. That would be textbook
curve-fitting and would likely break the next time the market does something we
haven't seen.

So the plan, if we pursue it, is gated hard:
- The fix has to be a **principled** change — a real reason why "this is a V and
  that is a grind" — **not** a magic number reverse-engineered to rescue 2015–16.
- It has to be tested on **data it wasn't tuned on** (out-of-sample) to prove
  it's a real distinction, not memorization.
- It must not make **any** historical crisis episode worse.
- Until and unless it clears all of that, **nothing changes** — the re-entry
  wait stays at its current safe setting (6).

## 5. Bottom line — what we're asking you
Nothing is adopted and no code or settings have changed. This is a *research
lead*, and the question for you is simply:

**Is it worth spending effort chasing this?**

The case *for*: the whipsaw bleed is the last real, identified leak in an
otherwise very solid strategy, and this is the most surgical idea we have for it.

The case *against*: every structural tweak we tried last week failed its safety
gate, the engine keeps proving itself robust, and this particular idea is the
most curve-fit-prone of the bunch (we'd be tuning a rule to fix the one case we
know it fails).

A reasonable read is: it's worth *one* carefully-gated attempt, with the bar set
high and a quick willingness to drop it — exactly like we dropped the gamma
overlay, the weekly cadence, and the flow gate. If it can't clear the
out-of-sample and per-crisis bars, we shelve it and bank the (valuable)
conclusion that the engine just can't be improved here.
