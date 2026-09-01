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


def qualified_contracts(ib, symbols, known=None) -> tuple[dict, list]:
    """{symbol: contract} to quote/trade `symbols` with, PLUS the symbols IBKR will not
    resolve. THE one place a contract is chosen, so no rail reintroduces a guess of its own.

    `known` maps symbol -> a contract THE BROKER ALREADY HANDED US. `ib.positions()` returns
    a resolved contract per holding — MEASURED 2026-09-01, a fund comes back as secType FUND
    with its real conId (DODGX 86797803) and a BLANK exchange, which is IBKR's own identity
    for the instrument. There is nothing for us to reconstruct, so it is used AS-IS.

    Every OTHER symbol is built as Stock(symbol, "SMART", "USD") — the desk's long-standing
    assumption — and then QUALIFIED before use. That assumption is wrong for anything that is
    not a US stock/ETF. A MUTUAL FUND is secType FUND on FUNDSERV: IBKR answers "Unknown
    contract", the conId is never populated, and reqMktData then RAISES while hashing the
    unqualified contract ("can't be hashed because no 'conId' value exists"). MEASURED live
    2026-09-01: two accounts newly moved onto custom models (U27295881, U27305011) held
    mutual funds from a previous advisor (AFMBX DFISX DODGX HIGFX MFEKX MIEIX TRGXX) and ONE
    of those holdings killed the ENTIRE 16-account batch preview. IBKR knows all of them —
    only our contract construction was wrong.

    ib_async.qualifyContracts returns ONE SLOT PER INPUT, IN INPUT ORDER, holding None where
    IBKR would not resolve the contract. That None is THE BROKER'S OWN ANSWER, and it is
    exactly the case that leaves conId unset — so it is what we key on. If the broker's reply
    is unusable (wrong length, or it raised) we fall back to the literal crash condition: no
    conId. Either way the symbol is DROPPED here and returned NAMED; it can never reach
    reqMktData or an order."""
    known = known or {}
    ordered = list(dict.fromkeys(symbols))          # input order, no duplicate requests
    rebuilt = {s: Stock(s, "SMART", "USD") for s in ordered if known.get(s) is None}

    resolved = None
    if rebuilt:
        try:
            resolved = ib.qualifyContracts(*rebuilt.values())
        except Exception:  # noqa: BLE001 — an unreadable reply must DROP, never crash the run
            resolved = None
    keys = list(rebuilt)
    usable_reply = isinstance(resolved, (list, tuple)) and len(resolved) == len(keys)
    ok_rebuilt: set = set()
    for i, s in enumerate(keys):
        ok = (resolved[i] is not None) if usable_reply else bool(getattr(rebuilt[s], "conId", 0))
        if ok:
            ok_rebuilt.add(s)

    contracts: dict = {}
    unqualified: list = []
    for s in ordered:
        c = known.get(s)
        if c is not None:
            contracts[s] = c                        # the broker's own — never rebuilt
        elif s in ok_rebuilt:
            contracts[s] = rebuilt[s]
        else:
            unqualified.append(s)
    return contracts, sorted(unqualified)


def report_unqualified(unqualified: list, indent: str = "    ") -> None:
    """Print the no-contract tally LOUDLY — the same convention as report_unquoted, because
    the outcome is the same: the symbol DOES NOT TRADE and is named. Before this the run did
    not report it at all; it raised and took every other account down with it."""
    if not unqualified:
        return
    print(f"{indent}!! IBKR WOULD NOT RESOLVE A CONTRACT for {len(unqualified)} symbol(s): "
          f"{', '.join(unqualified)}")
    print(f"{indent}   There is nothing to quote or to place an order against, so these "
          f"symbols WILL NOT BE TRADED on this run — not bought, not sold. Every other "
          f"account and symbol on the run is unaffected.")


def fetch(ib, symbols, wait: float = 3.0) -> dict:
    """Snapshot quotes for `symbols`. Requests live data (IBKR serves delayed if a
    symbol lacks a live entitlement). Snapshots auto-cancel — nothing to clean up.

    `symbols` is the usual sequence of tickers, OR a MAPPING of symbol -> the BROKER'S OWN
    already-qualified contract (from ib.positions(); a None value means "no contract, rebuild
    it"). A symbol with a broker contract is quoted against THAT contract — the mutual-fund
    fix; every other symbol is built and qualified exactly as before. The signature is
    deliberately UNCHANGED: a mapping already IS an iterable of its symbols, so all thirteen
    existing callers are byte-identical and no rail had to learn a new argument.

    A symbol IBKR will not resolve is dropped and NAMED here, and is then simply absent from
    the returned quotes — which execution_prices already reports as unquoted, i.e. this
    rail's established "this symbol does not trade" behaviour. Nothing unqualified ever
    reaches reqMktData, which is what used to raise and kill the whole run."""
    ib.reqMarketDataType(1)   # prefer live; IBKR downgrades per-symbol if needed
    known = symbols if isinstance(symbols, dict) else None
    contracts, unqualified = qualified_contracts(ib, symbols, known=known)
    report_unqualified(unqualified)
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
