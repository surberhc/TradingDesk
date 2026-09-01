"""test_broker_contracts.py — the desk must use THE CONTRACT THE BROKER ALREADY GAVE US, and
must never die because one holding cannot be resolved.

THE BUG THIS PINS (reproduced live 2026-09-01). Two client accounts (U27295881, U27305011)
were moved onto custom models while still holding MUTUAL FUNDS from a previous advisor. Both
the quote path and the transmit path RECONSTRUCTED a contract from the ticker string and
assumed every instrument is a US stock — Stock(symbol, "SMART", "USD"). A mutual fund is not:
IBKR answers "Unknown contract", conId is never populated, and reqMktData then RAISES while
hashing it —

    ValueError: Contract Stock(symbol='AFMBX', exchange='SMART', currency='USD') can't be
    hashed because no 'conId' value exists. Qualify contract to populate 'conId'.

— which killed the preview for ALL 15-16 accounts in the batch, not just those two.

ESTABLISHED LIVE, READ-ONLY (2026-09-01), and encoded in these fakes:
  AFMBX DFISX DODGX HIGFX MFEKX MIEIX TRGXX qualify FINE as secType FUND on FUNDSERV, each
  with a real conId (DODGX = 86797803). They do NOT qualify as Stock.
  BIV IVV VO and the other ETFs qualify fine as Stock on SMART, and NOT as FUND.
IBKR knows all of them. Only our contract construction was wrong.
"""
from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

import config
import live_quotes
import order_router
import safe_execute as se


ACCT = "U27295881"

# The seven funds the two Stevens accounts actually hold, and what IBKR really answers for
# them. Keyed the way the broker keys them: a FUND contract on FUNDSERV with a real conId.
FUND_CONIDS = {"AFMBX": 12345001, "DFISX": 12345002, "DODGX": 86797803, "HIGFX": 12345004,
               "MFEKX": 12345005, "MIEIX": 12345006, "TRGXX": 12345007}
ETFS = {"BIV": 55001, "IVV": 55002, "VO": 55003, "SCHB": 55004, "USFR": 55005}


class _BrokerContract:
    """Stands in for the fully-qualified contract ib.positions() hands back: it already
    carries the real conId / secType / exchange, so there is nothing to reconstruct."""

    def __init__(self, symbol, sec_type, exchange, con_id):
        self.symbol = symbol
        self.secType = sec_type
        self.exchange = exchange
        self.currency = "USD"
        self.conId = con_id

    def __repr__(self):  # pragma: no cover - diagnostics only
        return (f"Contract(symbol={self.symbol!r}, secType={self.secType!r}, "
                f"exchange={self.exchange!r}, conId={self.conId})")


def _fund_contract(symbol):
    return _BrokerContract(symbol, "FUND", "FUNDSERV", FUND_CONIDS[symbol])


class _QuoteIB:
    """Fake broker for the QUOTE path that behaves like the real one in the ONE way that
    matters: qualifyContracts returns a slot per input with None where IBKR will not resolve
    the contract, and reqMktData RAISES on an unqualified contract — the actual crash."""

    def __init__(self, resolvable=None):
        self.resolvable = dict(ETFS if resolvable is None else resolvable)
        self.qualified = []        # every contract handed to qualifyContracts, flattened
        self.md = []               # every contract that actually reached reqMktData
        self.md_types = []

    def reqMarketDataType(self, n):
        self.md_types.append(n)

    def qualifyContracts(self, *contracts):
        out = []
        for c in contracts:
            self.qualified.append(c)
            con_id = self.resolvable.get(c.symbol)
            if con_id is None:
                out.append(None)          # IBKR: "Unknown contract: ..."
                continue
            c.conId = con_id
            out.append(c)
        return out

    def reqMktData(self, contract, *a, **k):
        # THE REGRESSION PIN: this is what the live run hit. An unqualified contract cannot be
        # hashed, so reqMktData blows up and takes every other account down with it.
        if not getattr(contract, "conId", 0):
            raise ValueError(f"Contract {contract} can't be hashed because no 'conId' value "
                             f"exists. Qualify contract to populate 'conId'.")
        self.md.append(contract)
        return SimpleNamespace(bid=100.0, ask=100.5, last=100.25, close=99.75,
                               marketDataType=1)

    def sleep(self, *a, **k):
        return None


# =========================================================================================
# QUOTE PATH — live_quotes
# =========================================================================================
def test_the_fake_reproduces_the_live_crash_shape():
    """Guard on the guard: prove this fake really does raise the live ValueError for an
    unqualified AFMBX Stock, so the tests below are pinning the real failure."""
    from ib_async import Stock

    ib = _QuoteIB()
    bad = Stock("AFMBX", "SMART", "USD")
    assert not getattr(bad, "conId", 0)
    with pytest.raises(ValueError, match="can't be hashed"):
        ib.reqMktData(bad, "", snapshot=True)


def test_held_fund_is_quoted_against_the_brokers_contract_not_a_rebuilt_stock():
    """A held mutual fund is quoted using the contract ib.positions() gave us — the SAME
    object, FUND/FUNDSERV/real conId — and is never rebuilt or re-qualified as a Stock."""
    dodgx = _fund_contract("DODGX")
    ib = _QuoteIB()

    quotes = live_quotes.fetch(ib, {"DODGX": dodgx, "IVV": None})

    assert set(quotes) == {"DODGX", "IVV"}
    sent = {c.symbol: c for c in ib.md}
    assert sent["DODGX"] is dodgx                       # the broker's own object, verbatim
    assert (sent["DODGX"].secType, sent["DODGX"].exchange) == ("FUND", "FUNDSERV")
    assert sent["DODGX"].conId == 86797803
    # It was never handed to qualifyContracts either — there is nothing to qualify.
    assert "DODGX" not in [c.symbol for c in ib.qualified]
    # The ETF still took the Stock path.
    assert [c.symbol for c in ib.qualified] == ["IVV"]


def test_every_stevens_fund_is_quoted_from_the_brokers_contract():
    """All seven funds the two accounts hold — none rebuilt, none dropped, none crashing."""
    known = {s: _fund_contract(s) for s in FUND_CONIDS}
    ib = _QuoteIB()

    quotes = live_quotes.fetch(ib, known)

    assert set(quotes) == set(FUND_CONIDS)
    assert {c.symbol for c in ib.md} == set(FUND_CONIDS)
    assert ib.qualified == []                           # nothing was reconstructed at all


def test_unqualifiable_symbol_is_dropped_and_named_and_fetch_returns_normally(capsys):
    """THE REGRESSION PIN. AFMBX arrives as a bare ticker (nobody handed us a contract), so it
    is rebuilt as a Stock, IBKR will not resolve it, and it must be DROPPED and NAMED — never
    reaching reqMktData. fetch MUST RETURN NORMALLY: the rest of the book still gets quoted."""
    ib = _QuoteIB()

    quotes = live_quotes.fetch(ib, ["AFMBX", "BIV", "IVV", "VO"])   # must not raise

    assert set(quotes) == {"BIV", "IVV", "VO"}
    assert "AFMBX" not in quotes
    assert "AFMBX" not in [c.symbol for c in ib.md]     # never reached the crash site
    out = capsys.readouterr().out
    assert "IBKR WOULD NOT RESOLVE A CONTRACT" in out
    assert "AFMBX" in out
    assert "WILL NOT BE TRADED" in out


def test_one_unqualifiable_holding_does_not_kill_the_whole_book():
    """The shape of the live incident: 16 accounts' worth of symbols, one of them a fund held
    with no broker contract. Everything else must still price."""
    universe = ["AFMBX", "HIGFX"] + sorted(ETFS)
    ib = _QuoteIB()

    quotes = live_quotes.fetch(ib, universe)

    prices, unquoted = live_quotes.execution_prices(quotes, universe)
    assert set(prices) == set(ETFS)                     # every ETF still priced
    assert unquoted == ["AFMBX", "HIGFX"]               # and the two funds are NAMED, not lost


def test_qualified_contracts_names_the_unresolvable_symbols():
    ib = _QuoteIB()
    contracts, unqualified = live_quotes.qualified_contracts(ib, ["IVV", "AFMBX", "DODGX"])
    assert set(contracts) == {"IVV"}
    assert unqualified == ["AFMBX", "DODGX"]            # sorted, so a report is stable


def test_a_target_symbol_nobody_holds_still_takes_the_unchanged_stock_path():
    """A model target no account holds has no broker contract, so it must be built EXACTLY as
    before: Stock(symbol, "SMART", "USD"), qualified once, in one batched call."""
    ib = _QuoteIB()

    live_quotes.fetch(ib, {"IVV": None, "SCHB": None})

    assert [(c.symbol, c.secType, c.exchange, c.currency) for c in ib.qualified] == [
        ("IVV", "STK", "SMART", "USD"), ("SCHB", "STK", "SMART", "USD")]
    assert [c.symbol for c in ib.md] == ["IVV", "SCHB"]


def test_all_etf_account_is_byte_identical_list_or_mapping():
    """EXISTING BEHAVIOUR UNCHANGED. An all-ETF account holds nothing exotic, so the plain
    list form and the mapping form must produce the same contracts, in the same order, with
    the same market-data requests — and reqMarketDataType(1) is still asked for first."""
    syms = ["BIV", "IVV", "VO"]
    ib_list, ib_map = _QuoteIB(), _QuoteIB()

    q_list = live_quotes.fetch(ib_list, syms)
    q_map = live_quotes.fetch(ib_map, {s: None for s in syms})

    assert list(q_list) == list(q_map) == syms
    assert ib_list.md_types == ib_map.md_types == [1]
    for a, b in zip(ib_list.qualified, ib_map.qualified):
        assert (a.symbol, a.secType, a.exchange, a.currency) == \
               (b.symbol, b.secType, b.exchange, b.currency)
    assert [c.symbol for c in ib_list.md] == [c.symbol for c in ib_map.md] == syms
    for s in syms:
        assert q_list[s] == q_map[s]


def test_broker_contracts_are_used_even_when_the_qualify_call_would_fail():
    """A held symbol needs NO broker round-trip. If qualifyContracts blows up entirely, the
    held funds must still be quoted (and only the rebuilt symbols are dropped)."""
    class _Exploding(_QuoteIB):
        def qualifyContracts(self, *contracts):
            raise RuntimeError("gateway hiccup")

    ib = _Exploding()
    quotes = live_quotes.fetch(ib, {"DODGX": _fund_contract("DODGX"), "IVV": None})

    assert set(quotes) == {"DODGX"}                     # the fund still priced
    assert "IVV" not in quotes                          # the unresolvable one dropped, not raised


# =========================================================================================
# TRANSMIT PATH — safe_execute
# =========================================================================================
class _TxIB:
    """Transmit-path fake that records the CONTRACT each order was placed against."""

    def __init__(self, resolvable=None):
        self.resolvable = dict(ETFS if resolvable is None else resolvable)
        self.placed = []           # (contract, order)

    def reqMarketDataType(self, n):
        return None

    def reqAllOpenOrders(self):
        return []

    def qualifyContracts(self, *contracts):
        out = []
        for c in contracts:
            con_id = self.resolvable.get(c.symbol)
            if con_id is None:
                out.append(None)
                continue
            c.conId = con_id
            out.append(c)
        return out

    def sleep(self, *a, **k):
        return None

    def cancelOrder(self, order):
        return None

    def placeOrder(self, contract, order):
        self.placed.append((contract, order))
        qty = float(order.totalQuantity)
        return SimpleNamespace(
            orderStatus=SimpleNamespace(status="Filled", filled=qty, remaining=0.0,
                                        avgFillPrice=float(order.lmtPrice or 0.0)),
            isDone=lambda: True)


def _leg(symbol, side, qty, limit):
    return SimpleNamespace(symbol=symbol, side=side, qty=qty, limit=limit,
                           notional=qty * limit, source="plan")


@pytest.fixture()
def _armed(monkeypatch):
    monkeypatch.setattr(config, "READONLY", False)
    monkeypatch.setattr(config, "DRY_RUN", False)
    permit, _why = order_router.transmit_guard(armed=True)
    assert permit is True
    return None


def test_a_sell_of_a_held_fund_is_placed_against_the_brokers_contract(_armed):
    """The half that makes the SELL CORRECT rather than silently wrong: the order goes out on
    the broker's FUND/FUNDSERV contract, not a rebuilt US-stock contract IBKR does not have."""
    dodgx = _fund_contract("DODGX")
    ib = _TxIB()

    results = se._transmit_phase(
        ib, [_leg("DODGX", "SELL", 40, 250.0)], account=ACCT,
        as_of=pd.Timestamp("2026-09-01"), run_id="R1", phase_label="SELL",
        quotes={}, prices={"DODGX": 250.0}, contracts={"DODGX": dodgx})

    assert len(ib.placed) == 1
    contract, order = ib.placed[0]
    assert contract is dodgx
    assert (contract.secType, contract.exchange, contract.conId) == ("FUND", "FUNDSERV",
                                                                    86797803)
    assert order.action == "SELL" and float(order.totalQuantity) == 40
    assert results[0]["skipped"] is False and results[0]["filled"] == 40.0


def test_an_unqualifiable_leg_is_not_placed_and_is_reported(_armed, capsys):
    """Fail closed, same posture as an unpriceable leg: no contract -> no order, named loudly,
    and the OTHER legs in the phase still go out."""
    ib = _TxIB()

    results = se._transmit_phase(
        ib, [_leg("AFMBX", "SELL", 10, 25.0), _leg("IVV", "SELL", 5, 600.0)],
        account=ACCT, as_of=pd.Timestamp("2026-09-01"), run_id="R1", phase_label="SELL",
        quotes={}, prices={"AFMBX": 25.0, "IVV": 600.0}, contracts={})

    assert [c.symbol for c, _o in ib.placed] == ["IVV"]        # AFMBX never placed
    by_symbol = {r["symbol"]: r for r in results}
    assert by_symbol["AFMBX"]["skipped"] is True
    assert by_symbol["AFMBX"]["status"] == "SKIPPED_UNQUALIFIED"
    assert by_symbol["AFMBX"]["filled"] == 0.0
    assert "would not resolve a contract" in by_symbol["AFMBX"]["reason"]
    assert by_symbol["IVV"]["skipped"] is False
    out = capsys.readouterr().out
    assert "NOT PLACED SELL AFMBX" in out


def test_transmit_for_an_all_etf_account_is_unchanged(_armed):
    """EXISTING BEHAVIOUR UNCHANGED. With no broker contracts supplied, an ETF leg is still
    built as Stock(symbol, "SMART", "USD") and placed exactly as before."""
    ib = _TxIB()

    results = se._transmit_phase(
        ib, [_leg("IVV", "BUY", 3, 600.0)], account=ACCT,
        as_of=pd.Timestamp("2026-09-01"), run_id="R1", phase_label="BUY",
        quotes={}, prices={"IVV": 600.0}, contracts=None)

    assert len(ib.placed) == 1
    contract, order = ib.placed[0]
    assert (contract.symbol, contract.secType, contract.exchange, contract.currency) == (
        "IVV", "STK", "SMART", "USD")
    assert order.action == "BUY" and float(order.totalQuantity) == 3
    assert results[0]["filled"] == 3.0


def test_leg_contract_prefers_the_broker_and_refuses_the_unresolvable():
    """The one chooser, directly: broker contract wins; unknown ticker -> None (do not place);
    known ETF -> the historic Stock construction."""
    ib = _TxIB()
    dodgx = _fund_contract("DODGX")
    assert se._leg_contract(ib, "DODGX", {"DODGX": dodgx}) is dodgx
    assert se._leg_contract(ib, "AFMBX", {}) is None
    built = se._leg_contract(ib, "IVV", {})
    assert (built.symbol, built.secType, built.exchange, built.currency) == (
        "IVV", "STK", "SMART", "USD")


def test_execution_request_carries_the_contract_map_and_defaults_empty():
    """Threaded on the request beside quotes/prices — and DEFAULTED, so every pre-existing
    construction (crm_execute's included) is unchanged."""
    req = se.ExecutionRequest(
        account=ACCT, strategy_version="Growth (Custom)", plan=object(), target=object(),
        quotes={}, prices={}, allowed_accounts=[ACCT], caps=se.ExecutionCaps(),
        conform=False, run_id=None, net_liq=1.0)
    assert req.contracts == {}
    req.contracts = {"DODGX": _fund_contract("DODGX")}
    assert req.contracts["DODGX"].secType == "FUND"
