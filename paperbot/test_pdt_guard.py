"""
test_pdt_guard.py — offline unit tests for the PURE per-account PATTERN-DAY-TRADER read
(pdt_guard) and for its wiring into safe_execute.execute_plan's pre-transmit gate.

ZERO broker, ZERO network. The gate is exercised with the same transmit-fake shape
test_safe_execute uses; nothing reaches a wire.

WHAT IS PINNED HERE
  1. The tag semantics carried over from the block rail: -1 CLEARS, n>0 CLEARS, 0 BLOCKS.
  2. The ABSENT-TAG design decision, BOTH DIRECTIONS:
       - absent + an otherwise complete summary (a witness tag present) -> CLEARS,
       - absent + nothing at all (empty/failed read)                    -> BLOCKS,
       - absent + a DayTradesRemainingT+n sibling present               -> BLOCKS.
  3. Present-but-unparseable BLOCKS (distinct from absent — that split is the whole point).
  4. Every refusal reason NAMES the account and the observed tag value.
  5. execute_plan on the ARMED lane refuses a PDT-flagged account and transmits nothing, and
     still transmits for a cleared one.

Run:
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe -m pytest test_pdt_guard.py -q
"""
from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

import config
import pdt_guard as pg
import safe_execute as se
import strategy_target

ACCT = "U14438624"


@pytest.fixture(autouse=True)
def _no_config_flag_leak():
    """Mirrors test_safe_execute's leak guard: the armed-lane tests below flip
    config.DRY_RUN/READONLY inside armed_session, and that flip must never outlive the test."""
    prev_dry_run, prev_readonly = config.DRY_RUN, config.READONLY
    assert prev_dry_run is True and prev_readonly is True
    try:
        yield
    finally:
        config.DRY_RUN, config.READONLY = prev_dry_run, prev_readonly


def _row(tag, value, account=ACCT):
    return SimpleNamespace(account=account, tag=tag, value=value)


def _rows(dtr=None, *, witness=True, siblings=False, account=ACCT):
    """One account's accountSummary rows, in the shape ib_async returns.

    `dtr=None` means the DayTradesRemaining tag is ABSENT (exactly what U23414989 /
    U23415099 / U27295881 return live). `witness=False` strips every witness tag too, i.e.
    an empty/failed read. `siblings=True` adds the forward-dated T+n tags."""
    out = []
    if witness:
        out += [_row("AccountType", "INDIVIDUAL", account),
                _row("NetLiquidation", "100000", account),
                _row("TotalCashValue", "100000", account),
                _row("BuyingPower", "100000", account)]
    if dtr is not None:
        out.append(_row(pg.PDT_TAG, str(dtr), account))
    if siblings:
        out += [_row(t, "-1", account) for t in pg.PDT_SIBLING_TAGS]
    return out


# ========================================================================================
# 1. THE TAG SEMANTICS — preserved EXACTLY from the block rail.
# ========================================================================================
def test_minus_one_clears():
    v = pg.pdt_verdict(ACCT, _rows(-1))
    assert v.ok is True
    assert v.code == pg.CLEAR_UNLIMITED
    assert "-1" in v.reason and ACCT in v.reason


def test_positive_value_clears():
    v = pg.pdt_verdict(ACCT, _rows(3))
    assert v.ok is True
    assert v.code == pg.CLEAR_REMAINING
    assert "'3'" in v.reason and ACCT in v.reason


def test_zero_blocks():
    v = pg.pdt_verdict(ACCT, _rows(0))
    assert v.ok is False
    assert v.code == pg.BLOCK_NO_DAY_TRADES


def test_large_positive_clears():
    assert pg.pdt_verdict(ACCT, _rows(4)).ok is True


def test_dict_shape_is_accepted_too():
    """filter_account_summary passes a {tag: value} dict through unchanged, so the reader
    must handle that shape as well as the row-object list."""
    assert pg.pdt_verdict(ACCT, {pg.PDT_TAG: "-1", "NetLiquidation": "1"}).ok is True
    assert pg.pdt_verdict(ACCT, {pg.PDT_TAG: "0", "NetLiquidation": "1"}).ok is False


def test_day_trades_remaining_parses_like_the_block_rail():
    assert pg.day_trades_remaining(_rows(-1)) == -1
    assert pg.day_trades_remaining(_rows(0)) == 0
    assert pg.day_trades_remaining(_rows(7)) == 7
    assert pg.day_trades_remaining(_rows()) is None          # absent
    assert pg.day_trades_remaining(_rows("")) is None        # blank
    assert pg.day_trades_remaining(_rows("junk")) is None    # garbage


# ========================================================================================
# 2. THE ABSENT-TAG DECISION — tested EXPLICITLY, BOTH DIRECTIONS.
#
# Measured live on 4003 (2026-09-01): U23414989 / U23415099 / U27295881 each return 22
# accountSummary rows with NO DayTradesRemaining tag, while U27305011 returns '3' and
# U14438624 returns '-1'. A naive "missing = block" rule would refuse three real, tradeable
# accounts, two of them queued for an imminent first deployment.
# ========================================================================================
def test_absent_tag_in_a_complete_summary_CLEARS():
    v = pg.pdt_verdict("U23414989", _rows(None, account="U23414989"))
    assert v.ok is True
    assert v.code == pg.CLEAR_NOT_A_DAY_TRADING_ACCOUNT
    # Never silent, even on a clearance: the account and the observed value are both named.
    assert "U23414989" in v.reason and "<absent>" in v.reason


def test_absent_tag_with_NO_witness_tag_BLOCKS():
    """The other direction: an empty/thin/failed read tells us nothing, so it fails closed.
    This is what stops the clearance rule from degrading into a blanket fail-open."""
    v = pg.pdt_verdict(ACCT, [])
    assert v.ok is False
    assert v.code == pg.BLOCK_UNREADABLE_SUMMARY
    assert ACCT in v.reason and "<absent>" in v.reason


def test_absent_tag_with_only_irrelevant_tags_BLOCKS():
    v = pg.pdt_verdict(ACCT, [_row("Cushion", "1.0"), _row("Leverage", "0.0")])
    assert v.ok is False and v.code == pg.BLOCK_UNREADABLE_SUMMARY


def test_absent_base_tag_but_sibling_present_BLOCKS():
    """A partial answer to the PDT question is not the shape a non-day-trading account
    returns — that is anomalous and fails closed."""
    v = pg.pdt_verdict(ACCT, _rows(None, siblings=True))
    assert v.ok is False
    assert v.code == pg.BLOCK_PARTIAL_PDT_FAMILY
    assert ACCT in v.reason and "<absent>" in v.reason


def test_each_witness_tag_alone_is_enough_to_clear_an_absent_tag():
    for tag in pg.WITNESS_TAGS:
        v = pg.pdt_verdict(ACCT, [_row(tag, "1")])
        assert v.ok is True, tag
        assert v.code == pg.CLEAR_NOT_A_DAY_TRADING_ACCOUNT, tag


def test_the_three_measured_tagless_accounts_all_clear():
    """The concrete regression this rule exists for."""
    for acct in ("U23414989", "U23415099", "U27295881"):
        assert pg.pdt_verdict(acct, _rows(None, account=acct)).ok is True, acct


def test_the_two_measured_tagged_accounts_clear_on_their_values():
    assert pg.pdt_verdict("U27305011", _rows(3, account="U27305011")).ok is True
    assert pg.pdt_verdict("U14438624", _rows(-1, account="U14438624")).ok is True


# ========================================================================================
# 3. PRESENT-BUT-UNPARSEABLE — the broker answered and we could not read it. Closed.
# ========================================================================================
def test_present_but_blank_BLOCKS_not_treated_as_absent():
    v = pg.pdt_verdict(ACCT, _rows(""))
    assert v.ok is False
    assert v.code == pg.BLOCK_UNPARSEABLE
    assert ACCT in v.reason and "''" in v.reason


def test_present_but_garbage_BLOCKS():
    v = pg.pdt_verdict(ACCT, _rows("junk"))
    assert v.ok is False and v.code == pg.BLOCK_UNPARSEABLE
    assert "'junk'" in v.reason


def test_negative_other_than_minus_one_BLOCKS():
    v = pg.pdt_verdict(ACCT, _rows(-2))
    assert v.ok is False and v.code == pg.BLOCK_UNPARSEABLE
    assert "'-2'" in v.reason


def test_absent_and_unparseable_are_DIFFERENT_codes():
    """The split that makes the whole design work."""
    assert (pg.pdt_verdict(ACCT, _rows(None)).code
            != pg.pdt_verdict(ACCT, _rows("")).code)


# ========================================================================================
# 4. EVERY REFUSAL NAMES THE ACCOUNT AND THE OBSERVED VALUE.
# ========================================================================================
def test_blocked_reason_names_the_account_and_the_value():
    ok, reason = pg.pdt_account_ok("U5721712", _rows(0, account="U5721712"))
    assert ok is False
    assert "U5721712" in reason
    assert "DayTradesRemaining='0'" in reason
    assert "REFUSING" in reason


def test_every_block_path_names_account_and_value():
    cases = [_rows(0), _rows(""), _rows("junk"), _rows(-2), [], _rows(None, siblings=True)]
    for rows in cases:
        v = pg.pdt_verdict(ACCT, rows)
        assert v.ok is False
        assert ACCT in v.reason
        assert pg.PDT_TAG in v.reason
        assert v.reason.strip(), "a refusal must never be silent"


def test_pdt_account_ok_returns_a_reason_on_a_clearance_too():
    ok, reason = pg.pdt_account_ok(ACCT, _rows(-1))
    assert ok is True and reason and ACCT in reason


# ========================================================================================
# 5. THE WIRING — execute_plan's ARMED pre-transmit gate.
# ========================================================================================
def _target(weights=None, prices=None):
    """Same shape test_safe_execute uses."""
    weights = weights or {"VTI": 0.5, "RSP": 0.3, "USFR": 0.2}
    prices = prices or {"VTI": 250.0, "RSP": 180.0, "USFR": 50.0, "BIL": 91.0}
    return strategy_target.Target(
        weights=pd.Series(weights), prices=pd.Series(prices),
        as_of=pd.Timestamp("2026-07-28"), price_date=pd.Timestamp("2026-07-28"),
        version="Growth")


def _plan(orders):
    return SimpleNamespace(account=ACCT, version="Growth", net_liq=100_000.0, reserve=0.0,
                           investable=98_500.0, lines=[], needs_rebalance=True,
                           orders=orders, alien_lines=[])


def _request(summary, *, orders=None):
    tgt = _target()
    return se.ExecutionRequest(
        account=ACCT, strategy_version="Growth", plan=_plan(orders or {"VTI": 10}),
        target=tgt, quotes={}, prices=dict(tgt.prices), allowed_accounts=[ACCT],
        caps=se.ExecutionCaps(), conform=True, run_id=None, net_liq=100_000.0,
        summary=summary, armed=True, kill=False)


class _TxFakeIB:
    """Records placed orders; reaches no wire (mirrors test_safe_execute's transmit fake)."""
    def __init__(self, summary_rows):
        self._summary = summary_rows
        self.placed = []

    def accountSummary(self): return self._summary
    def positions(self): return []
    def reqAllOpenOrders(self): return []
    def qualifyContracts(self, *a, **k): return list(a)
    def sleep(self, *a, **k): return None
    def placeOrder(self, contract, order):
        self.placed.append((contract, order))
        return SimpleNamespace(order=order, contract=contract,
                               orderStatus=SimpleNamespace(status="Filled", filled=0,
                                                           avgFillPrice=0.0),
                               log=[], fills=[])
    def cancelOrder(self, *a, **k): return None


def test_execute_plan_armed_SUPPRESSES_THE_WHOLE_ACCOUNT_including_sells(monkeypatch,
                                                                        capsys):
    """MEASURED 2026-09-03. IBKR restricts a flagged PDT account to LIQUIDATING TRADES ONLY:
    across five such accounts every one of 60 BUY legs came back Inactive and every SELL
    filled. So the sells are withheld too - selling an account that cannot buy strips holdings
    and parks the cash, leaving it further from its model than doing nothing. Half a rebalance
    is worse than none."""
    monkeypatch.setattr(se, "_probe_gateway_readonly", lambda ib, **k: False)
    rows = _rows(0)
    ib = _TxFakeIB(rows)
    res = se.execute_plan(_request(rows), mode=se.MODE_ARMED, ib=ib)
    assert res.status == se.STATUS_BLOCKED
    assert ib.placed == [], "NOTHING may transmit - not the buys and not the sells"
    hit = [r for r in res.reasons if pg.PDT_TAG in r]
    assert hit, f"no PDT reason in {res.reasons}"
    assert ACCT in hit[0] and "DayTradesRemaining='0'" in hit[0]
    assert "LIQUIDATING TRADES ONLY" in hit[0]
    assert "SELLS are withheld" in hit[0]
    # surfaced as a condition to fix as well as a refusal
    assert any(pg.PDT_TAG in w for w in res.warnings)
    out = capsys.readouterr().out
    assert "PDT pre-flight" in out
    assert "PDT BLOCK - transmitting NOTHING" in out


def test_execute_plan_armed_BLOCKS_on_an_unreadable_summary(monkeypatch):
    """An account whose summary carries no witness tag at all fails closed at the gate."""
    monkeypatch.setattr(se, "_probe_gateway_readonly", lambda ib, **k: False)
    rows = [_row("Cushion", "1.0")]
    ib = _TxFakeIB(rows)
    res = se.execute_plan(_request(rows), mode=se.MODE_ARMED, ib=ib)
    assert res.status == se.STATUS_BLOCKED
    assert ib.placed == []
    assert any(pg.PDT_TAG in r for r in res.reasons)


def test_execute_plan_armed_CLEARS_an_absent_tag_account_and_transmits(monkeypatch, capsys):
    """The other direction of the absent-tag design, proven at the gate: a real tag-less
    account is NOT refused — it reaches the transmit path."""
    monkeypatch.setattr(se, "_probe_gateway_readonly", lambda ib, **k: False)
    rows = _rows(None)          # complete summary, no DayTradesRemaining tag
    ib = _TxFakeIB(rows)
    res = se.execute_plan(_request(rows, orders={"VTI": -5}), mode=se.MODE_ARMED, ib=ib)
    assert res.status != se.STATUS_BLOCKED
    assert not any(pg.PDT_TAG in r for r in res.reasons), res.reasons
    out = capsys.readouterr().out
    assert pg.CLEAR_NOT_A_DAY_TRADING_ACCOUNT in out


def test_execute_plan_armed_CLEARS_minus_one(monkeypatch):
    monkeypatch.setattr(se, "_probe_gateway_readonly", lambda ib, **k: False)
    rows = _rows(-1)
    ib = _TxFakeIB(rows)
    res = se.execute_plan(_request(rows, orders={"VTI": -5}), mode=se.MODE_ARMED, ib=ib)
    assert res.status != se.STATUS_BLOCKED
    assert not any(pg.PDT_TAG in r for r in res.reasons), res.reasons


def test_execute_plan_PREVIEW_does_not_run_the_pdt_gate(capsys):
    """The gate is a connection-lane check, like buying power and margin: PREVIEW must not
    grow a PDT reason (nothing can transmit there anyway)."""
    rows = _rows(0)
    res = se.execute_plan(_request(rows), mode=se.MODE_PREVIEW)
    assert not any(pg.PDT_TAG in r for r in res.reasons), res.reasons
    assert "PDT pre-flight" not in capsys.readouterr().out
