"""Disciplined-execution DECISION ENGINE for the CAN SLIM replica (COMPUTE-ONLY).

WHAT THIS IS
------------
A daily decision engine that turns the PROVEN winning ruleset into a concrete, ranked
action list for one trading day. It is the deployable sibling of execution_backtest.py:
the backtest asked "does this stack beat his book?" (yes: the `E3 + timing` config ~2.7x'd
his realized return at lower drawdown); THIS module runs that same, frozen ruleset forward
on TODAY's portfolio + prices so it can be used live on the desk.

THE FROZEN RULESET (E3 + timing — do NOT tune; see research/execution_backtest.md)
----------------------------------------------------------------------------------
  ENTRY   buy a pick at/near its pivot, within the buy zone (<= ~5% above the pivot).
          Only take entries that fit sizing, the concurrent-position cap, and the
          exposure dial (invested_pct). Otherwise hold cash.
  INITIAL a catastrophic -7% stop from entry, ACTIVE ONLY until the position first closes
  STOP    above a RISING 50-day SMA. This kills immediate failures.
  WINNER  once a position has closed above a rising 50-day SMA, the -7% rule is RETIRED for
  MGMT    that name; thereafter HOLD and exit only on a DECISIVE close back below the
          50-day line (close < SMA_BUFFER * SMA50). NO profit cap — let winners run. (A
          fixed profit target was the single most destructive rule tested; it is not here.)
  SIZING  ~12% of equity target per position, ~18% hard cap, ~7 concurrent names.
  EXPOSURE gross deployed dollars gated by the market-pulse invested_pct (cash-timing dial).

This engine REUSES the already-validated primitives from execution_backtest.py (the -7% /
50-line thresholds, SMA_BUFFER, EW_TARGET/EW_CAP, the SMA function). It does NOT re-derive
the rules — it applies the SAME per-name state machine that simulate_exit() proved out, but
on a live book instead of a resolved historical path, and it adds the ENTRY side (which the
backtest held fixed to his picks).

NO LOOKAHEAD (desk causality rule)
----------------------------------
A decision for day D uses ONLY bars up to and including D. Per name, the caller supplies a
price series ending at (or before) D; the engine builds the 50-day SMA from that series and
never references a bar dated after D. The exposure dial value passed in must be the
prior-reading invested_pct (the caller is responsible for not handing in a same-or-future
dial). A guard raises if any supplied bar is dated after the decision date.

CRITICAL SAFETY BOUNDARY — COMPUTE ONLY
---------------------------------------
This module COMPUTES decisions. It NEVER submits, arms, or transmits an order. The desk's
review -> arm -> transmit gate is sacred and live wiring is a separate, deliberate, gated
step. The single place the paperbot dynamic order router would later consume this output is
marked `# === PAPERBOT SEAM ===` on the DayPlan. Nothing here imports paperbot, touches
arming state, or reaches an order path.

Usage (dry run over cached data):  python execution_engine.py
"""
from __future__ import annotations

import datetime as dt
import os
import sys
from dataclasses import dataclass, field
from typing import Optional

# Reuse the PROVEN, already-validated primitives — do not re-derive the ruleset.
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import execution_backtest as eb  # noqa: E402  (SMA, thresholds, cache loaders)

# ----------------------------------------------------------------------------- frozen knobs
# All imported from the backtest so the engine and the backtest CANNOT drift.
HARD_STOP = eb.HARD_STOP        # 0.07  catastrophic initial stop
SMA_BUFFER = eb.SMA_BUFFER      # 0.98  "decisive" break = close < 0.98 * SMA50
EW_TARGET = eb.EW_TARGET        # 0.12  per-position sizing target
EW_CAP = eb.EW_CAP              # 0.18  per-position hard cap
MAX_CONCURRENT = 7              # ~7 concurrent names (his revealed book)
BUY_ZONE = 0.05                 # a pick is buyable within +5% above its pivot
MIN_ROOM_FRAC = 0.005           # ignore <0.5%-of-equity crumbs of exposure room


class NoLookaheadError(AssertionError):
    """Raised if a supplied bar is dated after the decision day."""


# ----------------------------------------------------------------------------- inputs
@dataclass
class Position:
    """An OPEN position the desk currently holds."""
    symbol: str
    entry_date: dt.date
    entry_px: float
    dollars: float                 # capital deployed at entry (cost basis)
    sma_active: bool = False       # has this name already closed above a rising 50-SMA?
                                   # (persist this across days so the -7% stop stays retired
                                   #  once the 50-line rule has engaged — same latch as the
                                   #  backtest's per-name state machine.)


@dataclass
class Pick:
    """A candidate from the pluggable pick source (Doug's watch list or the detector)."""
    symbol: str
    pivot: float                   # the breakout pivot / buy point
    last_px: float                 # today's price (used for buy-zone + breakout check)
    breakout_confirmed: bool = True  # source asserts the breakout is confirmed


@dataclass
class MarketData:
    """Per-name price context for the decision day: today's close + a trailing close series
    (ending on or before `asof`) sufficient to compute a 50-day SMA (>=51 closes ideal)."""
    last_px: float
    closes: list[float]            # trailing daily closes, oldest..newest, newest == asof
    dates: Optional[list[dt.date]] = None  # optional, for the causality guard


# ----------------------------------------------------------------------------- outputs
@dataclass
class Action:
    kind: str                      # "ENTRY" | "HOLD" | "EXIT"
    symbol: str
    reason: str
    # entry fields
    target_dollars: Optional[float] = None
    entry_ref: Optional[float] = None      # pivot / buy-zone reference
    initial_stop: Optional[float] = None   # -7% level for a new entry
    # exit fields
    trigger: Optional[str] = None          # "stop_7pct" | "decisive_50sma_break"


@dataclass
class DayPlan:
    asof: dt.date
    entries: list[Action] = field(default_factory=list)
    holds: list[Action] = field(default_factory=list)
    exits: list[Action] = field(default_factory=list)
    cash_note: str = ""
    # === PAPERBOT SEAM =======================================================
    # This DayPlan is the ONLY thing the paperbot dynamic order router would later
    # consume. `exits` -> close orders, `entries` -> open orders sized to target_dollars
    # with `initial_stop` as the resting protective stop. That wiring is a SEPARATE,
    # gated review -> arm -> transmit step. Nothing in this module submits anything.
    # =========================================================================

    @property
    def actions(self) -> list[Action]:
        return self.exits + self.holds + self.entries


# ----------------------------------------------------------------------------- indicators (reuse)
def sma50(closes: list[float]) -> tuple[Optional[float], Optional[float]]:
    """(today's SMA50, yesterday's SMA50) from a trailing close series. Reuses eb.sma so the
    calculation is byte-identical to the validated backtest. None if <50 (or <51) closes."""
    s = eb.sma(closes, 50)
    today = s[-1] if s else None
    prev = s[-2] if len(s) >= 2 else None
    return today, prev


def _guard_causal(md: MarketData, asof: dt.date):
    if md.dates:
        if len(md.dates) != len(md.closes):
            raise NoLookaheadError("dates/closes length mismatch")
        for d in md.dates:
            if d > asof:
                raise NoLookaheadError(f"bar {d} is after decision day {asof}")


# ----------------------------------------------------------------------------- per-name exit logic
def evaluate_position(pos: Position, md: MarketData, asof: dt.date):
    """Apply the frozen per-name state machine to ONE open position for day `asof`.

    Returns (decision, updated_sma_active, reason, trigger) where decision in
    {"EXIT", "HOLD"}. Mirrors simulate_exit()'s E3 logic exactly, but on a live close:

      * If the 50-line rule has NOT yet engaged (sma_active False):
          - a close (proxy for the day's low in a daily engine) at/below the -7% stop -> EXIT.
          - if today closes above a RISING 50-SMA -> the -7% stop RETIRES (sma_active latches
            True); HOLD today.
          - else HOLD under the -7% stop.
      * If the 50-line rule HAS engaged (sma_active True):
          - a DECISIVE close below the 50-line (close < SMA_BUFFER*SMA50) -> EXIT.
          - else HOLD (let the winner run; NO profit cap).

    Note vs the intraday backtest: live EOD desk decisions act on the CLOSE, so the -7% stop
    here triggers on a closing breach (a resting broker stop, added at the seam, would catch
    the intraday touch — the engine flags the level via initial_stop on entry)."""
    _guard_causal(md, asof)
    c = md.last_px
    s_today, s_prev = sma50(md.closes)
    init_stop = pos.entry_px * (1 - HARD_STOP)

    if not pos.sma_active:
        if c <= init_stop:
            return "EXIT", pos.sma_active, (
                f"close {c:.2f} <= -7% stop {init_stop:.2f} (initial-stop breach; "
                "50-line rule had not engaged)"), "stop_7pct"
        rising = s_today is not None and s_prev is not None and s_today > s_prev
        if rising and c > s_today:
            return "HOLD", True, (
                f"closed {c:.2f} above rising 50-SMA {s_today:.2f} -> -7% stop RETIRED, "
                "now holding to the 50-line"), None
        stop_txt = f"{init_stop:.2f}"
        sma_txt = f"{s_today:.2f}" if s_today is not None else "n/a"
        return "HOLD", False, (
            f"under -7% stop {stop_txt} (close {c:.2f}); 50-line not yet engaged "
            f"(SMA50 {sma_txt})"), None

    # 50-line rule engaged
    if s_today is not None and c < SMA_BUFFER * s_today:
        return "EXIT", pos.sma_active, (
            f"decisive close {c:.2f} below 50-SMA {s_today:.2f} "
            f"(< {SMA_BUFFER:.2f}x) -> trend break"), "decisive_50sma_break"
    sma_txt = f"{s_today:.2f}" if s_today is not None else "n/a"
    return "HOLD", pos.sma_active, (
        f"holding winner above the 50-line (close {c:.2f}, SMA50 {sma_txt}); "
        "no profit cap"), None


# ----------------------------------------------------------------------------- entry buy-zone
def in_buy_zone(pick: Pick) -> tuple[bool, str]:
    """A pick is buyable if the breakout is confirmed AND price is at/near the pivot, within
    the +5% buy zone (never chase more than 5% above the pivot)."""
    if not pick.breakout_confirmed:
        return False, "breakout not confirmed"
    if pick.pivot <= 0:
        return False, "no pivot"
    above = pick.last_px / pick.pivot - 1.0
    if above < -0.001:
        return False, f"below pivot ({above * 100:.1f}%) - no breakout yet"
    if above > BUY_ZONE:
        return False, f"extended {above * 100:.1f}% above pivot (> {BUY_ZONE * 100:.0f}% buy zone) - chasing"
    return True, f"in buy zone (+{above * 100:.1f}% above pivot {pick.pivot:.2f})"


# ----------------------------------------------------------------------------- the daily engine
def decide_day(
    asof: dt.date,
    positions: list[Position],
    market: dict[str, MarketData],
    picks: list[Pick],
    invested_pct: Optional[float],
    cash: float,
) -> DayPlan:
    """Produce the ranked action list for `asof`.

    INPUTS
      asof          decision day (only data up to this day may be used).
      positions     current OPEN book (with per-name sma_active latch carried over).
      market        symbol -> MarketData (today's px + trailing closes) for every held name
                    AND every candidate. Missing data for a held name => HOLD (cannot act).
      picks         today's candidate list from the pluggable source (watch list / detector).
      invested_pct  the exposure dial (PRIOR reading). None => uncapped (100%).
      cash          current available cash.

    OUTPUT: a DayPlan (exits, holds, entries), each Action carrying a one-line reason and,
    for entries, target size / entry ref / initial stop. Respects sizing, the concurrent cap,
    and the exposure cap (never deploys past invested_pct * equity; holds cash otherwise).

    Ordering discipline: EXITS first (free cash + risk), then HOLDS, then ENTRIES ranked by
    buy-zone tightness (closest to pivot first — least extended = best entry)."""
    plan = DayPlan(asof=asof)

    # equity = cash + cost basis of open positions (entry-basis book value, same convention
    # as the backtest's exposure cap; conservative and causal).
    invested_dollars = sum(p.dollars for p in positions)
    equity = cash + invested_dollars

    # ---- 1. EXITS + HOLDS on the current book (per-name state machine) ----
    survivors: list[Position] = []
    freed_cash = 0.0
    for p in positions:
        md = market.get(p.symbol)
        if md is None:
            plan.holds.append(Action("HOLD", p.symbol,
                "no price data for the decision day -> cannot act, hold"))
            survivors.append(p)
            continue
        decision, new_active, reason, trigger = evaluate_position(p, md, asof)
        if decision == "EXIT":
            plan.exits.append(Action("EXIT", p.symbol, reason, trigger=trigger))
            freed_cash += p.dollars   # cost basis frees; the router realizes the fill
        else:
            p.sma_active = new_active  # latch the 50-line takeover
            plan.holds.append(Action("HOLD", p.symbol, reason))
            survivors.append(p)

    cash_after_exits = cash + freed_cash
    open_count = len(survivors)

    # ---- 2. ENTRIES, gated by buy-zone, sizing, concurrent cap, exposure dial ----
    cap_pct = invested_pct if invested_pct is not None else 1.0
    max_gross = cap_pct * equity
    invested_after_exits = sum(p.dollars for p in survivors)
    room = max(0.0, max_gross - invested_after_exits)

    # rank buyable picks by tightness to the pivot (least extended first)
    buyable = []
    for pk in picks:
        if pk.symbol in {p.symbol for p in survivors}:
            continue  # already hold it — no add/pyramid in this frozen ruleset
        ok, why = in_buy_zone(pk)
        if not ok:
            continue
        above = pk.last_px / pk.pivot - 1.0
        buyable.append((above, pk, why))
    buyable.sort(key=lambda x: x[0])

    for above, pk, why in buyable:
        if open_count >= MAX_CONCURRENT:
            plan.cash_note = (plan.cash_note + " ") if plan.cash_note else ""
            plan.cash_note += f"concurrent cap ({MAX_CONCURRENT}) reached - {pk.symbol} not taken."
            continue
        desired = EW_TARGET * equity
        size = min(desired, EW_CAP * equity, cash_after_exits, room)
        if size <= equity * MIN_ROOM_FRAC:
            plan.cash_note = (plan.cash_note + " ") if plan.cash_note else ""
            plan.cash_note += (f"no room for {pk.symbol}: exposure/cash exhausted "
                               f"(dial {cap_pct:.0%}).")
            continue
        stop = pk.last_px * (1 - HARD_STOP)
        plan.entries.append(Action(
            "ENTRY", pk.symbol,
            f"{why}; size ~{size / equity:.1%} of equity; initial stop -7% @ {stop:.2f}",
            target_dollars=round(size, 2), entry_ref=pk.pivot, initial_stop=round(stop, 2)))
        cash_after_exits -= size
        room -= size
        open_count += 1

    # cash / exposure summary line
    deployed = invested_after_exits + sum(a.target_dollars or 0 for a in plan.entries)
    plan.cash_note = (
        f"equity ${equity:,.0f}; exposure dial {cap_pct:.0%} -> max gross ${max_gross:,.0f}; "
        f"deployed ${deployed:,.0f} ({deployed / equity:.0%}); cash held ${equity - deployed:,.0f}."
        + ((" " + plan.cash_note) if plan.cash_note else ""))
    return plan


# ----------------------------------------------------------------------------- pluggable pick source
def picks_from_list(rows: list[dict]) -> list[Pick]:
    """Adapter: turn a passed-in list (Doug's watch list rows, or the detector's output) into
    Pick objects. Each row needs symbol, pivot, last_px; breakout_confirmed defaults True.
    Swap this for a detector adapter later without touching the engine."""
    out = []
    for r in rows:
        out.append(Pick(
            symbol=str(r["symbol"]).upper(),
            pivot=float(r["pivot"]),
            last_px=float(r["last_px"]),
            breakout_confirmed=bool(r.get("breakout_confirmed", True))))
    return out


# ----------------------------------------------------------------------------- DRY RUN
def _closes_upto(bars, asof):
    """Trailing (dates, closes) with all data on/before asof (causal slice). bars = list of
    (date,o,h,l,c) from eb.load_paths()."""
    ds, cs = [], []
    for (d, o, h, l, c) in bars:
        if d <= asof and c is not None:
            ds.append(d); cs.append(c)
    return ds, cs


def _last_close_on_or_before(bars, asof):
    for (d, o, h, l, c) in reversed(bars):
        if d <= asof and c is not None:
            return d, c
    return None, None


def dry_run(months_back: int = 4):
    """Walk the engine forward over the last few months of CACHED data and print the daily
    action list. This is a SANITY demonstration, not a backtest: it seeds a book from a few
    of his real names near their entries, feeds each trading day's real closes, and lets the
    engine manage exits + consider a rotating pick list. Shows the engine produces sensible,
    causal decisions day over day. COMPUTE ONLY — nothing is submitted."""
    paths = eb.load_paths()
    timing = eb.load_timing()
    if not paths:
        print("[dry-run] no cached price paths found; skipping.")
        return

    # decision-day span: last `months_back` months up to the last cached bar.
    last_bar = max(d for bars in paths.values() for (d, *_ ) in bars)
    start = last_bar - dt.timedelta(days=30 * months_back)

    # business days in the window that actually appear in the cache
    all_days = sorted({d for bars in paths.values() for (d, *_) in bars if start <= d <= last_bar})

    # seed a small starting book: pick a few liquid names present in the cache, "entered" ~1
    # month before the window at their close then, so the engine has live positions to manage.
    seed_syms = [s for s in ("AXON", "ANET", "APP", "ARM", "AMSC") if s in paths][:4]
    seed_date = start - dt.timedelta(days=1)
    equity0 = 650_000.0
    positions: list[Position] = []
    used = 0.0
    for s in seed_syms:
        d0, c0 = _last_close_on_or_before(paths[s], seed_date)
        if c0 is None:
            continue
        dollars = round(EW_TARGET * equity0, 2)
        positions.append(Position(s, d0, c0, dollars, sma_active=False))
        used += dollars
    cash = equity0 - used

    # a rotating candidate pool: any OTHER cached names; each day we offer up to 3 whose price
    # is currently near a synthetic pivot (prior 20-day high) — a stand-in pick source so the
    # ENTRY path exercises. (In production this is Doug's watch list / the detector.)
    pool = [s for s in sorted(paths) if s not in seed_syms][:40]

    def pivot_and_px(sym, asof):
        ds, cs = _closes_upto(paths[sym], asof)
        if len(cs) < 25:
            return None
        pivot = max(cs[-21:-1])       # prior 20-day high as a synthetic breakout pivot
        return pivot, cs[-1]

    print(f"[dry-run] {len(all_days)} trading days {all_days[0]} .. {all_days[-1]}; "
          f"seed book: {', '.join(seed_syms)}; pick pool {len(pool)} names.\n")

    # only print days where something happens (entry/exit) plus a periodic heartbeat
    shown = 0
    for i, day in enumerate(all_days):
        market: dict[str, MarketData] = {}
        for s in {p.symbol for p in positions} | set(pool):
            ds, cs = _closes_upto(paths[s], day)
            if cs:
                market[s] = MarketData(last_px=cs[-1], closes=cs, dates=ds)

        picks: list[Pick] = []
        for s in pool:
            pv = pivot_and_px(s, day)
            if pv is None:
                continue
            pivot, px = pv
            picks.append(Pick(s, pivot=pivot, last_px=px, breakout_confirmed=True))

        inv = eb.prior_week_invested(timing, day)
        plan = decide_day(day, positions, market, picks, invested_pct=inv, cash=cash)

        # apply exits/entries to carry the book forward (dry-run bookkeeping only)
        if plan.exits or plan.entries or i % 21 == 0:
            shown += 1
            print(f"=== {day} | dial {'%.0f%%' % (inv*100) if inv is not None else 'n/a'} "
                  f"| open {len(positions)} | cash ${cash:,.0f} ===")
            for a in plan.exits:
                print(f"  EXIT  {a.symbol:5s} [{a.trigger}] {a.reason}")
            for a in plan.entries:
                print(f"  ENTRY {a.symbol:5s} ${a.target_dollars:,.0f} @stop {a.initial_stop} - {a.reason}")
            if i % 21 == 0 and not (plan.exits or plan.entries):
                # heartbeat: show a couple of holds so we see the winners running
                for a in plan.holds[:2]:
                    print(f"  hold  {a.symbol:5s} {a.reason}")
            print(f"  {plan.cash_note}\n")

        # bookkeeping: realize exits, open entries
        exit_syms = {a.symbol for a in plan.exits}
        if exit_syms:
            keep = []
            for p in positions:
                if p.symbol in exit_syms:
                    _, cx = _last_close_on_or_before(paths[p.symbol], day)
                    cash += p.dollars * (cx / p.entry_px) if cx else p.dollars
                else:
                    keep.append(p)
            positions = keep
        for a in plan.entries:
            _, cx = _last_close_on_or_before(paths[a.symbol], day)
            if cx:
                positions.append(Position(a.symbol, day, cx, a.target_dollars, sma_active=False))
                cash -= a.target_dollars

    print(f"[dry-run] done. {shown} event/heartbeat days printed. "
          f"final: {len(positions)} open, cash ${cash:,.0f}. COMPUTE-ONLY — nothing submitted.")


if __name__ == "__main__":
    dry_run()
