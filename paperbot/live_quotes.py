"""
live_quotes.py — live IBKR market quotes for order sizing + limit pricing (read-only).

Replaces "last close from the strategy data" with the real market: bid / ask / last
pulled from the paper connection. READ-ONLY — requesting market data places no order.

Limit-price policy (config.ORDER_STYLE):
  * "limit" (default, neutral): the last trade, falling back to bid/ask midpoint, then
    the prior close. A fair resting limit, not chasing the spread.
  * "marketable_limit": cross the spread to fill now — BUY at the ask, SELL at the bid.
  * "market": last/close as a reference (we still attach a limit; no naked market order).

Every value is validated (None/NaN/<=0 are treated as "unavailable") so a missing tick
never silently becomes a $0 order; callers fall back to the strategy-data close.
"""
from __future__ import annotations

from dataclasses import dataclass

from ib_async import Stock

import config


@dataclass
class Quote:
    symbol: str
    bid: float | None
    ask: float | None
    last: float | None
    close: float | None
    md_type: int          # 1=live, 2=frozen, 3=delayed, 4=delayed-frozen


def _valid(x) -> bool:
    return x is not None and x == x and x > 0   # not None, not NaN, positive


def fetch(ib, symbols, wait: float = 3.0) -> dict:
    """Snapshot quotes for `symbols`. Requests live data (IBKR serves delayed if a
    symbol lacks a live entitlement). Snapshots auto-cancel — nothing to clean up."""
    ib.reqMarketDataType(1)   # prefer live; IBKR downgrades per-symbol if needed
    contracts = {s: Stock(s, "SMART", "USD") for s in symbols}
    if contracts:
        ib.qualifyContracts(*contracts.values())
    tickers = {s: ib.reqMktData(c, "", snapshot=True) for s, c in contracts.items()}
    ib.sleep(wait)
    quotes = {}
    for s, t in tickers.items():
        quotes[s] = Quote(
            symbol=s,
            bid=float(t.bid) if _valid(t.bid) else None,
            ask=float(t.ask) if _valid(t.ask) else None,
            last=float(t.last) if _valid(t.last) else None,
            close=float(t.close) if _valid(t.close) else None,
            md_type=int(t.marketDataType) if t.marketDataType else 0,
        )
    return quotes


def _mid(q: Quote) -> float | None:
    if _valid(q.bid) and _valid(q.ask):
        return (q.bid + q.ask) / 2.0
    return None


def reference_price(q: Quote) -> float | None:
    """A neutral current price for SIZING: last, else midpoint, else close."""
    for candidate in (q.last, _mid(q), q.close):
        if _valid(candidate):
            return candidate
    return None


def limit_price(side: str, q: Quote, style: str | None = None) -> float | None:
    """The limit price for an order of `side`, per ORDER_STYLE. None if unavailable."""
    style = style or config.ORDER_STYLE
    if style == "marketable_limit":
        ref = q.ask if side == "BUY" else q.bid
        if not _valid(ref):
            ref = reference_price(q)
    elif style == "market":
        ref = reference_price(q)
    else:  # neutral "limit"
        ref = reference_price(q)
    return round(ref, 2) if _valid(ref) else None


def relative_spread(q: Quote) -> float | None:
    """(ask-bid)/mid — the live relative spread width, used to classify an unknown
    symbol as liquid vs illiquid. None if a usable two-sided quote is unavailable."""
    mid = _mid(q)
    if mid is None or not (_valid(q.bid) and _valid(q.ask)):
        return None
    return (q.ask - q.bid) / mid


def execution_prices(quotes: dict, symbols) -> tuple[dict, list]:
    """THE execution-path price map: {symbol: live reference price} plus the list of
    symbols IBKR would NOT quote. (owner decision, v0.42.0)

    The desk does not maintain prices for execution — IBKR is the price source. If IBKR
    gives a quote we use it; if IBKR will not quote a symbol, that symbol DOES NOT TRADE
    and we say so, naming it. This function is the single place that decision is made, so
    no rail can quietly reintroduce a fallback of its own.

    What this deliberately REMOVED: every execution rail used to fall back to the model's
    stored daily close when a quote was missing, and the batch rail then silently dropped
    the key when even that was absent. A stale close is not a price you can trade at, and
    the silent drop was read downstream as "target 0 shares" — i.e. SELL EVERYTHING.

    The strategy's stored price HISTORY is untouched and still does its real job upstream:
    computing the model's target WEIGHTS. That is not execution.

    Returns (prices, unquoted); `unquoted` is sorted so a caller can count and print it."""
    prices: dict = {}
    unquoted: list = []
    for sym in sorted(set(symbols)):
        q = quotes.get(sym)
        ref = reference_price(q) if q is not None else None
        if _valid(ref):
            prices[sym] = float(ref)
        else:
            unquoted.append(sym)
    return prices, unquoted


def report_unquoted(unquoted: list, indent: str = "    ") -> None:
    """Print the no-quote tally LOUDLY. Never a silent omission (v0.42.0): before this the
    batch rail dropped these symbols with no tally, no counter and no warning."""
    if not unquoted:
        return
    print(f"{indent}!! IBKR RETURNED NO USABLE QUOTE for {len(unquoted)} symbol(s): "
          f"{', '.join(unquoted)}")
    print(f"{indent}   These symbols WILL NOT BE TRADED on this run — not bought, not "
          f"sold. No stored/stale close is substituted for a live quote on the execution "
          f"path. Any account whose model wants one of them is reported NOT in spec.")


def marketable_cap(side: str, q: Quote, k: float | None = None) -> float | None:
    """The WORST-CASE marketable cap for a laddered order: BUY = ask*(1+k),
    SELL = bid*(1-k). This is the hard price a rung will pay to GET DONE; pegs/algos
    usually fill better. Falls back to the neutral reference (then a tiny k pad) when a
    one-sided quote is missing, so the cap is still a real, positive, marketable number.
    None only when no usable price exists at all (caller then routes the PRICE GUARD).

    The returned value is ROUNDED to a cent and must still be passed through the router's
    HARD PRICE GUARD by the caller — this function does no validation of its own beyond
    requiring a usable input price."""
    k = config.ORDER_CAP_K if k is None else k
    touch = q.ask if side == "BUY" else q.bid
    if not _valid(touch):
        # No touch on our crossing side — pad the neutral reference toward marketable.
        ref = reference_price(q)
        if not _valid(ref):
            return None
        touch = ref
    cap = touch * (1 + k) if side == "BUY" else touch * (1 - k)
    return round(cap, 2) if _valid(cap) else None
