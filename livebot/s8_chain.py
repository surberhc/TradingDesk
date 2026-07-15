"""
s8_chain.py — on-demand live SPXW 0DTE option-chain snapshot for S8 (British IC + B2
long-leg auto-close). Stage 4 of the 5-stage S8 build (see docs/S8_SPEC.md and the
approved build plan at the top-level plans folder, calm-riding-hammock.md item 2).

>>> UNTESTED LIVE AS OF THIS BUILD <<<
`s8_runner.py` calls this module's functions against a connection to the live-TRADING
Gateway (`connections/ibkr_live_trade.py`, port 4003) — a real, funded, transmit-capable
account (NOT the paper Gateway at 4002, and NOT the earlier port-4001 live-DATA login).
This module itself is agnostic to which Gateway that connection came from: it takes
`ib: IB` as a parameter, per its original design, and never connects on its own, so it
generalizes across whichever handle the runner passes. It remains untested live only
because the live-trading Gateway has not yet had its first S8 dry run. This file is
therefore a careful, code-level ADAPTATION of
`datacollector/ibkr_forward.py`'s proven, nightly-production `_underlying()` /
`build_chain()` / `snapshot_chain()` — reviewed for correctness by inspection (contract
construction pattern, batching pattern, tick reads all mirrored from that proven code
where they generalize) — NOT reinvented from scratch. Its first real verification is a
live dry run once Andrew brings the live-trading Gateway up (this is the approved plan's
own stated Verification step for `s8_runner.py`, not a gap being introduced here).

WHAT'S REUSED VS. ADAPTED (stated plainly, per the build instructions):
  * get_underlying()      -- same `Index("SPX", "CBOE", "USD")` construction and
                              qualify+reqTickers-for-spot pattern as
                              ibkr_forward._underlying(), specialized to SPX/SPXW only
                              (S8 never trades any other root, so the Stock/other-index
                              branches of _underlying() are dropped, not silently carried
                              along unused).
  * snapshot_0dte_chain() -- same reqSecDefOptParams -> Option(...) -> qualifyContracts()
                              contract-construction pattern as build_chain(), and the same
                              batched reqMktData -> sleep(settle) -> harvest ->
                              cancelMktData pattern as snapshot_chain() -- both adapted
                              near verbatim. One deliberate simplification: this module
                              only needs bid/ask (see s8_strategy.pick_spread_by_credit's
                              required shape), not greeks/OI/volume, so it requests the
                              default (empty) generic tick list instead of
                              snapshot_chain()'s "100,101" (volume+OI) -- a narrower ask,
                              not a departure from the proven pattern's correctness.

OPEN QUESTIONS FLAGGED FOR THE LIVE DRY RUN (per the build instructions: stated plainly,
not silently worked around):

  1. EXPIRATION SELECTION IS EOD-SPECIFIC IN build_chain() AND DOES NOT CLEANLY GENERALIZE.
     build_chain()'s `max_exps` truncation takes the NEAREST N sorted expirations off
     reqSecDefOptParams's full list (`exps[:max_exps]`) -- correct for the nightly job,
     which wants "the next few expirations after today" or (max_exps=None) the full
     universe. For 0DTE, S8 needs TODAY'S OWN expiration specifically, not merely "the
     nearest one." These are USUALLY the same thing for SPXW (which lists a same-day
     expiry on every trading day per docs/S8_SPEC.md), but "usually" is not a guarantee
     this code should silently assume -- if the SPXW 0DTE listing is ever delayed,
     missing, or the day is a half-day/holiday-adjacent edge case, `exps[:1]` would
     silently hand back the WRONG (next-day) expiration with no error. This module
     therefore does not reuse `max_exps` truncation at all: it filters for today's exact
     YYYYMMDD string via `_todays_expiration()` and RAISES if that string is absent, so a
     mismatch surfaces loudly at the runner rather than trading the wrong expiration.
     Confirming SPXW reliably lists a live same-day expiration at every time S8's entry
     grid can fire (as early as 08:35 CT, i.e. shortly after the 09:30 ET cash open) is
     an open question for the live dry run, not verified by this build.

  2. STRIKE-BAND FALLBACK ON UNRESOLVED SPOT IS AN EOD-SAFE BEHAVIOR THAT WOULD BE
     DANGEROUS INTRADAY. build_chain()'s band restriction is
     `if band and spot == spot and spot:` -- if spot comes back None/NaN (ibkr_forward.py's
     own `_to_df()` comment notes reqTickers on an Index can return NaN for
     marketPrice/close/last), the guard silently no-ops and returns the FULL
     unrestricted strike list. That is an acceptable (even intentional, see `--full`)
     fallback for a nightly full-universe sweep. It would NOT be acceptable here: an
     on-demand pre-entry snapshot silently widening to "every SPXW strike" would blow
     past LINE_LIMIT by an order of magnitude and take far longer than "right before an
     entry decision" allows. This module treats an unresolved spot as a HARD FAILURE
     (raises) instead of silently widening scope. Confirming spot resolves reliably
     intraday (it should -- SPX real-time index quotes are normally live during market
     hours, unlike the EOD-timing edge case ibkr_forward.py's comment is guarding
     against) is an open question for the live dry run.

  3. LINE_LIMIT / SETTLE_SECS ARE REUSED UNCHANGED FROM ibkr_forward.py (90 lines, 6s
     settle) -- tuned for a nightly full-universe sweep's pacing needs, not validated for
     a single small near-money slice fired repeatedly during market hours. With the
     default `strikes_each_side` below (60 strikes each side x 2 rights = up to ~244
     contracts => up to ~3 LINE_LIMIT batches => roughly 3 x SETTLE_SECS ~= 18s total),
     this should comfortably fit inside "right before an entry decision," but that
     estimate has not been measured live. Whether 6s is still the right settle time (long
     enough for a real quote, short enough not to stale-out a fast-moving decision) when
     called several times an hour rather than once a night is a live-dry-run question.

  4. MARKET DATA TYPE IS INTENTIONALLY NOT SET HERE. ibkr_forward.py's `main()`
     explicitly requests delayed data (`reqMarketDataType(3)`) because an EOD snapshot
     doesn't need live entitlement. S8 fires intraday against live decision-quality
     quotes, so this module does NOT call `reqMarketDataType()` at all -- it inherits
     whatever market data type the caller's `ib` connection is already configured for.
     The runner (`s8_runner.py`) connects to the live-TRADING Gateway, whose funded
     account serves real-time data directly, so no delayed-data fallback is needed; if a
     connection without real-time entitlement were ever passed instead, the bid/ask
     returned here could silently be a stale delayed quote. Not this module's job to
     paper over silently -- stated loudly here instead.

  5. SPX/SPXW EXCHANGE QUIRK CHECK (per the build instructions: confirm or flag).
     ibkr_forward.py's `_INDEX_EXCH` comment states its CBOE/RUSSELL/NASDAQ mapping was
     "verified live 2026-06-26 via reqSecDefOptParams: RUT resolves on RUSSELL not CBOE;
     NDX on NASDAQ" -- i.e. SOME index roots need a listing exchange other than the
     obvious one. SPX and SPXW are NOT called out with any exception anywhere in that
     comment or in `_INDEX_EXCH` (both map to plain "CBOE", matching the literal
     `Index("SPX", "CBOE", "USD")` construction reused here) -- so by that
     already-verified mapping, SPX/SPXW has NO equivalent quirk. This module hardcodes
     "CBOE" (not a table lookup) precisely because S8 only ever trades this one root; if
     IBKR's real listing exchange for SPX/SPXW ever turns out to differ from CBOE the way
     RUT/NDX do, that would surface immediately and loudly as a qualifyContracts()
     failure (empty/unqualified underlying) at the live dry run -- flagged as something
     to watch for, not assumed impossible.

  6. `strikes_each_side` DEFAULT IS A DATA-SLICE PARAMETER, NOT A STRATEGY KNOB, BUT
     ITS SIZING IS A JUDGMENT CALL WORTH A SECOND LOOK LIVE. It counts STRIKES (list
     index offset), not POINTS, mirroring build_chain()'s own `band` semantics. The
     default (60) is sized generously to comfortably cover docs/S8_SPEC.md Sec 2.2's
     observed width range (5-85 points) plus the ~0.20-0.29-delta OTM offset of the short
     leg, assuming SPXW's typical near-the-money 5-point strike spacing holds on the day
     in question -- it is NOT fit to any specific historical day. Whether 60 is
     comfortably enough (or unnecessarily wide, costing extra reqMktData lines/time) on a
     real high-vol day is a live-dry-run question, not resolved here.

This module NEVER connects, disconnects, or owns the IBKR connection lifecycle -- `ib` is
an already-connected `IB()` instance passed in by the caller, exactly mirroring
`order_router.py`'s `place(ib, ...)` / `what_if(ib, ...)` convention (see the approved
plan: "connection lifecycle owned by the caller, a later stage, s8_runner.py"). This file
does not import `connections.ibkr_paper`, does not call `connect()`/`disconnect()`, and takes no
clientId of its own.
"""
from __future__ import annotations

from datetime import date, datetime

import pandas as pd
from ib_async import IB, Index, Option

# ---------------------------------------------------------------------------
# Reused verbatim from datacollector/ibkr_forward.py (same values, same rationale:
# stay under IBKR's ~100 simultaneous market-data-line cap; give greeks/quotes time to
# populate before harvesting a batch). See open question #3 above re: whether these
# EOD-tuned constants also suit the intraday on-demand cadence.
# ---------------------------------------------------------------------------
LINE_LIMIT = 90
SETTLE_SECS = 6
QUALIFY_CHUNK = 100

# Default strike-band half-width, in STRIKES (not points) each side of ATM — see open
# question #6 above for the sizing rationale.
DEFAULT_STRIKES_EACH_SIDE = 60

# S8 only ever trades SPXW 0DTE — the exchange is hardcoded, not looked up, per open
# question #5 above (SPX/SPXW has no known RUT/NDX-style exchange quirk).
_SPX_EXCHANGE = "CBOE"
_SPXW_TRADING_CLASS = "SPXW"


def _num(x):
    """IBKR returns NaN for missing numerics; normalise NaN/None -> None.

    Duplicated (not imported) from ibkr_forward.py's identical helper: paperbot has no
    existing precedent for importing from datacollector (unlike s8_strategy.py's
    precedent for reaching into backtester/, which strategy_target.py already
    established), and datacollector's modules assume their own directory is on
    sys.path (bare `import config` / `import storage`) — reusing across that boundary
    would be more fragile than duplicating this two-line, dependency-free helper.
    """
    if x is None:
        return None
    try:
        return None if x != x else float(x)
    except (TypeError, ValueError):
        return None


def get_underlying(ib: IB) -> tuple[Index, float]:
    """Qualify and return (SPX Index contract, live spot).

    Reuses ibkr_forward._underlying()'s exact SPX construction
    (`Index("SPX", "CBOE", "USD")`) and its qualify -> reqTickers -> marketPrice/close/
    last spot-fallback chain verbatim — specialized here to SPX/SPXW only (see module
    docstring for why the general multi-root dispatch in _underlying() is dropped, and
    open question #5 for the exchange-quirk check).

    Returns a (contract, spot) PAIR rather than a bare Contract: the plan names this
    function `get_underlying() -> Contract`, but the live spot is needed immediately
    afterward to band the strike search (see build_0dte_chain()), and reqTickers is a
    single cheap round-trip exactly like _underlying() already bundles it — splitting
    the spot fetch into a second public function would just force the caller to redo
    that same round-trip. This is a deliberate, narrow widening of the plan's literal
    signature in favor of its stated intent ("reusing _underlying()'s exact
    construction"); flagged here explicitly rather than silently diverging.

    spot may legitimately be None/NaN (see open question #2) — this function does NOT
    raise on that; the caller (build_0dte_chain) is the one that treats an unresolved
    spot as a hard failure, since a bare underlying lookup by itself is still valid.
    """
    c = Index("SPX", _SPX_EXCHANGE, "USD")
    ib.qualifyContracts(c)
    [t] = ib.reqTickers(c)
    spot = t.marketPrice() or t.close or t.last
    return c, spot


def _todays_expiration(exps: list[str]) -> str:
    """Pick TODAY's own YYYYMMDD expiration string, or raise (see open question #1).

    Deliberately does NOT fall back to `exps[0]` ("nearest") the way
    ibkr_forward.build_chain()'s `max_exps` truncation does — for 0DTE, "nearest" and
    "today" must be the identical string, and silently accepting a near-but-wrong
    expiration would mean building a chain for the WRONG DAY with no error raised.
    """
    today = date.today().strftime("%Y%m%d")
    if today not in exps:
        raise RuntimeError(
            f"s8_chain: no SPXW 0DTE expiration listed for today ({today}). "
            f"reqSecDefOptParams returned {len(exps)} expirations; nearest="
            f"{exps[:3]!r}. Refusing to silently fall back to the nearest listed "
            "expiration (that could silently trade the wrong day) -- see s8_chain.py's "
            "module docstring, open question #1."
        )
    return today


def _qualify(ib: IB, candidates: list[Option]) -> list[Option]:
    """Qualify in pacing-friendly chunks; keep only contracts IBKR resolved.

    Reused verbatim from ibkr_forward._qualify() (same chunking, same "keep only
    resolved" filter).
    """
    out: list[Option] = []
    for i in range(0, len(candidates), QUALIFY_CHUNK):
        chunk = candidates[i:i + QUALIFY_CHUNK]
        out.extend(o for o in (ib.qualifyContracts(*chunk) or []) if o and o.conId)
    return out


def build_0dte_chain(
    ib: IB, strikes_each_side: int = DEFAULT_STRIKES_EACH_SIDE
) -> tuple[Index, float, str, list[Option]]:
    """Construct today's near-money SPXW 0DTE option chain.

    Adapts ibkr_forward.build_chain()'s reqSecDefOptParams -> filter-by-tradingClass ->
    ATM-band -> Option(...) -> qualifyContracts() pipeline, narrowed to exactly what S8
    needs: SPXW's tradingClass only, TODAY's expiration only (see _todays_expiration(),
    open question #1), and a strike band around the live spot (see open question #6 for
    sizing, open question #2 for why an unresolved spot is a hard failure here rather
    than build_chain()'s silent full-chain fallback).

    Returns (underlying contract, spot, expiration YYYYMMDD string, qualified Option
    contracts for both rights within the band).
    """
    c, spot = get_underlying(ib)
    if not (spot == spot and spot):  # None or NaN
        raise RuntimeError(
            "s8_chain: underlying SPX spot did not resolve (None/NaN) from reqTickers. "
            "Refusing to fall back to the full unrestricted SPXW strike chain (that "
            "would blow past LINE_LIMIT and take far longer than an on-demand pre-entry "
            "snapshot should) -- see s8_chain.py's module docstring, open question #2."
        )

    params = ib.reqSecDefOptParams(c.symbol, "", c.secType, c.conId)
    spxw_params = [p for p in params if p.tradingClass == _SPXW_TRADING_CLASS] or params
    exps = sorted({e for p in spxw_params for e in p.expirations})
    strikes = sorted({s for p in spxw_params for s in p.strikes})
    exp = _todays_expiration(exps)

    atm = min(range(len(strikes)), key=lambda i: abs(strikes[i] - spot))
    band_strikes = strikes[max(0, atm - strikes_each_side): atm + strikes_each_side + 1]

    candidates = [
        Option(c.symbol, exp, k, r, "SMART", tradingClass=_SPXW_TRADING_CLASS, currency="USD")
        for k in band_strikes for r in ("C", "P")
    ]
    contracts = _qualify(ib, candidates)
    return c, spot, exp, contracts


def snapshot_0dte_chain(
    ib: IB, strikes_each_side: int = DEFAULT_STRIKES_EACH_SIDE
) -> pd.DataFrame:
    """Take one live bid/ask snapshot of today's near-money SPXW 0DTE chain.

    Adapts ibkr_forward.snapshot_chain()'s batched reqMktData -> sleep(settle) ->
    harvest -> cancelMktData pattern, narrowed to bid/ask only (no greeks/OI/volume —
    S8's live spread selection, `s8_strategy.pick_spread_by_credit`, needs only strike/
    right/bid/ask; see that function's docstring for the exact expected shape). Uses the
    default (empty) generic tick list rather than snapshot_chain()'s "100,101"
    (volume+OI) since those fields are not requested here.

    Returns a DataFrame with EXACTLY the columns `pick_spread_by_credit` expects:
    strike (float), right ("CALL"/"PUT"), bid (float or None), ask (float or None) — one
    row per qualified contract in the band, both rights. `spot`, `expiration`, and
    `snapshot_time` are attached via `DataFrame.attrs` (not extra columns, so the column
    shape stays exactly what the strategy layer's docstring documents) for callers that
    want to pass `spot=`/`expiration=` straight through to `pick_spread_by_credit`'s
    optional diagnostic-delta kwargs without a second IBKR round-trip.

    Raises if the chain comes back empty (0 qualified contracts) rather than returning
    an empty DataFrame silently — an empty result right before a scheduled entry is
    never a valid "no trade today" signal, it is either a bug or a real market-data
    problem that must not be swallowed.
    """
    c, spot, exp, contracts = build_0dte_chain(ib, strikes_each_side)
    if not contracts:
        raise RuntimeError(
            f"s8_chain: 0 qualified SPXW {exp} contracts near spot={spot} with "
            f"strikes_each_side={strikes_each_side} -- refusing to return an empty "
            "snapshot silently (see snapshot_0dte_chain's docstring)."
        )

    rows: list[dict] = []
    for i in range(0, len(contracts), LINE_LIMIT):
        batch = contracts[i:i + LINE_LIMIT]
        tickers = [ib.reqMktData(o, "", False, False) for o in batch]
        ib.sleep(SETTLE_SECS)
        for o, t in zip(batch, tickers):
            rows.append({
                "strike": float(o.strike),
                "right": "CALL" if o.right == "C" else "PUT",
                "bid": _num(t.bid),
                "ask": _num(t.ask),
            })
        for o in batch:
            ib.cancelMktData(o)
        ib.sleep(0.2)

    df = pd.DataFrame(rows, columns=["strike", "right", "bid", "ask"])
    df.attrs["spot"] = spot
    df.attrs["expiration"] = exp
    df.attrs["snapshot_time"] = datetime.now().isoformat(timespec="milliseconds")
    return df
