"""
s8_capture.py — S8 live-pilot RICH ENTRY CAPTURE (Phase 1 of the S8 live-pilot
data-capture build; see docs/S8_LIVE_PILOT_DATA_CAPTURE_PLAN.md, component 2).

WHAT THIS IS FOR
----------------
When the S8 runner APPROVES a (would-be) entry in PILOT_MODE, this module grabs a full
real-data snapshot of BOTH legs — quotes/sizes/volume/OI PLUS the model greeks/IV — along
with spot and VIX at the entry instant, assembles an s8_schema.TradeRecord (status="open"),
and persists it via s8_store. It is a pure OBSERVATION layer wrapped around the frozen S8
strategy: it changes NOTHING about how S8 picks entries or exits (rule #1 stays clean), and
it never places, modifies, or transmits an order (zero-transmit preserved — there is no
ib.placeOrder / order_router.place path anywhere in this file).

THE GREEKS SETTLE-DELAY FIX (the whole point of Phase 1)
-------------------------------------------------------
The earlier bare-snapshot path recorded short_delta=null because IBKR's model greeks arrive
only AFTER a market-data subscription settles — a snapshot read immediately after subscribing
sees ticker.modelGreeks is None. grab_leg_live() therefore SUBSCRIBES (streaming, not a
one-shot snapshot) and WAITS with a bounded timeout for ticker.modelGreeks to populate before
harvesting, then records what is present and flags completeness rather than silently writing
nulls (plan Risk #3).

STRUCTURE
---------
  * leg_grab_from_ticker(ticker, right, strike)  PURE, offline-testable — extract one leg's
      quotes + greeks from an ib_async Ticker-like object into an s8_schema.LegGrab.
  * build_entry_trade_record(...)                PURE, offline-testable — assemble an
      "open" TradeRecord from a SpreadPick + two LegGrabs + entry context.
  * grab_leg_live(ib, contract, ...)             LIVE — subscribe, bounded-wait for greeks
      THEN a short bounded-wait for open interest, harvest, cancel. Thin; verified against
      the installed ib_async, not offline-unit-tested (wait_for_oi's logic IS unit-tested).
  * grab_vix_live(ib, ...)                        LIVE — best-effort VIX last/close.
  * capture_and_persist_entry(ib, pick, ...)     LIVE — the top-level entry hook. BEST-EFFORT:
      it NEVER raises into the caller; any failure returns None so the pilot cycle is never
      broken by a capture problem.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any, Optional

from ib_async import Index, Option

# version.py lives in paperbot/ (see s8_runner.py's own sys.path shim for the same reason —
# the livebot/ package split left version.py, ledger.py, order_router.py in paperbot/). Add
# paperbot/ so `import version` resolves for the Provenance stamp. Path is derived from
# __file__ (per CLAUDE.md), never an absolute string.
_PAPERBOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "paperbot")
if _PAPERBOT not in sys.path:
    sys.path.insert(0, _PAPERBOT)

import s8_chain            # noqa: E402  (sibling in livebot/)
import s8_schema           # noqa: E402
import s8_store            # noqa: E402
import s8_vol              # noqa: E402  (sibling in livebot/)
import version             # noqa: E402  (from paperbot/)

_CT_ZONE = ZoneInfo("America/Chicago")

# SpreadPick.side ("PUT"/"CALL") -> ib_async Option.right ("P"/"C"). Same mapping direction
# as s8_runner._SIDE_TO_OPT_RIGHT; duplicated (not imported) to keep this module importable
# without pulling in the whole runner (which would be a circular import — the runner imports
# THIS module).
_SIDE_TO_RIGHT = {"PUT": "P", "CALL": "C"}


# --------------------------------------------------------------------------- #
# NaN/None normaliser (same convention as s8_chain._num / ibkr_forward._num)
# --------------------------------------------------------------------------- #
def _num(x: Any) -> Optional[float]:
    """IBKR returns NaN for missing numerics; normalise NaN/None -> None, else float."""
    if x is None:
        return None
    try:
        return None if x != x else float(x)   # NaN != NaN
    except (TypeError, ValueError):
        return None


def _is_call(right: Any) -> bool:
    """True if `right` denotes a CALL (accepts ib_async 'C'/'CALL' spellings)."""
    return None if right is None else str(right) in ("C", "CALL", "Call", "call")


def oi_from_ticker(ticker: Any, right: Any) -> Optional[float]:
    """Right-appropriate open interest off an ib_async Ticker-like object.

    Calls and puts carry OI on distinct ticker fields: ``callOpenInterest`` for a call,
    ``putOpenInterest`` for a put. Reads the correct one for `right` and normalises
    NaN/None (the "not ticked in yet" state) to None. Falls back gracefully to None when
    the field is absent entirely.
    """
    oi_field = "callOpenInterest" if _is_call(right) else "putOpenInterest"
    return _num(getattr(ticker, oi_field, None))


# --------------------------------------------------------------------------- #
# PURE: Ticker -> LegGrab
# --------------------------------------------------------------------------- #
def leg_grab_from_ticker(ticker: Any, right: Any, strike: Any) -> s8_schema.LegGrab:
    """Extract one option leg's full data grab from an ib_async Ticker-like object.

    Reads quotes/sizes/volume from the top-level ticker fields and delta/gamma/vega/theta/
    IV/underlying-spot from ``ticker.modelGreeks``. Open interest is taken from the
    right-appropriate field (``callOpenInterest`` for calls, ``putOpenInterest`` for puts).

    ``complete`` is True ONLY when ``ticker.modelGreeks`` was present and non-None at grab
    time (the settle-delay flag — see module docstring); an incomplete grab records whatever
    quotes are present with all greeks None and complete=False, rather than silently writing
    null greeks as if they were real. All NaN/None numerics are normalised to None.
    """
    right_str = None if right is None else str(right)

    greeks = getattr(ticker, "modelGreeks", None)
    complete = greeks is not None

    delta = _num(getattr(greeks, "delta", None)) if complete else None
    gamma = _num(getattr(greeks, "gamma", None)) if complete else None
    vega = _num(getattr(greeks, "vega", None)) if complete else None
    theta = _num(getattr(greeks, "theta", None)) if complete else None
    iv = _num(getattr(greeks, "impliedVol", None)) if complete else None
    und = _num(getattr(greeks, "undPrice", None)) if complete else None

    # Right-appropriate open interest (calls vs puts have distinct ticker fields).
    open_interest = oi_from_ticker(ticker, right)

    return s8_schema.LegGrab(
        right=right_str,
        strike=_num(strike),
        bid=_num(getattr(ticker, "bid", None)),
        ask=_num(getattr(ticker, "ask", None)),
        last=_num(getattr(ticker, "last", None)),
        bid_size=_num(getattr(ticker, "bidSize", None)),
        ask_size=_num(getattr(ticker, "askSize", None)),
        volume=_num(getattr(ticker, "volume", None)),
        open_interest=open_interest,
        delta=delta,
        gamma=gamma,
        vega=vega,
        theta=theta,
        iv=iv,
        underlying_spot=und,
        grab_ts=datetime.now(tz=_CT_ZONE).isoformat(timespec="milliseconds"),
        complete=complete,
    )


# --------------------------------------------------------------------------- #
# PURE: SpreadPick + LegGrabs + context -> open TradeRecord
# --------------------------------------------------------------------------- #
def _date_slot_from_ts(entry_ts: Any) -> tuple[Optional[str], Optional[str]]:
    """Derive (YYYYMMDD date, HH:MM WALL-CLOCK MINUTE) from an ISO entry timestamp string.

    WARNING — the second return value is the WALL-CLOCK MINUTE, **not** the ENTRY_GRID_CT
    slot. They are different things and must never be conflated:

      * the GRID SLOT is the schedule label (e.g. "08:45") that s8_runner.due_templates
        returns for any cycle within DUE_TOLERANCE_MINUTES (+/-2 min) of it;
      * the WALL-CLOCK MINUTE is whenever the capture actually ran (08:43, 08:44, 08:45...).

    Conflating them broke the store-backed idempotency guard: s8_service.slot_already_entered
    searches the store for the GRID slot, but the persisted TradeRecord.slot held the
    wall-clock minute, so the guard never matched and the SAME grid slot was re-entered on
    every ~30s entry cycle for the whole +/-2min tolerance window (observed live 2026-07-20:
    grid slots 08:45 and 08:50 each produced four entry batches). The GRID SLOT must be
    PASSED IN (build_entry_trade_record's `slot` parameter, threaded from
    s8_runner.evaluate_and_capture_due_template) — never re-derived from a timestamp.

    Only the DATE half of this function's output is used on the normal path. Returns
    (None, None) if entry_ts is unusable.
    """
    if not entry_ts or not isinstance(entry_ts, str) or len(entry_ts) < 16:
        return None, None
    try:
        date = entry_ts[:10].replace("-", "")
        slot = entry_ts[11:16]
        return date, slot
    except Exception:
        return None, None


def build_entry_trade_record(
    pick: Any,
    template_cfg: dict,
    account: Optional[str],
    qty: Optional[int],
    entry_ts: Optional[str],
    slot: Optional[str],
    entry_spot: Optional[float],
    entry_vix: Optional[float],
    entry_realized_vol: Optional[float],
    short_leg: s8_schema.LegGrab,
    long_leg: s8_schema.LegGrab,
    stop_price: Optional[float],
    paperbot_version: Optional[str],
    pilot_mode: bool,
) -> s8_schema.TradeRecord:
    """Assemble a well-formed status="open" TradeRecord for one approved (would-be) entry.

    Fills the entry group from the frozen SpreadPick + the two live LegGrabs + entry-time
    context; exit stays None (Phase 2's shadow-monitor fills it). ``greeks_complete`` is the
    conjunction of both legs' ``complete`` flags.

    ``slot`` MUST be the ENTRY_GRID_CT schedule label (e.g. "08:45") for the slot being
    entered — the SAME key ``s8_service.slot_already_entered`` looks up in the store. It is
    threaded down from ``s8_runner.evaluate_and_capture_due_template``. It is used for both
    ``TradeRecord.slot`` and the ``trade_id``, so the idempotency key stored and the
    idempotency key queried are one and the same thing (see _date_slot_from_ts's warning for
    the bug this prevents). ``date`` is still derived from entry_ts, and ``entry_ts`` itself
    keeps the TRUE wall-clock entry instant on ``EntryInfo.entry_ts`` — the grid label never
    overwrites it.

    FALLBACK: if `slot` is None/empty this falls back to the wall-clock minute derived from
    entry_ts AND prints a loud warning — the degraded key is never silent.
    """
    date, ts_slot = _date_slot_from_ts(entry_ts)
    if slot:
        slot_key = slot
    else:
        slot_key = ts_slot
        print(f"    !! s8_capture: NO GRID SLOT PASSED to build_entry_trade_record — falling "
              f"back to the WALL-CLOCK minute {ts_slot!r} for TradeRecord.slot/trade_id. "
              f"THE IDEMPOTENCY KEY IS DEGRADED: s8_service.slot_already_entered looks up the "
              f"ENTRY_GRID_CT label, so this record may not match and the slot could be "
              f"re-entered. Fix the caller to pass the grid slot.")
    greeks_complete = bool(short_leg.complete and long_leg.complete)

    entry = s8_schema.EntryInfo(
        entry_ts=entry_ts,
        entry_spot=_num(entry_spot),
        entry_vix=_num(entry_vix),
        entry_realized_vol=_num(entry_realized_vol),
        short_strike=_num(pick.short_strike),
        long_strike=_num(pick.long_strike),
        width=_num(pick.width),
        realized_credit=_num(pick.realized_credit),
        stop_multiple=_num(template_cfg.get("stop_multiple")),
        stop_price=_num(stop_price),
        short_leg=short_leg,
        long_leg=long_leg,
        greeks_complete=greeks_complete,
    )

    trade_id = s8_schema.make_trade_id(
        date, pick.template_name, slot_key, pick.short_strike, pick.long_strike
    )

    return s8_schema.TradeRecord(
        trade_id=trade_id,
        date=date,
        account=account,
        template=pick.template_name,
        slot=slot_key,
        side=pick.side,
        expiration=None,   # set by capture_and_persist_entry from the chain snapshot
        qty=qty,
        status="open",
        entry=entry,
        exit=None,
        provenance=s8_schema.Provenance(
            paperbot_version=paperbot_version, pilot_mode=bool(pilot_mode)
        ),
    )


# --------------------------------------------------------------------------- #
# LIVE: subscribe, bounded-wait for greeks, harvest
# --------------------------------------------------------------------------- #
# Generic tick list: 100 = Option Volume, 101 = Option Open Interest, 106 = Option Implied
# Volatility. Model greeks stream automatically for a qualified option subscription; the
# bounded wait below is what lets them populate before we harvest (the settle-delay fix).
_GREEKS_GENERIC_TICKS = "100,101,106"
_POLL_INTERVAL_SECS = 0.25


def _env_float(name: str, default: float) -> float:
    """Read a float from env, falling back to `default` on missing/garbage."""
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# Additional bounded wait for the option OPEN-INTEREST tick (generic tick 101) AFTER model
# greeks (tick 106) have populated. OI ticks in slightly later than greeks, so harvesting
# the instant greeks arrive left open_interest=None. For SPXW 0DTE the OI figure is the
# PRIOR session's (it updates once daily), so it should be available with a small wait.
# Configurable via env so the timeout can be tuned without a code edit.
_OI_EXTRA_WAIT_SECS = _env_float("S8_OI_WAIT_SECS", 3.0)


def wait_for_oi(ticker: Any, right: Any, timeout: float, *, sleep,
                monotonic=time.monotonic,
                poll_interval: float = _POLL_INTERVAL_SECS) -> Optional[float]:
    """Poll until the right-appropriate open interest is present on `ticker`, or `timeout`.

    BOUNDED — never hangs: polls every `poll_interval` (via the injected `sleep`) until
    oi_from_ticker(ticker, right) is non-None OR `timeout` seconds have elapsed on the
    injected `monotonic` clock, then returns whatever OI is present (possibly None). `sleep`
    and `monotonic` are injected so the pure wait/predicate logic is offline-testable with a
    fake clock and a ticker that never populates OI.
    """
    deadline = monotonic() + float(timeout)
    oi = oi_from_ticker(ticker, right)
    while oi is None and monotonic() < deadline:
        sleep(poll_interval)
        oi = oi_from_ticker(ticker, right)
    return oi


def grab_leg_live(ib, contract, right, strike, timeout: float = 8.0,
                  oi_timeout: Optional[float] = None) -> s8_schema.LegGrab:
    """Subscribe to one option leg, WAIT (bounded) for model greeks THEN open interest,
    harvest, cancel.

    Streams live market data (snapshot=False) for `contract`, polling with ib.sleep every
    _POLL_INTERVAL_SECS until ticker.modelGreeks populates OR `timeout` elapses. Then does a
    SHORT additional bounded wait (up to `oi_timeout` secs, default _OI_EXTRA_WAIT_SECS) for
    the right-appropriate open-interest tick, which arrives slightly after greeks. Finally
    reads the leg via leg_grab_from_ticker and always cancels the subscription in a finally.

    Neither wait ever hangs: if greeks never arrive the LegGrab is complete=False (flagged,
    not fabricated); if OI never arrives it is recorded as None (flagged, not fabricated).
    """
    ot = _OI_EXTRA_WAIT_SECS if oi_timeout is None else oi_timeout
    ticker = ib.reqMktData(contract, genericTickList=_GREEKS_GENERIC_TICKS,
                           snapshot=False, regulatorySnapshot=False)
    try:
        deadline = time.monotonic() + float(timeout)
        while getattr(ticker, "modelGreeks", None) is None and time.monotonic() < deadline:
            ib.sleep(_POLL_INTERVAL_SECS)
        # Short additional bounded wait for the OI tick (arrives after greeks). Bounded by
        # `ot`; never hangs even if OI never populates.
        if ot and ot > 0:
            wait_for_oi(ticker, right, ot, sleep=ib.sleep)
        return leg_grab_from_ticker(ticker, right, strike)
    finally:
        try:
            ib.cancelMktData(contract)
        except Exception:
            pass


def grab_vix_live(ib, timeout: float = 5.0) -> Optional[float]:
    """Best-effort live VIX level (last, else close, else marketPrice). None on any failure."""
    vix = Index("VIX", "CBOE")
    try:
        try:
            ib.qualifyContracts(vix)
        except Exception:
            pass
        ticker = ib.reqMktData(vix, "", False, False)
        try:
            deadline = time.monotonic() + float(timeout)
            while time.monotonic() < deadline:
                val = _num(getattr(ticker, "last", None))
                if val is None:
                    val = _num(getattr(ticker, "close", None))
                if val is not None:
                    return val
                ib.sleep(_POLL_INTERVAL_SECS)
            # last resort: whatever marketPrice()/close is available at timeout
            try:
                mp = _num(ticker.marketPrice())
            except Exception:
                mp = None
            return mp if mp is not None else _num(getattr(ticker, "close", None))
        finally:
            try:
                ib.cancelMktData(vix)
            except Exception:
                pass
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# LIVE: the top-level entry hook — BEST-EFFORT, never raises into the caller
# --------------------------------------------------------------------------- #
def _pilot_mode() -> bool:
    """Read s8_runner.PILOT_MODE honestly (lazy import avoids the circular import — the
    runner imports THIS module). Falls back to True (the safe assumption) on any import
    problem, so the provenance stamp never under-reports the zero-transmit wall."""
    try:
        import s8_runner
        return bool(s8_runner.PILOT_MODE)
    except Exception:
        return True


def capture_and_persist_entry(ib, pick, template_cfg, account, qty, chain_snap,
                              stop_price, slot: Optional[str] = None) -> Optional[str]:
    """Grab both legs live (quotes+greeks) + spot + VIX at entry, build an "open"
    TradeRecord, persist it via s8_store, and return its trade_id.

    ``slot`` is the ENTRY_GRID_CT schedule label of the slot being entered (threaded down
    from s8_runner.evaluate_and_capture_due_template, which has it as its own parameter). It
    becomes TradeRecord.slot / the trade_id so the persisted idempotency key matches the one
    s8_service.slot_already_entered queries. Omitting it degrades that key to the wall-clock
    minute and prints a loud warning (see build_entry_trade_record).

    BEST-EFFORT by contract: this NEVER raises into the caller — any failure is caught,
    logged, and returns None so a capture problem can never break the pilot cycle. Never
    transmits: it only reads market data and writes to the local off-Drive store.

    entry_realized_vol is populated best-effort from s8_vol.realized_vol_live (annualized
    close-to-close realized vol of SPX over ~21 trading days; see that module for the exact
    definition). It is CONTEXT data, not a strategy input — None on any failure, never faked.
    """
    try:
        right = _SIDE_TO_RIGHT.get(pick.side)
        if right is None:
            print(f"    ! s8_capture: unknown pick.side {pick.side!r}; skipping capture")
            return None

        exp = chain_snap.attrs.get("expiration")
        if not exp:
            print("    ! s8_capture: chain_snap.attrs['expiration'] missing; skipping capture")
            return None

        # Contracts built the same way s8_runner.build_entry_order_group builds them (same
        # SPXW trading class from s8_chain). Qualify so reqMktData has a resolved conId.
        short_contract = Option("SPX", exp, pick.short_strike, right, "SMART",
                                tradingClass=s8_chain._SPXW_TRADING_CLASS, currency="USD")
        long_contract = Option("SPX", exp, pick.long_strike, right, "SMART",
                               tradingClass=s8_chain._SPXW_TRADING_CLASS, currency="USD")
        try:
            ib.qualifyContracts(short_contract, long_contract)
        except Exception as exc:
            print(f"    ! s8_capture: qualifyContracts failed ({exc}); attempting grab anyway")

        entry_ts = datetime.now(tz=_CT_ZONE).isoformat(timespec="milliseconds")
        entry_spot = _num(chain_snap.attrs.get("spot"))
        entry_vix = grab_vix_live(ib)
        entry_realized_vol = s8_vol.realized_vol_live(ib)   # best-effort; None on failure

        short_leg = grab_leg_live(ib, short_contract, right, pick.short_strike)
        long_leg = grab_leg_live(ib, long_contract, right, pick.long_strike)

        rec = build_entry_trade_record(
            pick=pick,
            template_cfg=template_cfg,
            account=account,
            qty=qty,
            entry_ts=entry_ts,
            slot=slot,
            entry_spot=entry_spot,
            entry_vix=entry_vix,
            entry_realized_vol=entry_realized_vol,
            short_leg=short_leg,
            long_leg=long_leg,
            stop_price=stop_price,
            paperbot_version=getattr(version, "VERSION", None),
            pilot_mode=_pilot_mode(),
        )
        rec.expiration = exp

        s8_store.upsert_trade_record(rec)
        print(f"    s8_capture: persisted entry {rec.trade_id} "
              f"(greeks_complete={rec.entry.greeks_complete})")
        return rec.trade_id
    except Exception as exc:
        print(f"    ! s8_capture: entry capture FAILED ({type(exc).__name__}: {exc}); "
              f"pilot cycle continues, nothing persisted")
        return None
