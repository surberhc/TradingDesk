"""s8_collector.py — S8 live-pilot INTRADAY ATM-BAND MARKET COLLECTOR (Phase 3).

The periodic CONTEXT feed for the S8 zero-transmit live pilot (see
docs/S8_LIVE_PILOT_DATA_CAPTURE_PLAN.md, component 4). It streams a BOUNDED ATM band of
today's SPXW 0DTE strikes (both rights) plus the SPX underlying and VIX off the live-
trading Gateway (read-only), and at a configurable cadence harvests a full snapshot of the
band — quotes/sizes/volume/OI + model greeks/IV + spot + VIX — to the market-context
parquet via ``s8_store.write_market`` (rows shaped by ``s8_schema.MARKET_COLUMNS``).

WHAT THIS IS (AND IS NOT)
-------------------------
This is SAMPLED CONTEXT, not the position legs. The exit monitor (Phase 2b, ``s8_monitor``)
captures each open position's two legs FULL-TICK regardless of this collector's band. This
collector's job is the surrounding market picture at a periodic cadence — the ATM chain
shape, greeks, and underlying/VIX through the session — so entries and exits can later be
characterised against the market they fired into. It NEVER picks entries or exits (rule #1
stays clean) and NEVER transmits (zero order path anywhere — only reqMktData/cancelMktData/
reads; connects ``readonly=True``; PILOT_MODE upstream is the load-bearing wall).

RISK #1 — THE MARKET-DATA-LINE BUDGET (the whole reason the band is bounded)
---------------------------------------------------------------------------
IBKR caps concurrent market-data lines at ~100, SHARED account-wide. The exit monitor needs
2 lines per open position (its two legs), and the entry runner takes a transient chain
snapshot. So the collector's band is BOUNDED to a conservative line budget and DEGRADES
gracefully: each band strike costs 2 lines (a put + a call), plus 2 underlying lines
(SPX + VIX). The default is deliberately small and both the band size (``max_strikes``)
and the cadence (``cadence_secs``) are configurable.

  DEFAULT BUDGET
    max_strikes = 24 distinct strikes  ->  24 * 2 = 48 option lines
    + 2 underlying lines (SPX, VIX)     ->  50 lines total
    leaves ~50 lines of a ~100 cap free for the monitor's position legs (25 positions'
    worth) and the runner's transient snapshot. Conservative by design.

  HOW THE DEFAULT BAND MAPS TO THE TEMPLATES' STRIKE RANGE (honest, with the gap flagged)
    SPXW near-money strikes are spaced 5 points, so 24 strikes centred on ATM spans about
    +/-12 strikes = +/-60 points around spot. The templates' SHORT legs sit OTM at
    ~0.20-0.29 delta, which on a normal-vol 0DTE day is roughly 30-60 points from spot —
    inside the default band. But S8_SPEC.md 2.2 documents realized spread WIDTHS of 5-85
    points, so the widest templates' LONG legs can sit up to ~85 points BEYOND the short
    leg — i.e. beyond the default +/-60-point band. That is an accepted, flagged gap: the
    collector is context, and the monitor captures the exact position legs full-tick
    anyway. To also blanket the widest templates' long legs, widen ``max_strikes`` (e.g.
    to ~44 -> +/-110 points -> ~90 lines) ONLY when the monitor's line usage leaves room;
    the default favours monitor headroom over collector breadth (Risk #1). See
    ``s8_chain.DEFAULT_STRIKES_EACH_SIDE`` (60) for the far wider band the on-demand entry
    snapshot uses, which is fine because it is transient (subscribe -> harvest -> cancel).

  GRACEFUL DEGRADATION
    On an IBKR "max tickers"/line-limit error while subscribing, the band SHRINKS
    (``_LINE_LIMIT_SHRINK`` factor) and re-subscribes, logging each step — it never
    crashes. ``clamp_max_strikes`` also hard-caps the requested band against ``max_lines``
    up front.

STRUCTURE (pure seams separated from the live wiring, per the build instruction)
--------------------------------------------------------------------------------
  PURE, offline-testable (no IBKR, no network):
    * clamp_max_strikes(...)        bound a requested band to a market-data-line budget
    * compute_atm_band(...)         pick the centred ATM band of strikes from a chain grid
    * band_line_count(...)          lines a band of N strikes costs (N*2 + underlyings)
    * market_row_from_ticker(...)   one MARKET_COLUMNS row from a Ticker-like object
    * build_market_frame(...)       assemble the snapshot DataFrame from tickers + context

  LIVE, thin, IBKR-facing (verified by the live smoke, not the offline unit tests):
    * S8Collector.run(...)          connect read-only, subscribe the band, harvest on a
                                    cadence, reconnect+resubscribe on drop, shrink on a
                                    line-limit error, cancel + disconnect on exit.

RESTART-SAFE BY DESIGN
----------------------
Stateless context feed — there is NO durable critical state to recover. A crash loses at
most the in-flight cadence window; on restart it simply reconnects, rebuilds the band from
the live spot, and resumes harvesting. (Contrast the monitor, whose open-position state is
crash-critical.)
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any, Iterable, List, Optional, Sequence, Tuple

# Self-contained sys.path shims (same rationale as s8_runner/s8_capture/s8_monitor: the venv
# editable installs still point at the deleted pre-2026-07-16 My Drive path, so derive the
# repo's own package parents from __file__ — never an absolute string — and make this module
# importable and runnable without depending on the editable installs being regenerated).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _pkg_parent in ("paperbot", "connections", "strategies"):
    _p = os.path.join(_REPO_ROOT, _pkg_parent)
    if _p not in sys.path:
        sys.path.insert(0, _p)

import s8_capture          # noqa: E402  (leg_grab_from_ticker — quotes + greeks harvest, REUSED)
import s8_lock             # noqa: E402  (single-instance / orphan guard — shared pure seam)
import s8_schema           # noqa: E402  (MARKET_COLUMNS)
import s8_startup          # noqa: E402  (bounded startup connect-retry — shared pure seam)
import s8_store            # noqa: E402  (write_market)

_CT_ZONE = ZoneInfo("America/Chicago")

# Generic tick list for the option subscriptions: 100 = Option Volume, 101 = Option Open
# Interest, 106 = Option Implied Volatility. Model greeks stream automatically once the
# option subscription settles (same list s8_capture / s8_monitor use).
_GREEKS_GENERIC_TICKS = "100,101,106"

# --- Line-budget defaults (Risk #1) --------------------------------------------------- #
# Each band strike costs 2 market-data lines (a put + a call). These two underlyings
# (SPX + VIX) cost 1 line each on top of the band.
_LINES_PER_STRIKE = 2
_UNDERLYING_LINES = 2                 # SPX + VIX

DEFAULT_MAX_STRIKES = 24              # 24 * 2 + 2 = 50 lines (see module docstring)
DEFAULT_MAX_LINES = 100              # IBKR's shared ~100-line cap (hard ceiling for clamp)
DEFAULT_CADENCE_SECS = 12.0          # harvest a full band snapshot every ~10-15s
_MIN_MAX_STRIKES = 4                 # never shrink below a token ATM window
_LINE_LIMIT_SHRINK = 0.5            # on a line-limit error, halve the band and retry

# SPXW 0DTE cash-settles at 15:00 CT; the collector stops sampling at the close.
_MARKET_CLOSE_CT = (15, 0)

# --- Startup data-wait (Phase 4b boot robustness) -------------------------------------- #
# An all-day scheduled collector must NOT die on boot just because live SPX data isn't
# flowing yet (started pre-open, into a data gap, or restarted during one). Startup spot/
# chain resolution BOUNDED-retries: poll every STARTUP_DATA_POLL_SECS for a valid live SPX
# spot, up to STARTUP_DATA_WAIT_SECS total, before giving up. If the window elapses the
# collector exits CLEANLY (logged message + nonzero rc) so a Task Scheduler restart-on-
# failure can retry — never a raw uncaught traceback. Mid-session drops keep the existing
# reconnect path; this bounded wait is startup-only.
STARTUP_DATA_WAIT_SECS = 600.0   # ~10 min bounded startup window for live SPX data
STARTUP_DATA_POLL_SECS = 15.0    # re-check for a valid live SPX spot every ~15s

# Substrings that mark an IBKR "too many market-data lines" style error (matched
# case-insensitively). Used to trigger graceful band-shrink rather than a crash.
_LINE_LIMIT_MARKERS = ("max number of tickers", "max tickers", "market data lines",
                       "too many", "market depth", "line limit")


# =========================================================================== #
# PURE SEAMS — no IBKR, no network. Offline-testable.
# =========================================================================== #

def band_line_count(n_strikes: int, underlying_lines: int = _UNDERLYING_LINES) -> int:
    """Market-data lines a band of ``n_strikes`` distinct strikes costs.

    Each strike is streamed for BOTH rights (a put + a call) = 2 lines, plus the fixed
    underlying lines (SPX + VIX by default).
    """
    return max(0, int(n_strikes)) * _LINES_PER_STRIKE + int(underlying_lines)


def clamp_max_strikes(
    max_strikes: int,
    max_lines: int = DEFAULT_MAX_LINES,
    underlying_lines: int = _UNDERLYING_LINES,
) -> int:
    """Bound a requested band to a market-data-line budget (Risk #1).

    Returns the largest strike count <= ``max_strikes`` whose total line cost
    (``band_line_count``) fits within ``max_lines``. Never returns below
    ``_MIN_MAX_STRIKES`` (a token ATM window is always kept — the caller decides whether
    even that fits; the live path shrinks further on an actual IBKR error). Purely
    arithmetic so it is trivially unit-testable.
    """
    allowed_by_budget = (int(max_lines) - int(underlying_lines)) // _LINES_PER_STRIKE
    clamped = min(int(max_strikes), allowed_by_budget)
    return max(_MIN_MAX_STRIKES, clamped)


def compute_atm_band(
    spot: Any,
    strikes: Sequence[float],
    max_strikes: int = DEFAULT_MAX_STRIKES,
) -> List[float]:
    """Pick the ATM-centred band of at most ``max_strikes`` strikes from a chain grid.

    ``strikes`` is the available strike list (as ``reqSecDefOptParams`` returns it, in any
    order); ``spot`` is the live underlying. Finds the strike nearest spot (ATM) and returns
    up to ``max_strikes`` strikes centred on it, sorted ascending. Mirrors
    ``s8_chain.build_0dte_chain``'s ATM-index logic (index offset, not point offset), so it
    inherits the chain's real strike spacing rather than assuming one.

    Raises ``ValueError`` on an empty strike list or an unresolved (None/NaN) spot — an
    ATM band cannot be defined without both, and silently returning an empty or full-chain
    band would be exactly the kind of scope surprise Risk #1 is guarding against.
    """
    grid = sorted({float(s) for s in strikes if s is not None and s == s})  # drop None/NaN
    if not grid:
        raise ValueError("compute_atm_band: empty strike list")
    if spot is None or spot != spot:  # None or NaN
        raise ValueError("compute_atm_band: unresolved (None/NaN) spot")

    cap = max(1, int(max_strikes))
    atm = min(range(len(grid)), key=lambda i: abs(grid[i] - float(spot)))

    # Take cap strikes centred on the ATM index. half below, the rest (incl. ATM) at/above,
    # clamped to the available grid on both ends so a near-edge spot still yields cap
    # strikes where the grid allows.
    half = cap // 2
    lo = atm - half
    hi = lo + cap  # exclusive
    if lo < 0:
        lo, hi = 0, min(len(grid), cap)
    if hi > len(grid):
        hi = len(grid)
        lo = max(0, hi - cap)
    return grid[lo:hi]


def market_row_from_ticker(
    ticker: Any,
    right: Any,
    strike: Any,
    *,
    expiration: Optional[str],
    underlying_spot: Optional[float],
    vix: Optional[float],
    ts: str,
) -> dict:
    """Assemble one ``MARKET_COLUMNS`` row from a Ticker-like object + shared context.

    Quotes/sizes/volume/OI and delta/gamma/vega/theta/IV are harvested via the REUSED
    ``s8_capture.leg_grab_from_ticker`` (same NaN->None normalisation and modelGreeks
    handling as the entry/exit grabs — no reimplementation). ``expiration``, ``vix`` and
    ``ts`` are the snapshot-wide context; ``underlying_spot`` prefers the explicit snapshot
    spot (the SPX index mark) and falls back to the leg's own greeks ``undPrice`` when the
    snapshot spot is missing.
    """
    grab = s8_capture.leg_grab_from_ticker(ticker, right, strike)
    spot = underlying_spot if underlying_spot is not None else grab.underlying_spot
    row = {c: None for c in s8_schema.MARKET_COLUMNS}
    row["ts"] = ts
    row["expiration"] = expiration
    row["strike"] = grab.strike
    row["right"] = grab.right
    row["bid"] = grab.bid
    row["ask"] = grab.ask
    row["last"] = grab.last
    row["bid_size"] = grab.bid_size
    row["ask_size"] = grab.ask_size
    row["volume"] = grab.volume
    row["open_interest"] = grab.open_interest
    row["delta"] = grab.delta
    row["gamma"] = grab.gamma
    row["vega"] = grab.vega
    row["theta"] = grab.theta
    row["iv"] = grab.iv
    row["underlying_spot"] = spot
    row["vix"] = vix
    return row


def build_market_frame(
    specs: Iterable[Tuple[Any, Any, Any]],
    *,
    expiration: Optional[str],
    underlying_spot: Optional[float],
    vix: Optional[float],
    ts: Optional[str] = None,
):
    """Assemble the snapshot DataFrame (``MARKET_COLUMNS`` shape) from band tickers.

    ``specs`` is an iterable of ``(ticker, right, strike)`` — one per band contract (both
    rights). ``ts`` defaults to now in CT-ISO (millisecond precision), matching the rest of
    the store's timestamp convention. Pandas is imported lazily so the other pure seams stay
    dependency-light. Returns a DataFrame with EXACTLY ``MARKET_COLUMNS`` in order.
    """
    import pandas as pd

    if ts is None:
        ts = datetime.now(tz=_CT_ZONE).isoformat(timespec="milliseconds")
    rows = [
        market_row_from_ticker(tk, right, strike, expiration=expiration,
                               underlying_spot=underlying_spot, vix=vix, ts=ts)
        for (tk, right, strike) in specs
    ]
    return pd.DataFrame(rows, columns=s8_schema.MARKET_COLUMNS)


# =========================================================================== #
# STARTUP DATA-WAIT — a PURE, offline-testable seam (clock/sleep/resolver injected).
# =========================================================================== #

class StartupDataTimeout(RuntimeError):
    """Raised when the bounded startup wait elapses with still no valid live SPX spot.

    A CAUGHT, handled condition — run() turns it into a clean logged exit + nonzero rc so a
    scheduled restart-on-failure can retry, never a raw uncaught traceback. Distinct type so
    the startup path can catch exactly this and let genuine bugs propagate.
    """


def _spot_is_valid(spot: Any) -> bool:
    """A resolved SPX spot is usable iff it is a real, positive number (not None/NaN/<=0).

    Mirrors _resolve_chain's ``spot == spot and spot`` guard (None/NaN/0 all rejected), the
    condition that used to crash the process on boot.
    """
    try:
        return spot is not None and spot == spot and float(spot) > 0.0
    except (TypeError, ValueError):
        return False


def wait_for_live_spot(
    resolve_spot,
    *,
    timeout_secs: float = STARTUP_DATA_WAIT_SECS,
    poll_secs: float = STARTUP_DATA_POLL_SECS,
    clock=time.monotonic,
    sleep=time.sleep,
    log=print,
) -> float:
    """Poll ``resolve_spot`` until it yields a valid live SPX spot, or the window elapses.

    PURE seam — ``clock``, ``sleep`` and the ``resolve_spot`` callable are injected, so a fake
    clock makes this instant and fully offline-testable with no broker/network/real sleeps.

    ``resolve_spot()`` returns a candidate spot (a float, or None/NaN when data is not flowing
    yet); it MAY also raise (pre-open the gateway often has no SPX ticker at all) — a raise is
    treated as "data not ready yet" (logged, then retried), NOT a crash. Returns the first
    valid spot (``_spot_is_valid``). Raises ``StartupDataTimeout`` if ``timeout_secs`` elapses
    with still no valid spot — the caller turns that into a clean exit, never a traceback.
    """
    deadline = clock() + float(timeout_secs)
    attempt = 0
    while True:
        attempt += 1
        try:
            spot = resolve_spot()
        except Exception as exc:  # noqa: BLE001  — resolver not ready is expected pre-open
            log(f"s8_collector: waiting for live SPX data... (attempt {attempt}, "
                f"resolver not ready: {type(exc).__name__}: {exc})")
        else:
            if _spot_is_valid(spot):
                if attempt > 1:
                    log(f"s8_collector: live SPX data resolved (spot={float(spot):g}) "
                        f"after {attempt} attempt(s).")
                return float(spot)
            log(f"s8_collector: waiting for live SPX data... "
                f"(attempt {attempt}, spot={spot!r} not yet valid)")
        # Give up only once the bounded window is exhausted.
        if clock() >= deadline:
            raise StartupDataTimeout(
                f"no valid live SPX spot after {float(timeout_secs):g}s "
                f"({attempt} attempt(s)) — giving up startup for a scheduled restart.")
        sleep(float(poll_secs))


# =========================================================================== #
# LIVE WIRING — thin, IBKR-facing, NOT offline-unit-tested. Zero-transmit:
# only reqMktData/cancelMktData/qualify/reads; no order path anywhere.
# =========================================================================== #

def _is_line_limit_error(exc: BaseException) -> bool:
    """True if ``exc`` looks like an IBKR market-data-line/max-tickers limit error."""
    msg = str(exc).lower()
    return any(marker in msg for marker in _LINE_LIMIT_MARKERS)


class S8Collector:
    """Periodic ATM-band market-context collector for the S8 zero-transmit live pilot.

    The pure seams above do the band arithmetic and row assembly; this class is the thin
    live loop around them: connect read-only, resolve today's SPXW 0DTE chain + spot, build
    the bounded ATM band, subscribe (band both rights + SPX + VIX), and every
    ``cadence_secs`` harvest a full snapshot to the market parquet. Reconnects + resubscribes
    on a gateway drop; shrinks the band on a line-limit error. Zero-transmit throughout.
    """

    def __init__(self) -> None:
        # Live-wiring state (populated only by run(); nothing durable/crash-critical).
        self._ib = None
        self._band_specs: List[Tuple[Any, str, float]] = []  # (ticker, right, strike)
        self._band_contracts: List[Any] = []                 # qualified Option contracts
        self._spx_ticker = None
        self._spx_contract = None
        self._vix_ticker = None
        self._vix_contract = None
        self._expiration: Optional[str] = None
        self._max_strikes = DEFAULT_MAX_STRIKES

    # ------------------------------------------------------------------ #
    # Chain resolution (mirrors s8_chain's underlying + params steps)
    # ------------------------------------------------------------------ #
    def _resolve_chain(self):
        """Resolve (spot, today's expiration, full strike grid) off the live gateway.

        Reuses s8_chain.get_underlying (same SPX Index construction + spot fallback) and its
        _todays_expiration guard (raises if today's SPXW 0DTE expiration is not listed —
        never silently trades/collects the wrong day). Read-only: reqTickers /
        reqSecDefOptParams only.
        """
        import s8_chain

        c, spot = s8_chain.get_underlying(self._ib)
        if not (spot == spot and spot):  # None or NaN
            raise RuntimeError("s8_collector: SPX spot did not resolve (None/NaN)")
        params = self._ib.reqSecDefOptParams(c.symbol, "", c.secType, c.conId)
        spxw = [p for p in params if p.tradingClass == s8_chain._SPXW_TRADING_CLASS] or params
        exps = sorted({e for p in spxw for e in p.expirations})
        strikes = sorted({s for p in spxw for s in p.strikes})
        exp = s8_chain._todays_expiration(exps)
        return c, float(spot), exp, strikes

    # ------------------------------------------------------------------ #
    # Subscribe / cancel the band (line-budget aware, degrades gracefully)
    # ------------------------------------------------------------------ #
    def _subscribe_band(self, spot: float, strikes: Sequence[float], exp: str) -> None:
        """Build + subscribe the bounded ATM band (both rights) + SPX + VIX, read-only.

        On an IBKR line-limit/max-tickers error the band is shrunk (``_LINE_LIMIT_SHRINK``)
        and re-attempted down to ``_MIN_MAX_STRIKES``; any residual failure is logged and the
        collector proceeds with whatever subscribed (never crashes). Zero-transmit: qualify +
        reqMktData only.
        """
        from ib_async import Index
        import s8_chain

        self._cancel_all()  # clean slate before (re)subscribing

        # SPX + VIX underlyings first (cheap, needed for spot/vix context every harvest).
        try:
            self._spx_contract = Index("SPX", s8_chain._SPX_EXCHANGE, "USD")
            self._vix_contract = Index("VIX", "CBOE", "USD")
            self._ib.qualifyContracts(self._spx_contract, self._vix_contract)
            self._spx_ticker = self._ib.reqMktData(self._spx_contract, "", False, False)
            self._vix_ticker = self._ib.reqMktData(self._vix_contract, "", False, False)
        except Exception as exc:  # noqa: BLE001
            print(f"s8_collector: underlying (SPX/VIX) subscribe failed "
                  f"({type(exc).__name__}: {exc}); continuing with option band only")

        # Option band, shrinking on a line-limit error.
        want = self._max_strikes
        while want >= _MIN_MAX_STRIKES:
            band = compute_atm_band(spot, strikes, want)
            lines = band_line_count(len(band))
            print(f"s8_collector: subscribing ATM band of {len(band)} strikes "
                  f"[{band[0]:g}..{band[-1]:g}] around spot={spot:g} "
                  f"(~{lines} lines incl. SPX+VIX), exp={exp}")
            try:
                self._subscribe_options(band, exp)
                self._max_strikes = want  # remember the size that actually fit
                return
            except Exception as exc:  # noqa: BLE001
                if _is_line_limit_error(exc):
                    new_want = max(_MIN_MAX_STRIKES, int(want * _LINE_LIMIT_SHRINK))
                    print(f"s8_collector: LINE-LIMIT hit ({exc}); shrinking band "
                          f"{want} -> {new_want} strikes and retrying")
                    if new_want == want:
                        break
                    want = new_want
                    continue
                print(f"s8_collector: band subscribe failed "
                      f"({type(exc).__name__}: {exc}); proceeding with partial band")
                return
        print(f"s8_collector: could not fit even a {_MIN_MAX_STRIKES}-strike band; "
              f"proceeding with underlying-only context")

    def _subscribe_options(self, band: Sequence[float], exp: str) -> None:
        """Qualify + reqMktData every band strike for both rights. Populates _band_specs.

        Raised errors propagate to _subscribe_band's shrink loop (which classifies
        line-limit vs. other). Read-only: qualify + reqMktData only, no order path.
        """
        from ib_async import Option
        import s8_chain

        candidates = [
            Option("SPX", exp, k, r, "SMART",
                   tradingClass=s8_chain._SPXW_TRADING_CLASS, currency="USD")
            for k in band for r in ("P", "C")
        ]
        qualified = s8_chain._qualify(self._ib, candidates)
        specs: List[Tuple[Any, str, float]] = []
        contracts: List[Any] = []
        for o in qualified:
            tk = self._ib.reqMktData(o, _GREEKS_GENERIC_TICKS, False, False)
            specs.append((tk, o.right, float(o.strike)))
            contracts.append(o)
        self._band_specs = specs
        self._band_contracts = contracts
        self._expiration = exp

    def _cancel_all(self) -> None:
        """Cancel every live subscription (band + SPX + VIX) and free the lines. Tolerant."""
        for o in self._band_contracts:
            try:
                self._ib.cancelMktData(o)
            except Exception:  # noqa: BLE001
                pass
        for c in (self._spx_contract, self._vix_contract):
            if c is not None:
                try:
                    self._ib.cancelMktData(c)
                except Exception:  # noqa: BLE001
                    pass
        self._band_specs = []
        self._band_contracts = []
        self._spx_ticker = self._spx_contract = None
        self._vix_ticker = self._vix_contract = None

    # ------------------------------------------------------------------ #
    # Read the current underlying/VIX marks off the cached tickers
    # ------------------------------------------------------------------ #
    def _read_spot(self) -> Optional[float]:
        return _ticker_price(self._spx_ticker)

    def _read_vix(self) -> Optional[float]:
        return _ticker_price(self._vix_ticker)

    # ------------------------------------------------------------------ #
    # Harvest one snapshot of the whole band -> market parquet
    # ------------------------------------------------------------------ #
    def harvest_once(self) -> int:
        """Assemble one full-band snapshot and append it to the market parquet.

        Returns the number of rows written (0 if the band is empty or the write failed).
        Never raises into the loop — a bad harvest is logged and skipped so the collector
        keeps running.
        """
        if not self._band_specs:
            return 0
        try:
            spot = self._read_spot()
            vix = self._read_vix()
            df = build_market_frame(
                self._band_specs, expiration=self._expiration,
                underlying_spot=spot, vix=vix,
            )
            date = datetime.now(tz=_CT_ZONE).strftime("%Y%m%d")
            s8_store.write_market(df, date)
            print(f"s8_collector: harvested {len(df)} band rows "
                  f"(spot={spot if spot is not None else 'n/a'}, "
                  f"vix={vix if vix is not None else 'n/a'})")
            return len(df)
        except Exception as exc:  # noqa: BLE001
            print(f"s8_collector: harvest failed ({type(exc).__name__}: {exc}); skipped")
            return 0

    # ------------------------------------------------------------------ #
    # The live loop
    # ------------------------------------------------------------------ #
    def run(
        self,
        consumer: str = "s8_collector",
        cadence_secs: float = DEFAULT_CADENCE_SECS,
        max_strikes: int = DEFAULT_MAX_STRIKES,
        max_lines: int = DEFAULT_MAX_LINES,
        duration_secs: Optional[float] = None,
        settle_secs: float = 6.0,
    ) -> None:
        """Connect read-only to the live-trading Gateway (port 4003), stream a bounded ATM
        band + SPX + VIX, and harvest a market snapshot every ``cadence_secs`` until market
        close (15:00 CT) or ``duration_secs`` (the smoke).

        Zero-transmit: connects ``readonly=True`` and only ever subscribes to / reads market
        data — there is no order path here. Restart-safe: no durable state; on a gateway drop
        it reconnects and rebuilds the band from the live spot. Line-budget safe: the band is
        clamped against ``max_lines`` and shrinks on an IBKR line-limit error.
        """
        from connections import ibkr_live_trade  # lazy — keeps the pure seams IB-free

        self._max_strikes = clamp_max_strikes(max_strikes, max_lines)
        if self._max_strikes < max_strikes:
            print(f"s8_collector: clamped max_strikes {max_strikes} -> {self._max_strikes} "
                  f"to fit the {max_lines}-line budget")

        print(f"s8_collector.run: connecting read-only to the live-trading Gateway "
              f"(consumer={consumer!r}, port {ibkr_live_trade.LIVE_TRADE_PORT})...")
        # STARTUP connect is BOUNDED-RETRY (see s8_startup), and it sits UPSTREAM of the
        # startup DATA wait below: a MISSING GATEWAY is handled here (the collector is
        # launched by Task Scheduler shortly after the Gateway's own start, so IBC boot or a
        # pending 2FA can still be refusing API connections at trigger time), while MISSING
        # DATA on an established connection is handled by wait_for_live_spot. Windows
        # restart-on-failure is NOT a reliable net (it fires on unexpected termination, not
        # reliably on a non-zero exit code from cmd.exe), so the collector self-heals; if the
        # window elapses it exits CLEANLY with one legible line + nonzero rc, no traceback.
        try:
            ib = s8_startup.connect_with_retry(
                lambda: ibkr_live_trade.connect(consumer, launch=False, readonly=True),
                label="s8_collector", port=ibkr_live_trade.LIVE_TRADE_PORT,
            )
        except s8_startup.StartupConnectTimeout as exc:
            print(f"s8_collector.run: {exc} Exiting cleanly with rc=3 (no IB Gateway at "
                  f"startup); relaunch once the gateway is up.")
            raise SystemExit(3)
        self._ib = ib
        try:
            try:
                # Startup is bounded-retry: waits for live SPX data rather than crashing on
                # boot if it isn't flowing yet (pre-open / data gap / restart during one).
                self._build_and_subscribe(wait_for_data=True)
            except StartupDataTimeout as exc:
                print(f"s8_collector.run: {exc} Exiting cleanly with rc=2 so a scheduled "
                      f"restart-on-failure can retry (no live SPX data at startup).")
                raise SystemExit(2)
            if settle_secs:
                ib.sleep(settle_secs)  # let quotes + model greeks populate before harvest #1

            started = time.monotonic()
            last_harvest = 0.0
            while True:
                try:
                    ib.waitOnUpdate(timeout=1.0)
                except Exception:  # noqa: BLE001
                    pass

                now = time.monotonic()
                if duration_secs is not None and (now - started) >= duration_secs:
                    print(f"s8_collector.run: duration {duration_secs:g}s elapsed — stopping.")
                    break
                if duration_secs is None and self._after_close():
                    print("s8_collector.run: market close reached — stopping.")
                    break

                if not ib.isConnected():
                    print("s8_collector.run: gateway disconnected — reconnecting...")
                    try:
                        ib = ibkr_live_trade.connect(consumer, launch=False, readonly=True)
                        self._ib = ib
                        self._build_and_subscribe()
                        if settle_secs:
                            ib.sleep(settle_secs)
                        last_harvest = 0.0
                    except Exception as exc:  # noqa: BLE001
                        print(f"s8_collector.run: reconnect failed ({exc}); retrying...")
                        time.sleep(5)
                    continue

                if (now - last_harvest) >= cadence_secs:
                    self.harvest_once()
                    last_harvest = now
        finally:
            try:
                self._cancel_all()
            except Exception:  # noqa: BLE001
                pass
            try:
                ib.disconnect()
            except Exception:  # noqa: BLE001
                pass
            print("s8_collector.run: disconnected.")

    def _build_and_subscribe(self, wait_for_data: bool = False) -> None:
        """Resolve the chain and (re)subscribe the band.

        On STARTUP (``wait_for_data=True``) the SPX-spot resolution is first wrapped in a
        bounded retry (``wait_for_live_spot``): a boot before live data is flowing (pre-open,
        a data gap, or a restart during one) waits gracefully — logging "waiting for live SPX
        data..." — instead of dying on an uncaught 'SPX spot did not resolve'. If the bounded
        window elapses, ``StartupDataTimeout`` propagates to run(), which exits cleanly with a
        nonzero rc. On a MID-SESSION reconnect (``wait_for_data=False``) behaviour is unchanged:
        a hard chain-resolve failure (no spot / no today-expiration) propagates to run()'s
        reconnect loop as before; band subscribe itself always degrades gracefully.
        """
        if wait_for_data:
            import s8_chain

            def _resolve_spot():
                _c, spot = s8_chain.get_underlying(self._ib)
                return spot

            # Blocks until a valid live spot is seen, or raises StartupDataTimeout on giving up.
            wait_for_live_spot(_resolve_spot)
        _c, spot, exp, strikes = self._resolve_chain()
        self._subscribe_band(spot, strikes, exp)

    def _after_close(self) -> bool:
        now = datetime.now(tz=_CT_ZONE)
        return (now.hour, now.minute) >= _MARKET_CLOSE_CT


def _ticker_price(ticker: Any) -> Optional[float]:
    """Best-effort current level off a cached Index ticker: marketPrice, then last, then
    close. NaN/None -> None. Used for both SPX spot and VIX."""
    if ticker is None:
        return None
    try:
        mp = ticker.marketPrice()
    except Exception:  # noqa: BLE001
        mp = None
    val = s8_capture._num(mp)
    if val is None:
        val = s8_capture._num(getattr(ticker, "last", None))
    if val is None:
        val = s8_capture._num(getattr(ticker, "close", None))
    return val


def _main() -> None:
    """Entrypoint wrapper — keeps startup failures LEGIBLE.

    A clean bounded-window give-up (gateway never came up / data never flowed) already exits
    via SystemExit with one logged line and no traceback. Anything else gets a one-line
    headline FIRST so the scheduled-task log is readable at a glance; the traceback is still
    printed underneath because a genuine bug must stay diagnosable.
    """
    sys.stdout.reconfigure(line_buffering=True)
    # SINGLE-INSTANCE / ORPHAN GUARD (see s8_lock) — its OWN lock, separate from the
    # service's. Stop-ScheduledTask kills the .cmd wrapper but not this python child, so a
    # surviving orphan would still hold clientId 56 at the gateway and collide with
    # tomorrow's scheduled start. The collector is a stateless context feed, so terminating
    # a verified prior instance and taking over loses nothing but the in-flight cadence
    # window. Released in the finally below, which also covers the SystemExit paths.
    lock = s8_lock.SingleInstanceLock("s8_collector", "s8_collector.py")
    if not lock.acquire():
        print("s8_collector: could not take the single-instance lock — exiting with rc=4.")
        raise SystemExit(4)
    try:
        S8Collector().run()
    except (SystemExit, KeyboardInterrupt):
        raise
    except Exception as exc:  # noqa: BLE001
        import traceback
        print(f"s8_collector: FATAL startup/run failure "
              f"({type(exc).__name__}: {exc}) — exiting with rc=1.")
        traceback.print_exc()
        raise SystemExit(1)
    finally:
        lock.release()


if __name__ == "__main__":
    _main()
