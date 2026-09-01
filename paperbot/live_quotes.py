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

import copy
import time
from dataclasses import dataclass

from ib_async import Stock

import config


# =========================================================================================
# MUTUAL FUNDS — the ONE instrument class that has no live quote BY CONSTRUCTION.
# =========================================================================================
# A mutual fund does not trade intraday. It prices ONCE A DAY, at NAV, after the close, and
# every order entered that day fills at that same NAV. So reqMktData returning nothing for a
# fund is not a failure, not an entitlement gap and not an after-hours artifact — there is no
# such thing as a fund's live bid/ask/last. MEASURED LIVE, READ-ONLY, 2026-09-01 on the 4003
# gateway: every price field came back NaN for all seven funds the two Stevens accounts hold
# (AFMBX DFISX DODGX HIGFX MFEKX MIEIX TRGXX), while the SAME contracts priced perfectly on
# the broker's own portfolio feed.
#
# THIS IS NOT A REINSTATEMENT OF THE STALE-CLOSE FALLBACK v0.42.0 REMOVED.
# That removal was about substituting an OLD STORED CLOSE from the strategy data when a LIVE
# quote for a normally-quoted instrument failed — a price you cannot trade at, standing in for
# a price we simply failed to get. This is a different thing in every respect:
#   * it is scoped to ONE instrument class that has no live quote AT ALL, by construction;
#   * the number comes from THE BROKER'S OWN CURRENT BOOK (ib.portfolio(): marketPrice /
#     marketValue), not from the desk's stored history; and
#   * for a fund the broker's mark IS its last transacting price — the NAV every fund order
#     that day fills at. It is the real execution price, not a stale proxy for one.
# EVERY OTHER INSTRUMENT STAYS LIVE-QUOTE-ONLY. Do not widen FUND_SEC_TYPES.
FUND_SEC_TYPES = frozenset({"FUND"})

# IBKR's routing destination for a mutual fund. The contract ib.positions() hands back for a
# HELD fund carries secType FUND and a real conId but a BLANK exchange (measured 2026-09-01:
# DODGX conId=86797803, exchange=''). The form VERIFIED to qualify is
# Contract(secType="FUND", exchange="FUNDSERV", currency="USD"), so a blank exchange is filled
# in with this — and ONLY when it is blank (see fund_contract).
FUND_EXCHANGE = "FUNDSERV"

# How long to let the broker's per-position value stream fill in before giving up on it, and
# how often to look. BOUNDED BY CONSTRUCTION: reqPnLSingle is non-blocking (it returns an
# object the stream updates), so this is a poll with a deadline, never an unbounded await.
# A fund still unpriced at the deadline is REPORTED and NOT TRADED — fail closed.
FUND_VALUE_WAIT_SEC = 12.0
FUND_VALUE_POLL_SEC = 0.5


def is_fund(sec_type) -> bool:
    """True iff this instrument type is a MUTUAL FUND — the no-intraday-price class. The ONE
    predicate; every fund-specific behaviour on the desk keys off it so none of them can drift
    apart. Deliberately strict: nothing else is a fund."""
    if sec_type is None:
        return False
    return str(sec_type).strip().upper() in FUND_SEC_TYPES


def fund_contract(contract):
    """The contract to QUOTE/TRADE a held mutual fund with.

    ib.positions() hands back a fund with its real conId and secType FUND but a BLANK
    exchange. IBKR resolves the instrument from the conId, but an order needs a destination,
    and FUNDSERV is the one verified to qualify. So: if (and ONLY if) a FUND contract's
    exchange is blank, return a COPY carrying FUNDSERV. The broker's object is never mutated
    (it is shared across every account in the batch) and a contract that already names an
    exchange — fund or not — is returned UNCHANGED, by identity."""
    if not is_fund(getattr(contract, "secType", None)):
        return contract
    if str(getattr(contract, "exchange", "") or "").strip():
        return contract                      # broker already named a destination — leave it
    fixed = copy.copy(contract)
    fixed.exchange = FUND_EXCHANGE
    return fixed


def fund_price_from_value(quantity, market_value) -> float | None:
    """PURE. Per-share NAV from the broker's own reported position size and market value —
    `market_value / quantity`. None when either is missing/zero/NaN, which leaves the symbol
    UNQUOTED and therefore UNTRADED (fail closed), never priced at 0.

    MEASURED 2026-09-01 against the broker's two independent value feeds, which agree exactly:
    AFMBX 5056.84 / 123.73 = 40.87, DODGX 18.22, MFEKX 207.58, HIGFX 9.72, DFISX 28.69,
    MIEIX 44.31, TRGXX 1.00 — identical to the marketPrice each reports."""
    if not _valid(quantity) or market_value is None:
        return None
    try:
        mv = float(market_value)
    except (TypeError, ValueError):
        return None
    if mv != mv or mv <= 0:          # NaN or non-positive -> no price
        return None
    px = mv / float(quantity)
    return float(px) if _valid(px) else None


def fund_prices(ib, fund_positions) -> tuple[dict, list]:
    """{symbol: NAV} for the HELD MUTUAL FUNDS named in `fund_positions`, read from the
    broker's own reported market value, plus the holdings it could not price.

    `fund_positions` is an iterable of (account, contract) for FUND holdings ONLY — the caller
    filters, using is_fund, so this can never be pointed at an ETF.

    WHY THE VALUE COMES FROM reqPnLSingle AND NOT FROM ib.portfolio() — MEASURED, NOT ASSUMED.
    The obvious source is ib.portfolio(), whose PortfolioItem carries BOTH marketPrice and
    marketValue, and both ARE populated for a fund. It is unusable here anyway:

      * ib_async fills its portfolio cache ONLY from the reqAccountUpdates subscription.
        Connecting to a SINGLE-account login auto-subscribes, which is why the single-account
        deploy rail's use of ib.portfolio() works. This is the FA-MASTER login (12 managed
        accounts) and connecting subscribes to NOTHING — so ib.portfolio(account) returns an
        empty list for every account and always has. THAT is why recon_report._portfolio_values
        came back EMPTY for U27295881 and U27305011.
      * Subscribing does work — but ONLY ONCE PER CONNECTION. MEASURED 2026-09-01, both
        orderings, on fresh connections: whichever account is subscribed FIRST returns its full
        portfolio, and EVERY LATER ACCOUNT RETURNS NOTHING, with or without an explicit
        unsubscribe in between. Worse, ib_async's reqAccountUpdates awaits accountDownloadEnd
        under IB.RequestTimeout, WHICH DEFAULTS TO 0 — no timeout at all — so the second
        account does not fail, it HANGS THE WHOLE RUN FOREVER with no output and no error.
        That is exactly what it did to the first batch preview of this change.

    reqPnLSingle has neither problem: it is per-POSITION, many subscriptions coexist happily on
    one connection (10 across both accounts, measured), and it is NON-BLOCKING — it returns an
    object the stream fills in, so the wait below is a poll against a deadline and can never
    hang. Its `value` is the same broker-reported market value, to the cent.

    Read-only: a value subscription places no order. Every subscription is cancelled on the way
    out, including on failure, so the run leaves nothing behind on the connection. A holding
    still unpriced at the deadline is RETURNED NAMED, which leaves it untraded — fail closed."""
    subs: list = []
    unpriced: list = []
    for account, contract in fund_positions:
        symbol = getattr(contract, "symbol", None)
        con_id = getattr(contract, "conId", 0)
        if not symbol or not con_id:
            unpriced.append(f"{account}/{symbol or '?'}")
            continue
        try:
            subs.append((account, symbol, con_id, ib.reqPnLSingle(account, "", con_id)))
        except Exception:  # noqa: BLE001 — one bad holding must never take the run down
            unpriced.append(f"{account}/{symbol}")

    prices: dict = {}
    try:
        deadline = time.monotonic() + FUND_VALUE_WAIT_SEC
        pending = list(subs)
        while pending and time.monotonic() < deadline:
            ib.sleep(FUND_VALUE_POLL_SEC)
            still: list = []
            for account, symbol, con_id, s in pending:
                px = fund_price_from_value(getattr(s, "position", None),
                                           getattr(s, "value", None))
                if px is None:
                    still.append((account, symbol, con_id, s))
                else:
                    prices[symbol] = px
            pending = still
        unpriced.extend(f"{a}/{sym}" for a, sym, _c, _s in pending)
    finally:
        for account, _symbol, con_id, _s in subs:
            try:
                ib.cancelPnLSingle(account, "", con_id)
            except Exception:  # noqa: BLE001 — best effort cleanup; never fatal
                pass
    return prices, sorted(set(unpriced))


def report_fund_prices(prices: dict, unpriced: list, indent: str = "    ") -> None:
    """Say out loud where a fund's price came from — it is NOT a live quote, and a human
    reading a preview must never have to guess which prices are which."""
    if prices:
        print(f"{indent}MUTUAL FUNDS priced at NAV from the broker's own reported market "
              f"value, not from a live quote — a mutual fund has NO intraday price: it prices "
              f"once a day at NAV after the close and every order that day fills at that same "
              f"NAV. {len(prices)} fund(s): "
              + ", ".join(f"{s} {p:,.2f}" for s, p in sorted(prices.items())))
    if unpriced:
        print(f"{indent}!! THE BROKER REPORTED NO USABLE VALUE for {len(unpriced)} mutual-fund "
              f"holding(s): {', '.join(unpriced)}. These have no price and WILL NOT BE TRADED "
              f"on this run — not bought, not sold.")


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
            # The broker's own — never rebuilt. The ONE adjustment: a held MUTUAL FUND comes
            # back with a BLANK exchange, and an order needs a destination, so fund_contract
            # fills in FUNDSERV (and ONLY then). Every other contract, fund or not, is
            # returned by identity.
            contracts[s] = fund_contract(c)
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
