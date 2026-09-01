"""
test_mutual_fund_sales.py — SELLING A CLIENT'S TRANSFERRED-IN MUTUAL FUNDS.

WHY THIS EXISTS. Clients arrive holding a previous advisor's mutual funds, and the desk has
to be able to SELL them so the account can be converted to its assigned model. Two live
accounts (U27295881, U27305011) sat fully BLOCKED because their fund holdings had no usable
price: reqMktData returns every field NaN for a mutual fund, the engine saw a HELD holding it
could not value, and it withheld the whole account's orders (fail-closed, correctly).

THE THREE FACTS ABOUT MUTUAL FUNDS THAT DRIVE EVERY TEST BELOW, none of them negotiable:
  1. A mutual fund has NO INTRADAY PRICE, EVER. It prices once a day, at NAV, after the close,
     and every order entered that day fills at that same NAV. The missing quote is not a
     failure — so the standing LIVE-QUOTE-ONLY rule (owner decision, v0.42.0) is
     UNSATISFIABLE for this instrument class by construction, and a fund is priced from the
     BROKER'S OWN book instead. Nothing else is.
  2. A fund order carries NO LIMIT PRICE. It is a market order.
  3. Fund positions are FRACTIONAL, and fund proceeds DO NOT SETTLE SAME DAY.

MEASURED LIVE, READ-ONLY, on the 4003 gateway 2026-09-01 (and encoded in the fakes here):
  * ib.positions() returns a fund as secType FUND with a real conId and a BLANK exchange
    (DODGX conId=86797803). Contract(secType="FUND", exchange="FUNDSERV", currency="USD") is
    the form that qualifies; the funds do NOT qualify as Stock.
  * reqMktData returns EVERY price field NaN for all seven funds.
  * ib.portfolio(account) returns 0 items on this FA-MASTER login — connecting subscribes to
    nothing, which is exactly why recon_report._portfolio_values came back EMPTY for both
    accounts. reqAccountUpdates fills it, but ONLY FOR THE FIRST ACCOUNT ON A CONNECTION
    (measured both orderings, fresh connections): every later account returns nothing, and the
    request HANGS FOREVER rather than failing, because ib_async awaits it under
    IB.RequestTimeout which defaults to 0. It hung the first batch preview of this change.
  * reqPnLSingle has neither problem — many subscriptions coexist on one connection and it is
    non-blocking — and reports the SAME broker value to the cent: AFMBX 5056.84 on 123.73
    shares = 40.87; DODGX 18.22; MFEKX 207.58; HIGFX 9.72; DFISX 28.69; MIEIX 44.31;
    TRGXX 1.00. That is the source the desk uses.

STILL UNPROVEN AND DELIBERATELY NOT ASSERTED HERE: whether IBKR ACCEPTS the resulting order.
The 4003 gateway is ReadOnlyApi=yes and refuses transmission at the API boundary, so order
acceptance of a FUND contract, of a fractional quantity, and of a market order have NOT been
demonstrated live. These tests pin what the desk BUILDS, not what the broker accepts.

ZERO real transmit. NO broker, NO gateway, NO network.

Run:
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe -m pytest test_mutual_fund_sales.py -q
"""
from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

import config
import live_quotes
import order_router
import safe_execute as se
import strategy_target

ACCT = "U27295881"

# The real NAVs read off the broker's portfolio feed, 2026-09-01.
NAV = {"AFMBX": 40.86999895, "DFISX": 28.69000055, "DODGX": 18.2199993,
       "MIEIX": 44.31000135, "TRGXX": 1.0}
# The real fractional positions Stevens holds.
HELD = {"AFMBX": 123.73, "MFEKX": 17.393, "MIEIX": 93.273}


# =========================================================================================
# fakes
# =========================================================================================
class _Contract:
    """Stands in for the fully-qualified contract ib.positions() hands back."""

    def __init__(self, symbol, sec_type, exchange, con_id=1):
        self.symbol = symbol
        self.secType = sec_type
        self.exchange = exchange
        self.currency = "USD"
        self.conId = con_id


def _fund_c(symbol, exchange=""):
    """A HELD fund exactly as the broker returns it: real conId, BLANK exchange."""
    return _Contract(symbol, "FUND", exchange, con_id=86797803)


def _etf_c(symbol):
    return _Contract(symbol, "STK", "ARCA", con_id=55001)


class _ValueIB:
    """Fake broker for the fund-VALUE path. reqPnLSingle is NON-BLOCKING in ib_async — it
    returns an object the stream fills in later — so this returns an empty one and populates
    it on the Nth sleep, which is exactly the shape live_quotes.fund_prices polls against."""

    def __init__(self, books, *, fail=(), fills_after=1, never=()):
        self.books = books                  # {(account, conId): (position, value)}
        self.fail = set(fail)               # conIds whose subscription raises
        self.never = set(never)             # conIds that never report a value
        self.fills_after = fills_after
        self.subscribed: list = []
        self.cancelled: list = []
        self._objs: list = []
        self._sleeps = 0

    def reqPnLSingle(self, account, model, con_id):
        if con_id in self.fail:
            raise RuntimeError("pnl subscription refused")
        self.subscribed.append((account, con_id))
        obj = SimpleNamespace(account=account, conId=con_id, position=0.0,
                              value=float("nan"))
        self._objs.append(obj)
        return obj

    def cancelPnLSingle(self, account, model, con_id):
        self.cancelled.append((account, con_id))

    def sleep(self, *a, **k):
        self._sleeps += 1
        if self._sleeps < self.fills_after:
            return None
        for o in self._objs:
            if o.conId in self.never:
                continue
            pos, val = self.books.get((o.account, o.conId), (None, None))
            if pos is not None:
                o.position, o.value = pos, val
        return None


class _FakeTrade:
    def __init__(self, order, *, fill):
        q = float(order.totalQuantity)
        if fill:
            self.orderStatus = SimpleNamespace(status="Filled", filled=q, remaining=0.0,
                                               avgFillPrice=0.0)
            self._done = True
        else:
            self.orderStatus = SimpleNamespace(status="PreSubmitted", filled=0.0,
                                               remaining=q, avgFillPrice=0.0)
            self._done = False

    def isDone(self):
        return self._done


class _TxIB:
    """Transmit-path fake. `no_fill` names symbols whose orders stay working."""

    def __init__(self, summary_rows, *, no_fill=()):
        self._summary = summary_rows
        self.no_fill = set(no_fill)
        self.placed: list = []              # (contract, order)
        self.cancelled: list = []

    def accountSummary(self):
        return self._summary

    def positions(self):
        return []

    def reqAllOpenOrders(self):
        return []

    def qualifyContracts(self, *contracts):
        for c in contracts:
            c.conId = 55001
        return list(contracts)

    def sleep(self, *a, **k):
        return None

    def cancelOrder(self, order):
        self.cancelled.append(order)

    def placeOrder(self, contract, order):
        self.placed.append((contract, order))
        return _FakeTrade(order, fill=contract.symbol not in self.no_fill)


def _row(tag, value, account=ACCT):
    return SimpleNamespace(account=account, tag=tag, value=value)


def _summary(net_liq="100000", buying_power="100000", total_cash="100000"):
    return [_row("NetLiquidation", net_liq), _row("BuyingPower", buying_power),
            _row("TotalCashValue", total_cash)]


def _line(symbol, actual_shares):
    return SimpleNamespace(symbol=symbol, actual_shares=float(actual_shares),
                           target_weight=0.0, target_shares=0, actual_weight=0.0,
                           drift_weight=0.0, status="DRIFTED")


def _plan(*, orders, lines, investable=98_500.0, net_liq=100_000.0, alien_lines=None):
    return SimpleNamespace(
        account=ACCT, version="Growth (Custom)", net_liq=net_liq, reserve=0.0,
        investable=investable, lines=list(lines), needs_rebalance=True, orders=dict(orders),
        alien_lines=list(alien_lines or []), managed_net_liq=net_liq,
        blocked_reasons=[], unpriced_reasons=[])


def _target(weights=None, prices=None):
    weights = weights or {"VTI": 1.0}
    prices = prices or {"VTI": 250.0}
    return strategy_target.Target(
        weights=pd.Series(weights), prices=pd.Series(prices),
        as_of=pd.Timestamp("2026-09-01"), price_date=pd.Timestamp("2026-09-01"),
        version="Growth (Custom)")


def _request(*, plan, target, contracts, prices, armed=False, total_cash="100000",
             net_liq=100_000.0):
    return se.ExecutionRequest(
        account=ACCT, strategy_version="Growth (Custom)", plan=plan, target=target,
        quotes={}, prices=dict(prices), allowed_accounts=[ACCT], caps=se.ExecutionCaps(),
        conform=False, run_id=None, net_liq=net_liq,
        summary=_summary(total_cash=total_cash), armed=armed, kill=False,
        purpose=se.PURPOSE_REBALANCE, contracts=dict(contracts))


@pytest.fixture(autouse=True)
def _no_config_flag_leak():
    prev_dry_run, prev_readonly = config.DRY_RUN, config.READONLY
    try:
        yield
    finally:
        config.DRY_RUN, config.READONLY = prev_dry_run, prev_readonly


@pytest.fixture()
def _armed(monkeypatch):
    monkeypatch.setattr(config, "READONLY", False)
    monkeypatch.setattr(config, "DRY_RUN", False)
    assert order_router.transmit_guard(armed=True)[0] is True


# =========================================================================================
# A. FUND PRICING — from the BROKER'S OWN BOOK, and scoped to secType FUND alone
# =========================================================================================
def test_is_fund_is_strict():
    """The ONE predicate, and it is deliberately narrow. Widening it would re-open the
    stale-price hole v0.42.0 closed for every normally-quoted instrument."""
    assert live_quotes.is_fund("FUND") is True
    assert live_quotes.is_fund(" fund ") is True
    for other in ("STK", "STOCK", "ETF", "BOND", "OPT", "CASH", "MF", "", None):
        assert live_quotes.is_fund(other) is False, other


def test_nav_is_the_brokers_reported_value_over_the_position():
    """The real measured numbers: the broker's own market value divided by its own position
    size reproduces each fund's NAV to the cent."""
    assert live_quotes.fund_price_from_value(123.73, 5056.844967842102) == pytest.approx(40.87)
    assert live_quotes.fund_price_from_value(305.38, 305.38) == pytest.approx(1.0)
    assert live_quotes.fund_price_from_value(17.393, 3610.4389718475345) == pytest.approx(
        207.58)


def test_an_unusable_value_is_no_price_never_zero():
    """Fail closed: anything we cannot turn into a real positive per-share price yields None,
    which leaves the symbol unquoted and therefore UNTRADED — never priced at 0."""
    for qty, val in ((0.0, 100.0), (None, 100.0), (123.73, None), (123.73, 0.0),
                     (123.73, float("nan")), (123.73, -5.0), (float("nan"), 100.0)):
        assert live_quotes.fund_price_from_value(qty, val) is None, (qty, val)


def test_fund_prices_subscribes_reads_and_always_cancels():
    """Many per-position value subscriptions coexist on ONE connection (unlike the portfolio
    subscription), and every one is released on the way out so the run leaves nothing behind."""
    ib = _ValueIB({(ACCT, 1): (123.73, 5056.84), (ACCT, 2): (153.077, 2789.06),
                   ("U27305011", 3): (305.38, 305.38)})
    prices, unpriced = live_quotes.fund_prices(
        ib, [(ACCT, _Contract("AFMBX", "FUND", "", 1)),
             (ACCT, _Contract("DODGX", "FUND", "", 2)),
             ("U27305011", _Contract("TRGXX", "FUND", "", 3))])

    assert unpriced == []
    assert prices["AFMBX"] == pytest.approx(40.87, abs=0.001)
    assert prices["DODGX"] == pytest.approx(18.22, abs=0.001)
    assert prices["TRGXX"] == pytest.approx(1.0, abs=0.001)
    assert sorted(ib.cancelled) == sorted(ib.subscribed)      # nothing left subscribed


def test_a_fund_the_broker_never_values_is_named_and_untraded(monkeypatch):
    """Bounded by a deadline, then fail closed: a holding still unvalued is RETURNED NAMED, so
    it is reported and does not trade. It never hangs and never guesses."""
    monkeypatch.setattr(live_quotes, "FUND_VALUE_WAIT_SEC", 0.2)
    ib = _ValueIB({(ACCT, 1): (123.73, 5056.84)}, never={2})
    prices, unpriced = live_quotes.fund_prices(
        ib, [(ACCT, _Contract("AFMBX", "FUND", "", 1)),
             (ACCT, _Contract("MYSTERY", "FUND", "", 2))])
    assert prices == {"AFMBX": pytest.approx(40.87, abs=0.001)}
    assert unpriced == [f"{ACCT}/MYSTERY"]
    assert sorted(ib.cancelled) == sorted(ib.subscribed)      # cancelled even on failure


def test_one_refused_subscription_does_not_kill_the_run():
    """One bad holding leaves that fund unpriced (and untraded) and is REPORTED — it never
    takes the other holdings, or the other accounts, down with it."""
    ib = _ValueIB({(ACCT, 1): (123.73, 5056.84)}, fail={9})
    prices, unpriced = live_quotes.fund_prices(
        ib, [(ACCT, _Contract("BROKEN", "FUND", "", 9)),
             (ACCT, _Contract("AFMBX", "FUND", "", 1))])
    assert prices == {"AFMBX": pytest.approx(40.87, abs=0.001)}
    assert unpriced == [f"{ACCT}/BROKEN"]


def test_a_fund_with_no_conid_is_never_subscribed_for():
    """No conId means IBKR has no instrument to value; it is named, not guessed at."""
    ib = _ValueIB({})
    prices, unpriced = live_quotes.fund_prices(
        ib, [(ACCT, _Contract("AFMBX", "FUND", "", 0))])
    assert (prices, unpriced, ib.subscribed) == ({}, [f"{ACCT}/AFMBX"], [])


def test_only_funds_ever_reach_this_price_source():
    """THE SCOPE GUARD. The broker reports a value for every ETF too — and it must NOT be used.
    An ETF has a live quote; if IBKR will not give us one, that ETF does not trade. The caller
    filters on live_quotes.is_fund, which is strict, so nothing else can be routed here."""
    sec_types = {"AFMBX": "FUND", "IVV": "STK", "BIV": "STK", "SOMEBOND": "BOND",
                 "MYSTERY": None}
    assert [s for s in sorted(sec_types) if live_quotes.is_fund(sec_types[s])] == ["AFMBX"]


# =========================================================================================
# B. THE CONTRACT — blank exchange gets FUNDSERV, and NOTHING else is touched
# =========================================================================================
def test_a_held_funds_blank_exchange_becomes_fundserv():
    """The broker's fund contract carries a real conId but no destination. FUNDSERV is the
    form verified to qualify, and the broker's own object is never mutated (it is shared
    across every account in the batch)."""
    broker = _fund_c("DODGX", exchange="")
    fixed = live_quotes.fund_contract(broker)
    assert fixed is not broker and broker.exchange == ""          # original untouched
    assert (fixed.symbol, fixed.secType, fixed.exchange, fixed.conId) == (
        "DODGX", "FUND", "FUNDSERV", 86797803)


def test_a_contract_that_already_names_an_exchange_is_returned_unchanged():
    """Identity, not a copy: we only fill in a destination the broker left blank."""
    already = _fund_c("DODGX", exchange="FUNDSERV")
    assert live_quotes.fund_contract(already) is already
    etf = _etf_c("IVV")
    assert live_quotes.fund_contract(etf) is etf


def test_the_one_chooser_hands_back_a_tradeable_fund_contract():
    """qualified_contracts is THE place a contract is chosen, so the FUNDSERV fill-in happens
    there and no rail can end up with a destination-less fund contract."""
    ib = _TxIB(_summary())
    picked, unqualified = live_quotes.qualified_contracts(
        ib, ["DODGX"], known={"DODGX": _fund_c("DODGX", exchange="")})
    assert unqualified == []
    assert picked["DODGX"].exchange == "FUNDSERV"
    assert picked["DODGX"].conId == 86797803


# =========================================================================================
# C. THE ORDER — market, no limit price, fraction intact
# =========================================================================================
def test_a_fund_order_is_a_market_order_with_no_limit_price():
    """A fund fills at tonight's NAV whatever you write on the ticket, so a limit is not a
    safer order — it is a malformed one."""
    o = order_router.build_mutual_fund_market("AFMBX", "SELL", 123.73, account=ACCT,
                                              order_ref="ref")
    assert o.orderType == "MKT"
    assert o.action == "SELL" and float(o.totalQuantity) == 123.73
    assert o.tif == "DAY" and o.account == ACCT and o.orderRef == "ref"
    assert o.transmit is False                       # placement flips it, behind the gate


def test_the_price_guard_still_refuses_a_bad_limit_on_every_other_builder():
    """The fund builder does not weaken the HARD PRICE GUARD anywhere else."""
    for bad in (None, float("nan"), 0.0, -1.0):
        with pytest.raises(ValueError):
            order_router.build_marketable_limit("IVV", "BUY", 1, bad)


# =========================================================================================
# D. THE LEG — the FULL fractional position, so the holding actually closes
# =========================================================================================
def _fund_exit_plan():
    """The live shape: the engine wants the fund gone, and its whole-share delta (-123) is the
    truncation of a 123.73-share holding."""
    return _plan(orders={"AFMBX": -123, "VTI": 10},
                 lines=[_line("AFMBX", HELD["AFMBX"]), _line("VTI", 0)])


def test_selling_a_fund_out_sells_the_whole_fractional_position():
    """The engine's whole-share delta would sell 123 and leave a 0.73-share stub — the account
    would never actually close the holding or be convertible to its model."""
    legs, _aliens, unpriceable = se.build_deploy_legs(
        _fund_exit_plan(), {}, {"AFMBX": NAV["AFMBX"], "VTI": 250.0}, conform=False,
        contracts={"AFMBX": _fund_c("AFMBX"), "VTI": _etf_c("VTI")})

    assert unpriceable == []
    by_symbol = {l.symbol: l for l in legs}
    assert by_symbol["AFMBX"].side == "SELL"
    assert by_symbol["AFMBX"].qty == pytest.approx(123.73)     # the FULL position
    assert by_symbol["AFMBX"].sec_type == "FUND"
    # and the ETF leg is untouched whole-share
    assert by_symbol["VTI"].qty == 10 and isinstance(by_symbol["VTI"].qty, int)


def test_a_fund_the_plan_only_partly_sells_keeps_its_whole_share_quantity():
    """The fraction is restored ONLY when the plan's intent is a complete exit. A partial
    fund sale (which this desk does not do) is not silently turned into a liquidation."""
    plan = _plan(orders={"AFMBX": -50}, lines=[_line("AFMBX", HELD["AFMBX"])])
    legs, _a, _u = se.build_deploy_legs(plan, {}, {"AFMBX": NAV["AFMBX"]}, conform=False,
                                        contracts={"AFMBX": _fund_c("AFMBX")})
    assert legs[0].qty == 50 and isinstance(legs[0].qty, int)


def test_a_fund_leg_is_priced_at_nav_and_sequenced_before_the_buys():
    """Sells before buys is unchanged, and the fund's notional is its real NAV value."""
    legs, _a, _u = se.build_deploy_legs(
        _fund_exit_plan(), {}, {"AFMBX": NAV["AFMBX"], "VTI": 250.0}, conform=False,
        contracts={"AFMBX": _fund_c("AFMBX"), "VTI": _etf_c("VTI")})
    assert [l.side for l in legs] == ["SELL", "BUY"]
    assert legs[0].notional == pytest.approx(123.73 * NAV["AFMBX"], abs=0.01)


def test_a_fund_with_no_price_is_unpriceable_not_free():
    """No NAV -> no leg, reported as unpriceable, which blocks. Never a $0 order."""
    legs, _a, unpriceable = se.build_deploy_legs(
        _fund_exit_plan(), {}, {"VTI": 250.0}, conform=False,
        contracts={"AFMBX": _fund_c("AFMBX"), "VTI": _etf_c("VTI")})
    assert [l.symbol for l in legs] == ["VTI"]
    assert unpriceable and unpriceable[0][0] == "AFMBX"


# =========================================================================================
# E. THE REGRESSION PIN — AN ALL-ETF ACCOUNT MUST BE BYTE-IDENTICAL
# =========================================================================================
def _all_etf_plan():
    return _plan(orders={"VTI": 10, "USFR": 5, "BIL": -3},
                 lines=[_line("VTI", 0), _line("USFR", 0), _line("BIL", 20)])


def _all_etf_prices():
    return {"VTI": 250.0, "USFR": 50.0, "BIL": 91.0}


def _leg_tuple(l):
    return (l.symbol, l.side, l.qty, type(l.qty).__name__, l.limit, l.notional, l.source)


def test_an_all_etf_account_plans_identically_with_and_without_the_contract_map():
    """THE PIN. Passing the broker's contracts (which is what the batch rail now always does)
    must not move a single number for an account with no funds in it — same legs, same sides,
    same INT quantities, same limits, same notionals, same order."""
    baseline, b_aliens, b_unpriceable = se.build_deploy_legs(
        _all_etf_plan(), {}, _all_etf_prices(), conform=False)
    withmap, w_aliens, w_unpriceable = se.build_deploy_legs(
        _all_etf_plan(), {}, _all_etf_prices(), conform=False,
        contracts={"VTI": _etf_c("VTI"), "USFR": _etf_c("USFR"), "BIL": _etf_c("BIL")})

    assert [_leg_tuple(l) for l in withmap] == [_leg_tuple(l) for l in baseline]
    assert all(isinstance(l.qty, int) for l in withmap)
    assert (w_aliens, w_unpriceable) == (b_aliens, b_unpriceable)


def test_an_all_etf_preview_is_unchanged_and_transmits_nothing(capsys):
    """End to end through execute_plan: no fund, no fund wording, no behaviour change."""
    req = _request(plan=_all_etf_plan(),
                   target=_target(weights={"VTI": 0.6, "USFR": 0.3, "BIL": 0.1}),
                   contracts={"VTI": _etf_c("VTI"), "USFR": _etf_c("USFR"),
                              "BIL": _etf_c("BIL")},
                   prices=_all_etf_prices(), armed=False)
    result = se.execute_plan(req, mode=se.MODE_PREVIEW, ib=None)
    out = capsys.readouterr().out

    assert result.status == se.STATUS_PREVIEW_ONLY
    assert result.reasons == ["not armed (default preview; pass --arm-i-understand to arm)"]
    assert "MUTUAL FUND" not in out.upper()
    assert "LIMIT ~" in out                       # the historic per-leg line, unchanged
    assert "MARKET" not in out


def test_an_all_etf_run_excludes_no_cash():
    """The settlement gate is arithmetically inert for an account with no funds."""
    legs = [SimpleNamespace(symbol="BIL", side="SELL", qty=3, limit=91.0, notional=273.0,
                            source="plan", sec_type="STK")]
    results = [{"symbol": "BIL", "side": "SELL", "requested": 3, "filled": 3.0,
                "status": "Filled", "reprices": 0, "skipped": False, "reason": ""}]
    assert se._unsettled_fund_proceeds(results, legs, {}) == 0.0


# =========================================================================================
# F. THE TRANSMIT PATH — market order, left working, never cancelled
# =========================================================================================
def _fund_leg(symbol="AFMBX", qty=123.73, limit=NAV["AFMBX"]):
    return SimpleNamespace(symbol=symbol, side="SELL", qty=qty, limit=limit,
                           notional=qty * limit, source="plan", sec_type="FUND")


def test_a_fund_sell_goes_out_as_a_market_order_on_the_brokers_fund_contract(_armed):
    ib = _TxIB(_summary(), no_fill=["AFMBX"])
    results = se._transmit_phase(
        ib, [_fund_leg()], account=ACCT, as_of=pd.Timestamp("2026-09-01"), run_id="R1",
        phase_label="SELL", quotes={}, prices={"AFMBX": NAV["AFMBX"]},
        contracts={"AFMBX": _fund_c("AFMBX")})

    assert len(ib.placed) == 1
    contract, order = ib.placed[0]
    assert (contract.secType, contract.exchange) == ("FUND", "FUNDSERV")
    assert order.orderType == "MKT"
    assert float(order.totalQuantity) == pytest.approx(123.73)   # fraction intact
    assert results[0]["sec_type"] == "FUND" and results[0]["skipped"] is False


def test_a_working_fund_order_is_never_cancelled_at_the_phase_timeout(_armed):
    """THE ONE THAT WOULD HAVE SILENTLY UNDONE EVERYTHING. The phase waits 90s for terminal
    state and CANCELS whatever is left. A fund order cannot fill inside that window by
    construction — it fills after the close — so waiting on it would burn the timeout and
    then cancel the very order we came to place. Fund legs are excluded from that wait."""
    ib = _TxIB(_summary(), no_fill=["AFMBX"])
    results = se._transmit_phase(
        ib, [_fund_leg()], account=ACCT, as_of=pd.Timestamp("2026-09-01"), run_id="R1",
        phase_label="SELL", quotes={}, prices={"AFMBX": NAV["AFMBX"]},
        contracts={"AFMBX": _fund_c("AFMBX")})

    assert ib.cancelled == []                      # left WORKING, on purpose
    assert results[0]["filled"] == 0.0
    assert "tonight's NAV" in results[0]["reason"]


def test_an_etf_leg_in_the_same_phase_is_transmitted_exactly_as_before(_armed):
    """Isolation: a fund in the phase changes nothing about the ETF legs beside it."""
    ib = _TxIB(_summary(), no_fill=["AFMBX"])
    etf = SimpleNamespace(symbol="IVV", side="SELL", qty=5, limit=600.0, notional=3000.0,
                          source="plan", sec_type="STK")
    results = se._transmit_phase(
        ib, [_fund_leg(), etf], account=ACCT, as_of=pd.Timestamp("2026-09-01"), run_id="R1",
        phase_label="SELL", quotes={}, prices={"AFMBX": NAV["AFMBX"], "IVV": 600.0},
        contracts={"AFMBX": _fund_c("AFMBX"), "IVV": _etf_c("IVV")})

    by_symbol = {sym: o for (c, o) in ib.placed for sym in [c.symbol]}
    assert by_symbol["IVV"].orderType == "LMT" and by_symbol["IVV"].lmtPrice == 600.0
    assert by_symbol["AFMBX"].orderType == "MKT"
    assert {r["symbol"]: r["filled"] for r in results} == {"AFMBX": 0.0, "IVV": 5.0}


# =========================================================================================
# G. THE SETTLEMENT RULE — fund proceeds can NEVER fund a same-run buy
# =========================================================================================
def test_working_fund_proceeds_are_zero_so_etf_cash_still_deploys_in_full():
    """Not over-conservative: an unfilled fund order subtracts nothing, so the account still
    deploys the ETF proceeds it really does have. That is the intended shape — this run sells
    everything and deploys the ETF cash; a later run deploys the fund cash."""
    legs = [_fund_leg(), SimpleNamespace(symbol="IVV", side="SELL", qty=5, limit=600.0,
                                         notional=3000.0, source="plan", sec_type="STK")]
    results = [{"symbol": "AFMBX", "filled": 0.0}, {"symbol": "IVV", "filled": 5.0}]
    assert se._unsettled_fund_proceeds(results, legs, {}) == 0.0


def test_a_fund_that_did_fill_has_its_proceeds_subtracted():
    """The second, independent wall. If a run happens to see a real fund fill, that money is
    unsettled and unspendable, so it is removed from the budget even though the broker's cash
    figure has picked it up."""
    results = [{"symbol": "AFMBX", "filled": 123.73}]
    assert se._unsettled_fund_proceeds(results, [_fund_leg()], {}) == pytest.approx(
        123.73 * NAV["AFMBX"])


def test_an_armed_run_never_buys_against_fund_proceeds(monkeypatch):
    """THE LOAD-BEARING TEST, end to end through the armed two-phase transmit.

    Setup: the account has $1,000 of its own cash and sells a $5,056.84 mutual fund. The
    broker's TotalCashValue comes back as $6,056.84 — i.e. it HAS counted the fund proceeds.
    The plan wants to buy 10 VTI at $250 = $2,500.

    If fund proceeds were treated as spendable, the run would buy ~$5,996 of VTI against money
    that has not arrived — the exact shape of the 2026-07-28 negative-balance incident. The
    run must instead size buys to the $1,000 that is really there: 3 shares, not 23."""
    monkeypatch.setattr(se, "_probe_gateway_readonly", lambda ib, **k: False)
    plan = _fund_exit_plan()
    req = _request(plan=plan, target=_target(weights={"VTI": 1.0}),
                   contracts={"AFMBX": _fund_c("AFMBX"), "VTI": _etf_c("VTI")},
                   prices={"AFMBX": NAV["AFMBX"], "VTI": 250.0}, armed=True,
                   total_cash="6056.84")
    ib = _TxIB(_summary(total_cash="6056.84"))    # the fund FILLS in this fake — worst case
    result = se.execute_plan(req, mode=se.MODE_ARMED, ib=ib)

    buys = [o for (_c, o) in ib.placed if o.action == "BUY"]
    assert buys, "the ETF buy should still transmit — just sized to real cash"
    spent = sum(float(o.totalQuantity) * float(o.lmtPrice) for o in buys)
    own_cash_budget = 1000.0 * (1.0 - se.CASH_SAFETY_BUFFER_PCT)
    assert spent <= own_cash_budget                       # NEVER the fund's money
    assert float(buys[0].totalQuantity) == 3              # not 23
    assert result.realized_cash == pytest.approx(1000.0, abs=0.05)
    assert config.DRY_RUN is True and config.READONLY is True


def test_the_preview_says_in_plain_english_that_fund_money_is_excluded(capsys):
    """A human has to be able to read the preview and understand, without being a programmer,
    that the account is only partly conformed today and why, and roughly how much lands
    later."""
    req = _request(plan=_fund_exit_plan(), target=_target(weights={"VTI": 1.0}),
                   contracts={"AFMBX": _fund_c("AFMBX"), "VTI": _etf_c("VTI")},
                   prices={"AFMBX": NAV["AFMBX"], "VTI": 250.0}, armed=False)
    se.execute_plan(req, mode=se.MODE_PREVIEW, ib=None)
    out = capsys.readouterr().out

    assert "EXCLUDED FROM THIS RUN'S BUYING POWER" in out
    assert "do NOT settle the same day" in out
    assert "5,056.85" in out                       # roughly how much lands later
    assert "MARKET at tonight's NAV" in out        # no limit price is claimed
    assert "123.7300" in out                       # the FULL fractional position
    assert "Nothing was transmitted." in out


def test_nothing_is_automatic_the_arm_gate_still_governs_a_fund_run():
    """Everything above stays behind the existing arm gate. A fund run previews and stops."""
    req = _request(plan=_fund_exit_plan(), target=_target(weights={"VTI": 1.0}),
                   contracts={"AFMBX": _fund_c("AFMBX"), "VTI": _etf_c("VTI")},
                   prices={"AFMBX": NAV["AFMBX"], "VTI": 250.0}, armed=False)
    result = se.execute_plan(req, mode=se.MODE_PREVIEW, ib=None)
    assert result.status == se.STATUS_PREVIEW_ONLY
    assert "not armed" in result.reasons[0]
    assert result.sell_results == [] and result.buy_results == []
