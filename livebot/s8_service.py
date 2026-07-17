"""s8_service.py — S8 live-pilot UNIFIED ALL-DAY SERVICE (Phase 4a).

ONE persistent, read-only process that does BOTH sides of the S8 zero-transmit live
pilot on ONE connection:

  * ENTRY  — at each grid slot's due-time it evaluates the due (template, slot) via the
             SHARED entry code path (``s8_runner.evaluate_and_capture_due_template``:
             frozen pick -> margin preflight -> build the entry/stop/B2 group -> log
             "WOULD HAVE TRANSMITTED" -> rich open-TradeRecord capture). It NEVER places
             or transmits an order.
  * EXIT   — every tick it drives the FROZEN exit logic for every open position via the
             composed ``s8_monitor.S8Monitor`` (its ``load_open_positions`` / ``on_sample``
             / ``finalize_exit`` / ``close_all_eod`` are reused VERBATIM — never
             reimplemented here).

Because it is ONE stateful process holding every open position in the durable store,
crash-safe entry idempotency falls out naturally: a mid-day restart reloads the open
positions from the store and, before re-entering any slot, checks the store for an
existing trade at that (date, template, slot). A slot already entered — open OR closed —
is skipped, so a restart never double-enters. This SUBSUMES the old standalone runner's
separate idempotency concern (plan component 3).

ZERO-TRANSMIT (absolute)
------------------------
PILOT_MODE stays True upstream (``s8_runner.PILOT_MODE``); this service connects
``ibkr_live_trade.connect(..., readonly=True)`` and has NO order path anywhere — no
``order_router.place``, no ``ib.placeOrder``, no ``bracketOrder``. The entry side only
BUILDS the order group and LOGS the would-have line (via the shared runner function); the
exit side only ``reqMktData`` / ``cancelMktData`` / reads. The unit tests assert this.

CLIENTID
--------
This unified service connects with the ``"s8_monitor"`` (55) read-only consumer id
(``connections.clientids``, Live-Trade lane, port 4003): it IS the all-day monitor+entry
process, so it takes the monitor's registered id. Notes on the two neighbouring ids:
  * ``"s8_live_pilot"`` (54) remains reserved for the standalone ``livebot/s8_runner.py``
    script, which is kept for manual runs / tests and connects on its own id so it can be
    driven independently of this service without a clientId collision.
  * ``"s8_collector"`` (56) is the SEPARATE intraday market-context collector process
    (its own connection, own id) — not part of this service.
Running this service and the standalone runner at the same moment would be redundant (both
would try to enter the same slots), but they cannot COLLIDE at the gateway because they
hold distinct clientIds.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime
from typing import List, Optional
from zoneinfo import ZoneInfo

# Self-contained sys.path shims — same rationale as s8_runner/s8_monitor/s8_capture: the
# venv editable installs still point at the deleted pre-2026-07-16 My Drive path, so derive
# the repo's own package parents from __file__ (never an absolute string) and make this
# module importable/runnable without depending on those installs being regenerated.
import os

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _pkg_parent in ("paperbot", "connections", "strategies"):
    _p = os.path.join(_REPO_ROOT, _pkg_parent)
    if _p not in sys.path:
        sys.path.insert(0, _p)

import s8_chain            # noqa: E402
import s8_config           # noqa: E402
import s8_monitor          # noqa: E402  (S8Monitor — exit side reused VERBATIM)
import s8_runner           # noqa: E402  (evaluate_and_capture_due_template — entry side, shared)
import s8_store            # noqa: E402

_CT_ZONE = ZoneInfo("America/Chicago")

# This service connects with the monitor's registered read-only consumer id (see the
# module docstring's CLIENTID section).
_CONSUMER = "s8_monitor"


def current_ct_date() -> str:
    """Today's date in US/Central as YYYYMMDD — the ``date`` half of a trade's identity.

    Module-level (not a method) so tests can monkeypatch it to a fixed trading date without
    reaching into an instance. DST-correct via zoneinfo, independent of the host tz.
    """
    return datetime.now(tz=_CT_ZONE).strftime("%Y%m%d")


class S8Service:
    """The unified all-day entry+exit service.

    Composes an ``S8Monitor`` (exit side) and shares a single read-only connection with it.
    The offline-testable seams are ``entry_cycle`` (entry decision + idempotency + capture +
    subscribe) and the monitor's own crash-safe methods; ``run`` is the thin live loop.
    """

    def __init__(self, account: Optional[str] = None) -> None:
        self.account = account if account is not None else s8_config.ACCOUNT
        # The composed exit monitor. Its four crash-safe methods (load_open_positions /
        # on_sample / finalize_exit / close_all_eod) and its live subscribe/tick wiring are
        # reused verbatim — this service never reimplements the stop/B2 logic.
        self.monitor = s8_monitor.S8Monitor()
        self._ib = None

    # ------------------------------------------------------------------ #
    # Connection binding — keep the service and the composed monitor on ONE ib
    # ------------------------------------------------------------------ #
    def _bind_ib(self, ib) -> None:
        """Point both this service and the composed monitor at the same connection."""
        self._ib = ib
        self.monitor._ib = ib

    # ------------------------------------------------------------------ #
    # ENTRY SIDE — idempotency + shared decision + subscribe
    # ------------------------------------------------------------------ #
    def slot_already_entered(self, date: str, template: str, slot: str) -> bool:
        """True if the durable store already holds a trade for this (date, template, slot),
        OPEN OR CLOSED. This is the store-backed idempotency guard: a slot that was already
        entered (this life or a prior, pre-crash one) is never entered again."""
        for r in s8_store.read_trade_records():
            if r.date == date and r.template == template and r.slot == slot:
                return True
        return False

    def _read_summary(self, ib):
        """Read + filter the account summary for the margin preflight. Returns the filtered
        summary, or None (skip entry this cycle) on read failure / target-account-absent —
        an entry-side problem must NEVER take the exit monitoring down."""
        try:
            summary_all = ib.accountSummary()
        except Exception as exc:  # noqa: BLE001
            print(f"s8_service: accountSummary() failed ({type(exc).__name__}: {exc}); "
                  f"skipping entry this cycle")
            return None
        summary = s8_runner.filter_account_summary(summary_all, self.account)
        if not summary:
            seen = sorted(str(a) for a in {getattr(r, "account", None) for r in summary_all}
                          if a is not None)
            print(f"s8_service: target account {self.account!r} not found under login "
                  f"(seen: {seen}); skipping entry this cycle")
            return None
        return summary

    def entry_cycle(self, ib, now=None) -> List[dict]:
        """Evaluate any due (template, slot) right now and ENTER the ones not already in the
        store. Returns the list of per-template outcome dicts (empty if nothing was due /
        nothing pending). NEVER raises — entry problems must not disturb exit monitoring.

        Order of operations (idempotency FIRST, then gateway contact):
          1. reuse ``s8_runner.due_templates`` + ``current_ct_time`` for what's due;
          2. drop any (template, slot) already in the store for today (idempotency) —
             BEFORE any summary/chain read, so a restart mid-slot makes zero extra gateway
             contact for an already-entered slot;
          3. for the remainder: read+filter the account summary, snapshot the live 0DTE
             chain ONCE, and run the SHARED entry function per due slot;
          4. on a real entry (a persisted open TradeRecord), pick it up + subscribe its legs
             via the monitor's own rescan path.
        """
        if self.account == "TBD":
            # Fail-closed: with no real test account set, enter nothing (exit monitoring of
            # any pre-existing positions still continues). Mirrors the runner's TBD refusal.
            return []

        now = now if now is not None else s8_runner.current_ct_time()
        due = s8_runner.due_templates(now)
        if not due:
            return []

        date = current_ct_date()
        pending = [(t, s) for (t, s) in due if not self.slot_already_entered(date, t, s)]
        if not pending:
            # Every due slot is already in the store — the idempotent no-op restart path.
            return []

        # Keep the composed monitor bound to this connection so its subscribe path uses it.
        self.monitor._ib = ib

        summary = self._read_summary(ib)
        if summary is None:
            return []

        try:
            chain_snap = s8_chain.snapshot_0dte_chain(ib)
        except Exception as exc:  # noqa: BLE001
            print(f"s8_service: chain snapshot failed ({type(exc).__name__}: {exc}); "
                  f"skipping entry this cycle")
            return []

        outcomes: List[dict] = []
        entered_any = False
        for template, slot in pending:
            try:
                outcome = s8_runner.evaluate_and_capture_due_template(
                    ib, chain_snap, summary, template, slot, self.account)
            except Exception as exc:  # noqa: BLE001 — one bad template must not sink the rest
                outcome = {"template": template, "slot": slot,
                           "error": f"evaluate raised: {type(exc).__name__}: {exc}"}
                print(f"s8_service: entry evaluate {template}@{slot} raised "
                      f"({type(exc).__name__}: {exc}); continuing")
            outcomes.append(outcome)
            if outcome.get("trade_id"):
                entered_any = True

        # A real entry persisted a new open TradeRecord; pick it up + subscribe its legs via
        # the monitor's own rescan (reused verbatim — writes open-state, subscribes).
        if entered_any:
            self.monitor._rescan_and_subscribe()
        return outcomes

    # ------------------------------------------------------------------ #
    # RESUME / EOD — thin passthroughs to the composed monitor (crash recovery + close)
    # ------------------------------------------------------------------ #
    def resume(self, ib=None) -> List[str]:
        """Crash-recovery: reload open positions from the durable store and subscribe each.
        Returns the trade_ids now being monitored. Reuses the monitor's load_open_positions
        (reconcile-on-load, idempotent) verbatim."""
        if ib is not None:
            self._bind_ib(ib)
        ids = self.monitor.load_open_positions()
        for trade_id in list(self.monitor._positions.keys()):
            self.monitor._subscribe(trade_id)
        return ids

    def close_eod(self, reason: str = "eod") -> List[str]:
        """Close out every still-open position at session end. Passthrough to the monitor's
        close_all_eod (reused verbatim)."""
        return self.monitor.close_all_eod(reason=reason)

    # ================================================================== #
    # LIVE LOOP — thin, IBKR-facing, NOT offline-unit-tested. Zero-transmit:
    # only reqMktData/cancelMktData/reads (exit) + build-and-log (entry); no order path.
    # ================================================================== #
    def run(
        self,
        duration_secs: Optional[float] = None,
        entry_check_secs: float = 15.0,
        rescan_secs: float = 30.0,
        flush_secs: float = 5.0,
    ) -> None:
        """Connect read-only to the live-trading Gateway (port 4003) and run the unified
        entry+exit loop until market close (or ``duration_secs`` for a smoke), then close
        out anything still open and exit cleanly.

        Each iteration: pump the tick event (drives exit monitoring via the monitor's
        pendingTickers handler), then on cadence run the entry check, the position rescan,
        and the tick flush. Reconnects + re-subscribes on a gateway drop. Zero-transmit:
        connects ``readonly=True`` and never places/modifies/transmits an order.
        """
        from connections import ibkr_live_trade  # lazy — keeps the offline seams IB-free

        print(f"s8_service.run: connecting read-only to the live-trading Gateway "
              f"(consumer={_CONSUMER!r}, port {ibkr_live_trade.LIVE_TRADE_PORT}, "
              f"account={self.account!r})...")
        ib = ibkr_live_trade.connect(_CONSUMER, launch=False, readonly=True)
        self._bind_ib(ib)
        try:
            ib.pendingTickersEvent += self.monitor._on_pending_tickers
            # Crash recovery: resume every open position from the durable store + subscribe.
            self.resume()

            started = time.monotonic()
            last_entry = last_rescan = last_flush = started
            while True:
                # Bounded wait for the next tick batch; the pendingTickers handler drives
                # exit monitoring (on_sample) for every open position.
                try:
                    ib.waitOnUpdate(timeout=1.0)
                except Exception:  # noqa: BLE001
                    pass

                now = time.monotonic()

                # Duration-boxed run (smoke) or market close (production) -> stop.
                if duration_secs is not None and (now - started) >= duration_secs:
                    print(f"s8_service.run: duration {duration_secs:g}s elapsed — closing out.")
                    break
                if duration_secs is None and self.monitor._after_close():
                    print("s8_service.run: market close reached — closing out.")
                    break

                # Reconnect + re-subscribe on a gateway drop.
                if not ib.isConnected():
                    print("s8_service.run: gateway disconnected — reconnecting...")
                    try:
                        ib = ibkr_live_trade.connect(_CONSUMER, launch=False, readonly=True)
                        self._bind_ib(ib)
                        ib.pendingTickersEvent += self.monitor._on_pending_tickers
                        self.monitor._tickers.clear()
                        self.monitor._ticker_owner.clear()
                        for trade_id in list(self.monitor._positions.keys()):
                            self.monitor._subscribe(trade_id)
                    except Exception as exc:  # noqa: BLE001
                        print(f"s8_service.run: reconnect failed ({exc}); retrying...")
                        time.sleep(5)
                    continue

                # ENTRY check on cadence (idempotent; zero gateway contact if nothing due).
                if (now - last_entry) >= entry_check_secs:
                    try:
                        self.entry_cycle(ib)
                    except Exception as exc:  # noqa: BLE001 — entry never sinks the service
                        print(f"s8_service.run: entry_cycle raised "
                              f"({type(exc).__name__}: {exc}); exit monitoring continues")
                    last_entry = now

                # Pick up any positions opened since last scan + subscribe them.
                if (now - last_rescan) >= rescan_secs:
                    self.monitor._rescan_and_subscribe()
                    last_rescan = now
                if (now - last_flush) >= flush_secs:
                    self.monitor.flush_all_ticks()
                    last_flush = now

            # Session end: close out everything still open, persist, flush.
            self.close_eod(reason="eod")
            self.monitor.flush_all_ticks()
        finally:
            try:
                for trade_id in list(self.monitor._tickers.keys()):
                    self.monitor._cancel_subscription(trade_id)
            except Exception:  # noqa: BLE001
                pass
            try:
                ib.pendingTickersEvent -= self.monitor._on_pending_tickers
            except Exception:  # noqa: BLE001
                pass
            try:
                ib.disconnect()
            except Exception:  # noqa: BLE001
                pass
            print("s8_service.run: disconnected.")


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    S8Service().run()
