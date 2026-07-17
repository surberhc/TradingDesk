"""s8_monitor.py — S8 shadow-monitor LIVE EXIT-MONITOR SERVICE (Phase 2b).

The crash-safe, zero-transmit, full-tick-capturing service that WRAPS the pure exit
core from Phase 2a (``s8_monitor_core``). The core decides *when/how* an exit fires from
synthetic price samples; this module is the machinery around it: it reads open positions
from the durable store, streams each open credit spread's two legs off the live-trading
Gateway (read-only), feeds every tick through the core, persists every tick, records the
exit (with full greeks) the instant the frozen stop/B2 rule fires, and closes out anything
still open at session end.

DESIGN PRIORITIES (in the order the plan fixes them)
----------------------------------------------------
 1. CRASH-SAFE + IDEMPOTENT. The durable trade record (JSONL, latest-wins) is the source
    of truth. ``load_open_positions`` reconciles on every start: a trade already
    ``status=="closed"`` in the records is dropped from open-state and never re-monitored,
    so a crash between "wrote the exit" and "removed from open-state" recovers cleanly and
    never double-closes. ``finalize_exit`` is idempotent — finalizing an already-closed
    trade_id is a no-op.
 2. ZERO-TRANSMIT. There is NO order path anywhere in this file: no ``order_router``, no
    ``ib.placeOrder``, no ``ib.qualifyContracts``-then-transmit. The live connection is
    ``ibkr_live_trade.connect(..., readonly=True)``. It only ever ``reqMktData`` /
    ``cancelMktData`` / reads. PILOT_MODE remains the load-bearing wall upstream; this
    service structurally cannot transmit.
 3. FULL-TICK CAPTURE. Every sample the service sees is written to the per-trade ticks
    parquet (quotes + greeks), batched for I/O sanity but never dropped.

OFFLINE-TESTABILITY (the crash-safe orchestration has no IB dependency)
-----------------------------------------------------------------------
``load_open_positions`` / ``on_sample`` / ``finalize_exit`` / ``close_all_eod`` touch only
the store and the pure core — they never require a broker. They are exercised end-to-end in
``test_s8_monitor.py`` with synthetic tick streams and a fake IB. The live wiring (``run``,
``_subscribe``, the pendingTickers handler) is the ONLY part that talks to IBKR; it is kept
deliberately thin and is verified by the live smoke harness, not the offline unit tests.

CANONICAL TIMESTAMP CONVENTION (reconciled with Phase 1)
--------------------------------------------------------
Phase 1 (``s8_capture.build_entry_trade_record``) stores ``EntryInfo.entry_ts`` and every
``LegGrab.grab_ts`` as CT **ISO-8601 strings** (``datetime.now(tz=CT).isoformat(...)``).
The pure core (``s8_monitor_core``) works exclusively in **epoch seconds** so it stays
clock-free. This service is the bridge:

  * ON LOAD: parse the stored ISO ``entry_ts`` -> epoch seconds for ``MonitorPosition``.
  * SAMPLES: ``Sample.ts`` is epoch seconds (``time.time()``) throughout the live loop.
  * TICKS PARQUET: the ``ts`` column is written as a CT ISO string (human-inspectable and
    consistent with ``LegGrab.grab_ts``), converted from the sample's epoch ts.
  * ON FINALIZE: the core returns ``exit_ts`` in epoch seconds; we convert it back to a CT
    ISO string for ``ExitInfo.exit_ts`` (and each exit leg's ``grab_ts``). ``duration_secs``
    is ``exit_epoch - entry_epoch`` — plain epoch arithmetic, tz-independent and correct
    regardless of the ISO offsets, which is exactly why the endpoints are reconciled to
    epoch before the subtraction.

CLIENTID NOTE
-------------
This service connects with its own dedicated ``"s8_monitor"`` (55) read-only consumer id,
registered in ``connections.clientids`` (Live-Trade lane, port 4003). It is distinct from
the entry runner's ``"s8_live_pilot"`` (54) precisely so the monitor and ``s8_runner`` can
run CONCURRENTLY on port 4003 without a clientId collision.
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional, Tuple

# Self-contained sys.path shims (same rationale as s8_runner/s8_capture: the venv editable
# installs still point at the deleted pre-2026-07-16 My Drive path, so derive the repo's own
# package parents from __file__ — never an absolute string — and make this module importable
# and runnable without depending on the editable installs being regenerated).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _pkg_parent in ("paperbot", "connections", "strategies"):
    _p = os.path.join(_REPO_ROOT, _pkg_parent)
    if _p not in sys.path:
        sys.path.insert(0, _p)

import s8_capture          # noqa: E402  (leg_grab_from_ticker — greeks harvest)
import s8_monitor_core as core  # noqa: E402  (the pure exit logic — REUSED, never reimplemented)
import s8_schema           # noqa: E402
import s8_store            # noqa: E402

_CT_ZONE = ZoneInfo("America/Chicago")

# SpreadPick/record side ("PUT"/"CALL") -> ib_async Option.right ("P"/"C"). Same mapping the
# runner and capture use; duplicated to keep this module free of a runner import.
_SIDE_TO_RIGHT = {"PUT": "P", "CALL": "C"}

# Generic tick list for the live leg subscriptions: 100 = Option Volume, 101 = Option Open
# Interest, 106 = Option Implied Volatility. Model greeks stream automatically once the
# option subscription settles (same list s8_capture uses for the entry grab).
_GREEKS_GENERIC_TICKS = "100,101,106"

# Batch size for the ticks parquet: flush a position's tick buffer once it holds this many
# ROWS (2 rows — short + long — per sample). Small enough that a crash loses at most a
# fraction of a second of ticks; large enough to avoid a parquet part per tick.
_TICK_FLUSH_ROWS = 50

# SPXW 0DTE cash-settles at 15:00 CT; anything still open at that wall-clock is closed EOD.
_MARKET_CLOSE_CT = (15, 0)


# --------------------------------------------------------------------------- #
# ts helpers — the ISO<->epoch bridge documented in the module docstring
# --------------------------------------------------------------------------- #

def iso_to_epoch(ts: Any) -> Optional[float]:
    """Parse a CT ISO-8601 string (as Phase 1 stores entry_ts/grab_ts) to epoch seconds.

    Returns None on anything unparseable (missing, non-string, malformed) rather than
    raising — a bad stored timestamp must not crash the loader. A tz-naive string is
    assumed to be CT (the convention Phase 1 writes in).
    """
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return float(ts)
    if not isinstance(ts, str):
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_CT_ZONE)
    return dt.timestamp()


def epoch_to_iso(ts: Any) -> Optional[str]:
    """Render epoch seconds as a CT ISO-8601 string (millisecond precision).

    The inverse of iso_to_epoch, used to write ExitInfo.exit_ts / leg grab_ts / the ticks
    parquet ``ts`` column back in the same CT-ISO form the rest of the store uses. Returns
    None if ts is missing/unparseable.
    """
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(float(ts), tz=_CT_ZONE).isoformat(timespec="milliseconds")
    except (ValueError, TypeError, OSError):
        return None


# --------------------------------------------------------------------------- #
# Record <-> MonitorPosition
# --------------------------------------------------------------------------- #

def position_from_record(rec: s8_schema.TradeRecord) -> core.MonitorPosition:
    """Build a pure-core MonitorPosition from a stored open TradeRecord.

    Strikes/credit/stop come from the record's EntryInfo (stop_price was frozen at entry by
    s8_strategy.stop_price and is used verbatim — never recomputed). entry_ts is converted
    from the stored CT-ISO string to epoch seconds so the core's epoch-only arithmetic is
    correct.
    """
    entry = rec.entry or s8_schema.EntryInfo()
    return core.MonitorPosition(
        trade_id=rec.trade_id,
        side=rec.side,
        short_strike=entry.short_strike,
        long_strike=entry.long_strike,
        qty=rec.qty if rec.qty is not None else 1,
        realized_credit=entry.realized_credit if entry.realized_credit is not None else 0.0,
        stop_price=entry.stop_price if entry.stop_price is not None else 0.0,
        entry_ts=iso_to_epoch(entry.entry_ts),
    )


def _state_info(rec: s8_schema.TradeRecord) -> Dict[str, Any]:
    """Compact open-state record for one position — enough to re-subscribe on reconnect
    without re-reading the full trade record. Kept tiny (crash-recovery state)."""
    entry = rec.entry or s8_schema.EntryInfo()
    return {
        "status": "open",
        "date": rec.date,
        "side": rec.side,
        "expiration": rec.expiration,
        "short_strike": entry.short_strike,
        "long_strike": entry.long_strike,
        "qty": rec.qty,
    }


# --------------------------------------------------------------------------- #
# The service
# --------------------------------------------------------------------------- #

class S8Monitor:
    """Persistent exit-monitor for the S8 zero-transmit live pilot.

    The four crash-safe methods (load_open_positions / on_sample / finalize_exit /
    close_all_eod) are pure orchestration over the store + core and need no broker — they
    are the offline-tested seams. The live wiring (run / _subscribe / the pendingTickers
    handler) is thin and IBKR-facing.
    """

    def __init__(self) -> None:
        # Per-trade monitor state. _positions holds the trades currently being monitored.
        self._positions: Dict[str, core.MonitorPosition] = {}
        self._states: Dict[str, core.MonitorState] = {}
        self._last_sample: Dict[str, core.Sample] = {}
        self._pos_date: Dict[str, str] = {}
        self._tick_buffer: Dict[str, List[dict]] = {}

        # Live-wiring state (populated only by run()/_subscribe(); untouched offline).
        self._ib = None
        self._tickers: Dict[str, Dict[str, Any]] = {}   # trade_id -> {"short":tk,"long":tk,...}
        self._ticker_owner: Dict[int, Tuple[str, str]] = {}  # id(ticker) -> (trade_id, leg)

    # ------------------------------------------------------------------ #
    # (1) LOAD + RECONCILE  — crash recovery, idempotent
    # ------------------------------------------------------------------ #
    def load_open_positions(self) -> List[str]:
        """Read open positions from the store and reconcile crash state. Returns the list
        of trade_ids now being monitored.

        Source of truth is the durable trade records (latest-wins). Every record with
        ``status=="open"`` is (re)loaded as a fresh MonitorPosition + MonitorState. Any
        trade_id lingering in open-state that the records now show ``status=="closed"`` (or
        that has no open record at all) is dropped — that is the idempotent crash-recovery
        step: if the service died after writing an exit but before pruning open-state, the
        closed trade is NOT resurrected or double-closed. The cleaned open-state (exactly
        the still-open set) is persisted atomically.
        """
        records = {r.trade_id: r for r in s8_store.read_trade_records()}
        prior_state = s8_store.read_open_state()

        open_ids = [tid for tid, r in records.items() if r.status == "open"]

        # Rebuild fresh monitor state for every open record (a fresh MonitorState per the
        # spec — the core recomputes life-of-position stats from the resumed tick stream;
        # the durable record already holds the entry, so nothing is lost on restart).
        self._positions = {}
        self._states = {}
        for tid in open_ids:
            rec = records[tid]
            self._positions[tid] = position_from_record(rec)
            self._states[tid] = core.MonitorState()
            self._pos_date[tid] = rec.date or self._derive_date(rec)
            self._tick_buffer.setdefault(tid, [])

        # Reconcile open-state: keep only still-open ids; report what was pruned.
        pruned = [tid for tid in prior_state if tid not in self._positions]
        if pruned:
            print(f"s8_monitor: reconcile dropped {len(pruned)} already-closed/stale "
                  f"trade_id(s) from open-state: {pruned}")
        clean_state = {tid: _state_info(records[tid]) for tid in open_ids}
        s8_store.write_open_state(clean_state)

        print(f"s8_monitor: monitoring {len(open_ids)} open position(s): {open_ids}")
        return open_ids

    @staticmethod
    def _derive_date(rec: s8_schema.TradeRecord) -> Optional[str]:
        ep = iso_to_epoch((rec.entry or s8_schema.EntryInfo()).entry_ts)
        iso = epoch_to_iso(ep)
        return iso[:10].replace("-", "") if iso else None

    # ------------------------------------------------------------------ #
    # (2) ON SAMPLE — full-tick capture + exit detection
    # ------------------------------------------------------------------ #
    def on_sample(
        self,
        trade_id: str,
        sample: core.Sample,
        short_leg: Optional[s8_schema.LegGrab] = None,
        long_leg: Optional[s8_schema.LegGrab] = None,
    ) -> None:
        """Fold one live sample into the monitor: persist the FULL tick, run the pure core,
        and finalize the exit if it just triggered.

        ``sample`` carries the price-only fields the core needs. ``short_leg`` / ``long_leg``
        are optional full LegGrabs (quotes + greeks) harvested from the live tickers — when
        present, the persisted tick rows carry greeks; when absent (offline tests), the tick
        rows are price-only from the sample. Either way the exit *decision* uses only the
        price-only Sample, exactly as the frozen core dictates.

        NEVER raises on a single bad sample: any per-sample failure is logged and swallowed
        so one malformed tick can never take the service down or lose the position.
        """
        try:
            position = self._positions.get(trade_id)
            if position is None:
                return  # not a monitored (open) position — ignore stray ticks
            state = self._states[trade_id]

            self._last_sample[trade_id] = sample
            self._buffer_tick(trade_id, position, sample, short_leg, long_leg)

            core.process_sample(position, state, sample)

            if state.triggered:
                self.finalize_exit(trade_id, short_leg=short_leg, long_leg=long_leg)
        except Exception as exc:  # noqa: BLE001 — a bad tick must never crash the service
            print(f"s8_monitor: on_sample({trade_id}) swallowed {type(exc).__name__}: {exc}")

    def _buffer_tick(
        self,
        trade_id: str,
        position: core.MonitorPosition,
        sample: core.Sample,
        short_leg: Optional[s8_schema.LegGrab],
        long_leg: Optional[s8_schema.LegGrab],
    ) -> None:
        """Append this sample's two leg rows (short + long) to the trade's tick buffer;
        flush the buffer to the ticks parquet once it reaches _TICK_FLUSH_ROWS."""
        ts_iso = epoch_to_iso(sample.ts)
        right = _SIDE_TO_RIGHT.get(position.side or "", position.side)
        buf = self._tick_buffer.setdefault(trade_id, [])
        buf.append(self._tick_row(trade_id, ts_iso, "short", right, position.short_strike,
                                  sample.short_bid, sample.short_ask, sample.short_last,
                                  short_leg, sample.spot))
        buf.append(self._tick_row(trade_id, ts_iso, "long", right, position.long_strike,
                                  sample.long_bid, sample.long_ask, sample.long_last,
                                  long_leg, sample.spot))
        if len(buf) >= _TICK_FLUSH_ROWS:
            self._flush_ticks(trade_id)

    @staticmethod
    def _tick_row(trade_id, ts_iso, leg, right, strike, bid, ask, last,
                  grab: Optional[s8_schema.LegGrab], spot) -> dict:
        """One TICK_COLUMNS-shaped row. Prices come from the Sample; greeks/sizes/volume/OI
        come from the optional full LegGrab when present (else None — price-only tick)."""
        row = {c: None for c in s8_schema.TICK_COLUMNS}
        row["trade_id"] = trade_id
        row["ts"] = ts_iso
        row["leg"] = leg
        row["right"] = right
        row["strike"] = strike
        row["bid"] = bid
        row["ask"] = ask
        row["last"] = last
        row["underlying_spot"] = spot
        if grab is not None:
            row["bid_size"] = grab.bid_size
            row["ask_size"] = grab.ask_size
            row["volume"] = grab.volume
            row["open_interest"] = grab.open_interest
            row["delta"] = grab.delta
            row["gamma"] = grab.gamma
            row["vega"] = grab.vega
            row["theta"] = grab.theta
            row["iv"] = grab.iv
            if grab.underlying_spot is not None:
                row["underlying_spot"] = grab.underlying_spot
        return row

    def _flush_ticks(self, trade_id: str) -> None:
        """Write the trade's buffered tick rows to the date-partitioned ticks parquet and
        clear the buffer. No-op on an empty buffer. Never raises into the caller."""
        buf = self._tick_buffer.get(trade_id)
        if not buf:
            return
        try:
            import pandas as pd
            df = pd.DataFrame(buf, columns=s8_schema.TICK_COLUMNS)
            date = self._pos_date.get(trade_id) or datetime.now(tz=_CT_ZONE).strftime("%Y%m%d")
            s8_store.write_ticks(df, date)
            self._tick_buffer[trade_id] = []
        except Exception as exc:  # noqa: BLE001
            print(f"s8_monitor: tick flush for {trade_id} failed "
                  f"({type(exc).__name__}: {exc}); buffer retained for retry")

    def flush_all_ticks(self) -> None:
        """Flush every position's tick buffer (called periodically and on shutdown)."""
        for tid in list(self._tick_buffer.keys()):
            self._flush_ticks(tid)

    # ------------------------------------------------------------------ #
    # (3) FINALIZE EXIT — idempotent, greeks-enriched, atomic state prune
    # ------------------------------------------------------------------ #
    def finalize_exit(
        self,
        trade_id: str,
        short_ticker: Any = None,
        long_ticker: Any = None,
        short_leg: Optional[s8_schema.LegGrab] = None,
        long_leg: Optional[s8_schema.LegGrab] = None,
    ) -> bool:
        """Record the exit for one position and remove it from the live set. Returns True if
        it finalized here, False if it was a no-op (already closed — the idempotency guard).

        Steps:
          * IDEMPOTENT GUARD: if trade_id is not currently monitored, OR its stored record is
            already ``status=="closed"``, do nothing (a second call never writes a second
            exit or corrupts the record).
          * Build the exit via the pure core (``build_exit_info``) — price-only legs, epoch ts.
          * Attach FULL greeks to the exit legs from the live tickers (via
            ``s8_capture.leg_grab_from_ticker``): the core supplies the exit *prices*, the live
            tickers supply delta/gamma/vega/theta/iv/sizes/OI. Callers may pass the raw
            ib tickers (``short_ticker``/``long_ticker``) or pre-built LegGrabs
            (``short_leg``/``long_leg``); either is honored, ticker takes precedence.
          * Convert epoch ts -> CT ISO for ExitInfo.exit_ts and each leg's grab_ts.
          * Merge into the TradeRecord (status="closed", exit filled) and upsert (append-only
            latest-wins — the entry-only line is superseded, never mutated).
          * Prune from open-state atomically, flush remaining ticks, cancel subscriptions.
        """
        position = self._positions.get(trade_id)
        if position is None:
            return False  # already finalized/removed — idempotent no-op

        base = self._read_record(trade_id)
        if base is not None and base.status == "closed":
            # Record already closed (e.g. finalized in a prior life). Just drop the live
            # handle so we stop monitoring it; do not write a second exit.
            self._drop_position(trade_id)
            return False

        state = self._states[trade_id]

        # Pure core: exit info with price-only legs and epoch exit_ts.
        info = core.build_exit_info(position, state)

        # Attach full greeks from the live tickers (price stays the core's; greeks overlaid).
        sg = self._leg_grab(short_ticker, short_leg, position.side, position.short_strike)
        lg = self._leg_grab(long_ticker, long_leg, position.side, position.long_strike)
        self._overlay_greeks(info.get("short_leg_exit"), sg)
        self._overlay_greeks(info.get("long_leg_exit"), lg)

        # epoch -> CT ISO for the stored ExitInfo (and each exit leg's grab_ts).
        info["exit_ts"] = epoch_to_iso(info.get("exit_ts"))
        for leg_key in ("short_leg_exit", "long_leg_exit"):
            leg = info.get(leg_key)
            if leg is not None:
                leg["grab_ts"] = epoch_to_iso(leg.get("grab_ts"))

        exit_info = s8_schema.ExitInfo.from_dict(info)

        rec = base if base is not None else self._record_stub(trade_id, position)
        rec.status = "closed"
        rec.exit = exit_info
        s8_store.upsert_trade_record(rec)

        reason = exit_info.exit_reason
        pnl = exit_info.pnl
        print(f"s8_monitor: finalized {trade_id} reason={reason} "
              f"pnl={pnl if pnl is not None else 'n/a'} "
              f"mae={exit_info.max_adverse_excursion}")

        # Flush any remaining ticks, then prune from open-state + live handles atomically.
        self._flush_ticks(trade_id)
        self._drop_position(trade_id)
        self._prune_open_state(trade_id)
        self._cancel_subscription(trade_id)
        return True

    def _read_record(self, trade_id: str) -> Optional[s8_schema.TradeRecord]:
        for r in s8_store.read_trade_records():
            if r.trade_id == trade_id:
                return r
        return None

    @staticmethod
    def _record_stub(trade_id: str, position: core.MonitorPosition) -> s8_schema.TradeRecord:
        """Minimal record if none is on disk (defensive — the entry capture normally wrote
        one). Carries the strikes/credit/stop we do know so the exit isn't orphaned."""
        return s8_schema.TradeRecord(
            trade_id=trade_id,
            side=position.side,
            qty=position.qty,
            status="open",
            entry=s8_schema.EntryInfo(
                short_strike=position.short_strike,
                long_strike=position.long_strike,
                realized_credit=position.realized_credit,
                stop_price=position.stop_price,
                entry_ts=epoch_to_iso(position.entry_ts),
            ),
        )

    @staticmethod
    def _leg_grab(ticker, leg: Optional[s8_schema.LegGrab], side, strike
                  ) -> Optional[s8_schema.LegGrab]:
        """Prefer a live ticker (harvest greeks via s8_capture.leg_grab_from_ticker); else
        fall back to a pre-built LegGrab; else None."""
        if ticker is not None:
            right = _SIDE_TO_RIGHT.get(side or "", side)
            try:
                return s8_capture.leg_grab_from_ticker(ticker, right, strike)
            except Exception:  # noqa: BLE001 — greeks are best-effort at exit
                return leg
        return leg

    @staticmethod
    def _overlay_greeks(exit_leg: Optional[dict], grab: Optional[s8_schema.LegGrab]) -> None:
        """Overlay greeks/sizes/volume/OI from a live LegGrab onto the core's price-only exit
        leg dict, IN PLACE. The core's exit *prices* (bid/ask/last at the exit sample) are
        authoritative and preserved; only the fields the core leaves None are filled."""
        if exit_leg is None or grab is None:
            return
        for f in ("bid_size", "ask_size", "volume", "open_interest",
                  "delta", "gamma", "vega", "theta", "iv"):
            val = getattr(grab, f, None)
            if val is not None:
                exit_leg[f] = val
        if exit_leg.get("underlying_spot") is None and grab.underlying_spot is not None:
            exit_leg["underlying_spot"] = grab.underlying_spot
        # complete iff greeks actually populated on the live grab at exit.
        exit_leg["complete"] = bool(getattr(grab, "complete", False))

    def _drop_position(self, trade_id: str) -> None:
        self._positions.pop(trade_id, None)
        self._states.pop(trade_id, None)
        self._last_sample.pop(trade_id, None)

    def _prune_open_state(self, trade_id: str) -> None:
        """Atomically remove one trade_id from the persisted open-state."""
        state = s8_store.read_open_state()
        if trade_id in state:
            state.pop(trade_id, None)
            s8_store.write_open_state(state)

    # ------------------------------------------------------------------ #
    # (4) CLOSE ALL AT SESSION END
    # ------------------------------------------------------------------ #
    def close_all_eod(self, reason: str = "eod") -> List[str]:
        """Close out every still-open position at session end and finalize each. Returns the
        list of trade_ids closed here.

        Each position is valued at its last-seen sample (its final marks); a position that
        never got a priceable sample closes with the reason recorded and None marks (honest,
        not fabricated). Idempotent: already-triggered positions keep their intraday stop
        exit (close_at_session_end is a no-op on them).
        """
        closed: List[str] = []
        for trade_id in list(self._positions.keys()):
            position = self._positions[trade_id]
            state = self._states[trade_id]
            final = self._last_sample.get(trade_id)
            core.close_at_session_end(position, state, final, reason=reason)
            tk = self._tickers.get(trade_id, {})
            if self.finalize_exit(trade_id,
                                  short_ticker=tk.get("short"), long_ticker=tk.get("long")):
                closed.append(trade_id)
        print(f"s8_monitor: close_all_eod({reason}) closed {len(closed)} position(s): {closed}")
        return closed

    # ================================================================== #
    # LIVE WIRING — thin, IBKR-facing, NOT offline-unit-tested. Zero-transmit:
    # only reqMktData/cancelMktData/reads; no order path anywhere.
    # ================================================================== #
    def run(
        self,
        consumer: str = "s8_monitor",
        duration_secs: Optional[float] = None,
        rescan_secs: float = 30.0,
        flush_secs: float = 5.0,
    ) -> None:
        """Connect read-only to the live-trading Gateway (port 4003), monitor every open
        position tick-by-tick until market close (or ``duration_secs`` for the smoke), then
        close out anything still open and exit.

        Zero-transmit: connects ``readonly=True`` and only ever subscribes to / reads market
        data. NEVER places, modifies, or transmits an order — there is no order path here.
        Persists open-state on every open/close and survives gateway drops by reconnecting
        and re-subscribing the still-open positions.
        """
        from connections import ibkr_live_trade  # lazy — keeps the offline seams IB-free

        print(f"s8_monitor.run: connecting read-only to the live-trading Gateway "
              f"(consumer={consumer!r}, port {ibkr_live_trade.LIVE_TRADE_PORT})...")
        ib = ibkr_live_trade.connect(consumer, launch=False, readonly=True)
        self._ib = ib
        try:
            ib.pendingTickersEvent += self._on_pending_tickers
            self.load_open_positions()
            for trade_id in list(self._positions.keys()):
                self._subscribe(trade_id)

            started = time.monotonic()
            last_rescan = started
            last_flush = started
            while True:
                # bounded wait for the next tick batch; the handler does the real work.
                try:
                    ib.waitOnUpdate(timeout=1.0)
                except Exception:  # noqa: BLE001
                    pass

                now = time.monotonic()

                # Duration-boxed run (smoke) or market-close (production).
                if duration_secs is not None and (now - started) >= duration_secs:
                    print(f"s8_monitor.run: duration {duration_secs:g}s elapsed — closing out.")
                    break
                if duration_secs is None and self._after_close():
                    print("s8_monitor.run: market close reached — closing out.")
                    break

                # Reconnect + re-subscribe on a gateway drop.
                if not ib.isConnected():
                    print("s8_monitor.run: gateway disconnected — reconnecting...")
                    try:
                        ib = ibkr_live_trade.connect(consumer, launch=False, readonly=True)
                        self._ib = ib
                        ib.pendingTickersEvent += self._on_pending_tickers
                        self._tickers.clear()
                        self._ticker_owner.clear()
                        for trade_id in list(self._positions.keys()):
                            self._subscribe(trade_id)
                    except Exception as exc:  # noqa: BLE001
                        print(f"s8_monitor.run: reconnect failed ({exc}); retrying...")
                        time.sleep(5)
                    continue

                if (now - last_rescan) >= rescan_secs:
                    self._rescan_and_subscribe()
                    last_rescan = now
                if (now - last_flush) >= flush_secs:
                    self.flush_all_ticks()
                    last_flush = now

            # Session end: close out everything still open, persist, flush, disconnect.
            self.close_all_eod(reason="eod")
            self.flush_all_ticks()
        finally:
            try:
                for trade_id in list(self._tickers.keys()):
                    self._cancel_subscription(trade_id)
            except Exception:  # noqa: BLE001
                pass
            try:
                ib.pendingTickersEvent -= self._on_pending_tickers
            except Exception:  # noqa: BLE001
                pass
            try:
                ib.disconnect()
            except Exception:  # noqa: BLE001
                pass
            print("s8_monitor.run: disconnected.")

    def _after_close(self) -> bool:
        now = datetime.now(tz=_CT_ZONE)
        return (now.hour, now.minute) >= _MARKET_CLOSE_CT

    def _subscribe(self, trade_id: str) -> None:
        """Subscribe to a position's two legs (streaming market data + generic greek ticks).
        Read-only: reqMktData only, never an order. Tolerant — a subscribe failure logs and
        leaves the position monitored (a later rescan can retry)."""
        if self._ib is None or trade_id in self._tickers:
            return
        from ib_async import Option
        import s8_chain

        state = s8_store.read_open_state().get(trade_id, {})
        exp = state.get("expiration")
        side = state.get("side")
        right = _SIDE_TO_RIGHT.get(side or "", side)
        short_strike = state.get("short_strike")
        long_strike = state.get("long_strike")
        if not (exp and right and short_strike is not None and long_strike is not None):
            print(f"s8_monitor: cannot subscribe {trade_id} — incomplete state {state}")
            return
        try:
            short_c = Option("SPX", exp, short_strike, right, "SMART",
                             tradingClass=s8_chain._SPXW_TRADING_CLASS, currency="USD")
            long_c = Option("SPX", exp, long_strike, right, "SMART",
                            tradingClass=s8_chain._SPXW_TRADING_CLASS, currency="USD")
            self._ib.qualifyContracts(short_c, long_c)
            short_tk = self._ib.reqMktData(short_c, _GREEKS_GENERIC_TICKS, False, False)
            long_tk = self._ib.reqMktData(long_c, _GREEKS_GENERIC_TICKS, False, False)
            self._tickers[trade_id] = {
                "short": short_tk, "long": long_tk,
                "short_contract": short_c, "long_contract": long_c,
            }
            self._ticker_owner[id(short_tk)] = (trade_id, "short")
            self._ticker_owner[id(long_tk)] = (trade_id, "long")
            print(f"s8_monitor: subscribed {trade_id} legs {short_strike}/{long_strike}{right}")
        except Exception as exc:  # noqa: BLE001
            print(f"s8_monitor: subscribe {trade_id} failed ({type(exc).__name__}: {exc})")

    def _cancel_subscription(self, trade_id: str) -> None:
        """Cancel a position's live subscriptions and free its lines (line-budget recycling,
        plan Risk #1). No-op offline / when never subscribed."""
        tk = self._tickers.pop(trade_id, None)
        if tk is None or self._ib is None:
            return
        for leg in ("short", "long"):
            ticker = tk.get(leg)
            contract = tk.get(f"{leg}_contract")
            if ticker is not None:
                self._ticker_owner.pop(id(ticker), None)
            if contract is not None:
                try:
                    self._ib.cancelMktData(contract)
                except Exception:  # noqa: BLE001
                    pass

    def _rescan_and_subscribe(self) -> None:
        """Pick up positions opened since the last scan (the entry capture wrote new open
        records) and subscribe them. Persists the refreshed open-state."""
        records = {r.trade_id: r for r in s8_store.read_trade_records()}
        new_ids = [tid for tid, r in records.items()
                   if r.status == "open" and tid not in self._positions]
        if not new_ids:
            return
        state = s8_store.read_open_state()
        for tid in new_ids:
            rec = records[tid]
            self._positions[tid] = position_from_record(rec)
            self._states[tid] = core.MonitorState()
            self._pos_date[tid] = rec.date or self._derive_date(rec)
            self._tick_buffer.setdefault(tid, [])
            state[tid] = _state_info(rec)
        s8_store.write_open_state(state)
        for tid in new_ids:
            self._subscribe(tid)
        print(f"s8_monitor: rescan picked up {len(new_ids)} new position(s): {new_ids}")

    def _on_pending_tickers(self, tickers) -> None:
        """ib.pendingTickersEvent handler — TRUE per-tick capture. For each updated ticker,
        map it back to its position and drive on_sample with a fresh Sample (built from the
        position's two current cached tickers) plus full LegGrabs for greeks."""
        seen: set = set()
        for tk in tickers:
            owner = self._ticker_owner.get(id(tk))
            if owner is None:
                continue
            trade_id = owner[0]
            if trade_id in seen or trade_id not in self._positions:
                continue
            seen.add(trade_id)
            sample, short_leg, long_leg = self._build_sample(trade_id)
            if sample is not None:
                self.on_sample(trade_id, sample, short_leg=short_leg, long_leg=long_leg)

    def _build_sample(self, trade_id: str):
        """Build a price-only core.Sample + two full LegGrabs from a position's cached
        tickers. ts is epoch seconds (time.time())."""
        tk = self._tickers.get(trade_id)
        if tk is None:
            return None, None, None
        position = self._positions.get(trade_id)
        side = position.side if position else None
        short_tk = tk.get("short")
        long_tk = tk.get("long")
        right = _SIDE_TO_RIGHT.get(side or "", side)
        short_leg = s8_capture.leg_grab_from_ticker(
            short_tk, right, position.short_strike if position else None) if short_tk else None
        long_leg = s8_capture.leg_grab_from_ticker(
            long_tk, right, position.long_strike if position else None) if long_tk else None
        und = None
        if short_leg is not None and short_leg.underlying_spot is not None:
            und = short_leg.underlying_spot
        elif long_leg is not None and long_leg.underlying_spot is not None:
            und = long_leg.underlying_spot
        sample = core.Sample(
            ts=time.time(),
            short_bid=short_leg.bid if short_leg else None,
            short_ask=short_leg.ask if short_leg else None,
            short_last=short_leg.last if short_leg else None,
            long_bid=long_leg.bid if long_leg else None,
            long_ask=long_leg.ask if long_leg else None,
            long_last=long_leg.last if long_leg else None,
            spot=und,
        )
        return sample, short_leg, long_leg


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    S8Monitor().run()
