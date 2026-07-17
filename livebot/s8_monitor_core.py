"""s8_monitor_core.py — S8 shadow-monitor PURE EXIT-LOGIC CORE (Phase 2a).

The correctness-critical heart of the streaming exit monitor, deliberately carved out
as 100% pure functions over synthetic samples:

  * NO IBKR, NO network, NO store I/O, NO threads, NO clock. Nothing here connects,
    subscribes, or writes a file. It takes a stored open position plus a stream of
    price samples and decides — deterministically — when and how the (would-be) exit
    fires and what the P&L / MAE were.

This is an OBSERVATION layer over the FROZEN S8 exit mechanics (rule #1 stays clean).
It does NOT reimplement the stop: the stop_price is computed at entry by
``s8_strategy.stop_price(realized_credit, stop_multiple)`` and stored on the position;
this module only compares live prices against that already-frozen level.

FROZEN EXIT MECHANICS (implemented exactly, not invented)
---------------------------------------------------------
An S8 position is a credit spread. At entry we SOLD the short leg (received short_bid)
and BOUGHT the long leg (paid long_ask); the net received is ``realized_credit`` (points).

  * The stop is a BUY-to-close on the SHORT leg. It fires when the short leg's current
    ASK reaches ``stop_price`` (we buy to close at the ask):  short_ask >= stop_price.
    exit_reason = "stop_hit".
  * B2 rule: when the stop fires, the LONG leg is closed simultaneously at market
    (sell to close at its current BID). The pnl formula below already accounts for this.
  * If the stop never fires by session end: exit_reason = "eod" (or "expiry" at
    expiration) — both legs valued at their final marks.

P&L per spread (x100 x qty):

    spread_close_value = short_ask - long_bid      (net debit to close the spread now)
    pnl = (realized_credit - spread_close_value) * 100 * qty

Closing the spread CHEAPER than the credit received  => positive pnl.
Getting stopped out (closing RICHER than the credit) => negative pnl.

max_adverse_excursion (MAE) = the most-negative running pnl observed over the life of
the position (the min pnl across all samples).

TIMESTAMP CONVENTION
--------------------
``ts`` on both MonitorPosition.entry_ts and Sample.ts is EPOCH SECONDS (a float or int;
e.g. time.time()). This is the single convention this core uses — it never parses ISO
strings, so the pure functions stay clock-free and trivially testable. The live 2b layer
is responsible for converting to/from the ISO strings that s8_schema stores, and
``build_exit_info`` returns exit_ts as whatever ts type the samples carried. duration_secs
is computed as the plain arithmetic difference ``exit_ts - entry_ts`` (seconds), or None
if either endpoint is missing.

GREEKS ARE NOT POPULATED HERE
-----------------------------
This core is PRICE-ONLY. The per-leg exit LegGrab-style dicts that ``build_exit_info``
emits carry the exit-sample quote fields (bid/ask/last, strike, right, underlying_spot,
grab_ts) but leave delta/gamma/vega/theta/iv as None. The live Phase-2b layer attaches
the full model greeks to the exit legs; do not expect greeks from this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


# --------------------------------------------------------------------------- #
# Inputs: the stored open position, and one live price sample
# --------------------------------------------------------------------------- #

@dataclass
class MonitorPosition:
    """The fields needed to evaluate exits for one open S8 credit spread.

    Sourced from a stored open ``s8_schema.TradeRecord`` (trade_id + side + qty from the
    record's top level; short/long strikes, realized_credit and stop_price from its
    EntryInfo; entry_ts as epoch seconds). ``stop_price`` was computed at entry by the
    frozen ``s8_strategy.stop_price`` and is used here verbatim — never recomputed.
    """

    trade_id: str
    side: Optional[str] = None                 # "PUT" / "CALL" (informational)
    short_strike: Optional[float] = None
    long_strike: Optional[float] = None
    qty: int = 1
    realized_credit: float = 0.0               # points received at entry (net credit)
    stop_price: float = 0.0                     # frozen short-leg stop level (points)
    entry_ts: Optional[float] = None            # epoch seconds


@dataclass
class Sample:
    """One live price sample of the two legs (+ underlying) at one instant.

    All prices are Optional: a feed can drop a side momentarily. Any evaluation that
    needs a missing price returns None / is skipped cleanly rather than crashing.
    ``ts`` is epoch seconds (see module docstring).
    """

    ts: Optional[float] = None
    short_bid: Optional[float] = None
    short_ask: Optional[float] = None
    short_last: Optional[float] = None
    long_bid: Optional[float] = None
    long_ask: Optional[float] = None
    long_last: Optional[float] = None
    spot: Optional[float] = None


# --------------------------------------------------------------------------- #
# State: the running result of feeding samples through the monitor
# --------------------------------------------------------------------------- #

@dataclass
class MonitorState:
    """Running monitor state for one position. A fresh state is un-triggered.

    triggered      : has an exit fired yet.
    exit_reason    : "stop_hit" / "eod" / "expiry" / None.
    mae            : most-negative running pnl seen so far (0.0 until a sample prices
                     a loss; stays 0.0 if the position was never underwater). None only
                     if no priceable sample has ever been seen.
    last_pnl       : pnl at the most recent priceable sample (None until one exists).
    exit_sample    : the Sample at which the exit fired (crossing sample for a stop;
                     the final sample for eod/expiry). None until triggered.
    n_samples      : count of samples passed to process_sample (priceable or not).
    """

    triggered: bool = False
    exit_reason: Optional[str] = None
    mae: Optional[float] = 0.0
    last_pnl: Optional[float] = None
    exit_sample: Optional[Sample] = None
    n_samples: int = 0


# --------------------------------------------------------------------------- #
# Pure evaluation
# --------------------------------------------------------------------------- #

def pnl_at(position: MonitorPosition, sample: Sample) -> Optional[float]:
    """Running P&L (dollars) of the spread if closed at this sample's marks.

        spread_close_value = short_ask - long_bid
        pnl = (realized_credit - spread_close_value) * 100 * qty

    Needs short_ask and long_bid (the prices at which we'd close: buy the short back at
    its ask, sell the long out at its bid). Returns None if either is missing — the
    close value is genuinely unknown then, so we don't guess.
    """
    if sample is None:
        return None
    short_ask = sample.short_ask
    long_bid = sample.long_bid
    if short_ask is None or long_bid is None:
        return None
    spread_close_value = float(short_ask) - float(long_bid)
    qty = position.qty if position.qty is not None else 1
    return (float(position.realized_credit) - spread_close_value) * 100.0 * float(qty)


def process_sample(
    position: MonitorPosition, state: MonitorState, sample: Sample
) -> MonitorState:
    """Fold one sample into the state and return the (mutated) state.

    Always: increment n_samples; if the sample is priceable, update last_pnl and the
    running MAE (worst/min pnl).

    Trigger (only if NOT already triggered): if short_ask is present and
    ``short_ask >= position.stop_price``, the stop fires — mark triggered, set
    exit_reason="stop_hit", and record this sample as the exit_sample.

    IDEMPOTENT: once triggered, a later sample never re-triggers and never overwrites
    exit_reason or exit_sample (it still counts toward n_samples and still updates
    last_pnl / MAE, which are life-of-position statistics). A sample missing short_ask
    simply skips the trigger check without crashing or false-firing.
    """
    state.n_samples += 1

    pnl = pnl_at(position, sample)
    if pnl is not None:
        state.last_pnl = pnl
        if state.mae is None or pnl < state.mae:
            state.mae = pnl

    if not state.triggered:
        short_ask = sample.short_ask
        if short_ask is not None and float(short_ask) >= float(position.stop_price):
            state.triggered = True
            state.exit_reason = "stop_hit"
            state.exit_sample = sample

    return state


def close_at_session_end(
    position: MonitorPosition,
    state: MonitorState,
    final_sample: Optional[Sample],
    reason: str = "eod",
) -> MonitorState:
    """Close out anything still open at session end.

    If not already triggered: mark triggered, set exit_reason=reason ("eod" by default,
    or "expiry"), and record final_sample as the exit_sample.

    If already triggered (the stop fired intraday): NO-OP — the stop exit stays intact
    (idempotent). This lets the caller unconditionally call this at close without erasing
    a stop that already happened.
    """
    if state.triggered:
        return state
    state.triggered = True
    state.exit_reason = reason
    state.exit_sample = final_sample
    return state


# --------------------------------------------------------------------------- #
# Emit the exit record (s8_schema ExitInfo-shaped dict)
# --------------------------------------------------------------------------- #

def _exit_leg_dict(
    strike: Optional[float],
    right: Optional[str],
    bid: Optional[float],
    ask: Optional[float],
    last: Optional[float],
    spot: Optional[float],
    grab_ts: Optional[float],
) -> Dict[str, Any]:
    """A LegGrab-shaped dict for one exit leg — PRICE-ONLY.

    Greeks (delta/gamma/vega/theta/iv) are intentionally None here and complete=False:
    this pure core never computes greeks; the live Phase-2b layer attaches them. Keys
    mirror s8_schema.LegGrab so 2b can drop these straight into a LegGrab.from_dict.
    """
    return {
        "right": right,
        "strike": strike,
        "bid": bid,
        "ask": ask,
        "last": last,
        "bid_size": None,
        "ask_size": None,
        "volume": None,
        "open_interest": None,
        "delta": None,
        "gamma": None,
        "vega": None,
        "theta": None,
        "iv": None,
        "underlying_spot": spot,
        "grab_ts": grab_ts,
        "complete": False,      # greeks not populated by this price-only core
    }


def build_exit_info(position: MonitorPosition, state: MonitorState) -> Dict[str, Any]:
    """Build an s8_schema.ExitInfo-shaped dict from the finished monitor state.

    Fields:
      exit_ts               : exit_sample.ts (epoch seconds; None if no exit sample)
      exit_reason           : state.exit_reason
      exit_spot             : exit_sample.spot
      spread_value_at_exit  : short_ask - long_bid at the exit sample (net debit to
                              close) — None if either price is missing
      pnl                   : pnl_at(position, exit_sample)
      max_adverse_excursion : state.mae
      duration_secs         : exit_ts - entry_ts (seconds), None if either endpoint None
      short_leg_exit /
      long_leg_exit         : price-only LegGrab-style dicts from the exit sample
                              (greeks None — populated by Phase-2b, not here)

    Returns exit_ts/pnl/legs as None when the monitor never produced an exit sample
    (e.g. a session with no priceable close sample) rather than fabricating values.
    """
    sample = state.exit_sample

    exit_ts = sample.ts if sample is not None else None
    exit_spot = sample.spot if sample is not None else None

    spread_value_at_exit: Optional[float] = None
    pnl: Optional[float] = None
    short_leg_exit: Optional[Dict[str, Any]] = None
    long_leg_exit: Optional[Dict[str, Any]] = None

    if sample is not None:
        if sample.short_ask is not None and sample.long_bid is not None:
            spread_value_at_exit = float(sample.short_ask) - float(sample.long_bid)
        pnl = pnl_at(position, sample)
        short_leg_exit = _exit_leg_dict(
            strike=position.short_strike,
            right=position.side,
            bid=sample.short_bid,
            ask=sample.short_ask,
            last=sample.short_last,
            spot=sample.spot,
            grab_ts=sample.ts,
        )
        long_leg_exit = _exit_leg_dict(
            strike=position.long_strike,
            right=position.side,
            bid=sample.long_bid,
            ask=sample.long_ask,
            last=sample.long_last,
            spot=sample.spot,
            grab_ts=sample.ts,
        )

    duration_secs: Optional[float] = None
    if exit_ts is not None and position.entry_ts is not None:
        duration_secs = float(exit_ts) - float(position.entry_ts)

    return {
        "exit_ts": exit_ts,
        "exit_reason": state.exit_reason,
        "exit_spot": exit_spot,
        "short_leg_exit": short_leg_exit,
        "long_leg_exit": long_leg_exit,
        "spread_value_at_exit": spread_value_at_exit,
        "pnl": pnl,
        "max_adverse_excursion": state.mae,
        "duration_secs": duration_secs,
    }
