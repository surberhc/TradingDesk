"""
test_live_fa_block_execute_phases.py — offline unit tests for the FA-GROUP BLOCK executor's
TWO-PHASE CASH GATE (conductor #64 applied to the block lane). NO broker, NO gateway, NO real
orders — a fake IB records every replaceFA / placeOrder / accountSummary read.

Companion to test_live_fa_block_execute.py (which covers the arm gate, the account wall and the
group-XML write). This file covers ONLY the phasing + cash gate:

  (a) SELLS ARE ALWAYS PLACED BEFORE BUYS, whatever order the engine emitted the routes in.
  (b) A BUY block is RE-SIZED DOWN when an account's realized cash is under plan (whole-share
      floor, never rounded up), and BOTH the block quantity and the group's ContractsOrShares
      split are recomputed.
  (c) An account whose realized TotalCashValue is missing/unparseable contributes ZERO to the
      buy phase (FAIL CLOSED) and is reported.
  (d) NO NETTING: one account's surplus realized cash can never fund another's buy.
  (e) An UNKNOWN route side FAILS CLOSED — the WHOLE run is refused before the backup, before
      any replaceFA and before any order.
  (f) The uninvested-proceeds exception report fires when an account sold but did not redeploy,
      and stays quiet when every account redeployed as intended.
  (g) PREVIEW still transmits nothing, writes no FA config, and does NO broker cash read — while
      still printing the phase plan.

Run:
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe -m pytest test_live_fa_block_execute_phases.py -q
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import config
import live_fa_block_execute as lx
import order_router
import rebalance_execute
from rebalance_engine import RoutePlan
from test_live_fa_block_execute import LIVE_GROUPS_XML

A1 = "DU8922143"
A2 = "DU8922144"

# The two symbols the phase tests trade. SPY is the SELL leg (limit 100), IVV the BUY leg
# (limit 50) — distinct prices so a re-size is unambiguous in share terms.
SELL_PX = 100.0
BUY_PX = 50.0


# ========================================================================================
# FAKES — no broker, no network.
# ========================================================================================
class _Row:
    """One accountSummary row (ib_async AccountValue shape: .account/.tag/.value)."""
    def __init__(self, account, tag, value):
        self.account, self.tag, self.value = account, tag, str(value)


class _FakeIB:
    """Records replaceFA writes, placed orders (in ORDER), accountSummary reads and cancels.

    `cash` maps account -> TotalCashValue; a value of None omits the tag entirely (the
    missing/unparseable case). `fill_frac` maps symbol -> the fraction of the block that fills.
    """
    def __init__(self, cash=None, fill_frac=None, status="Filled"):
        self.replace_fa_calls = 0
        self.replace_fa_splits: list = []
        self.placed: list = []            # (contract, order) in placement ORDER
        self.actions: list = []           # ("SELL"/"BUY", symbol) in placement ORDER
        self.account_summary_reads = 0
        self.cancelled: list = []
        self.slept = 0.0
        self._cash = dict(cash or {})
        self._fill_frac = dict(fill_frac or {})
        self._status = status

    # --- FA config -------------------------------------------------------------
    def requestFA(self, *_a, **_k):
        return LIVE_GROUPS_XML

    def replaceFA(self, _kind, xml):
        self.replace_fa_calls += 1
        self.replace_fa_splits.append(xml)

    # --- read-only reads -------------------------------------------------------
    def accountSummary(self, *_a, **_k):
        self.account_summary_reads += 1
        rows = []
        for acct, val in sorted(self._cash.items()):
            rows.append(_Row(acct, "NetLiquidation", 1_000_000.0))
            if val is not None:
                rows.append(_Row(acct, "TotalCashValue", val))
        return rows

    def reqAllOpenOrders(self):
        return []

    def openTrades(self):
        return []

    def reqExecutions(self, *_a, **_k):
        return []

    def qualifyContracts(self, *c):
        return c

    # --- orders ----------------------------------------------------------------
    def placeOrder(self, contract, order):
        self.placed.append((contract, order))
        self.actions.append((order.action, contract.symbol))
        qty = float(order.totalQuantity)
        filled = qty * self._fill_frac.get(contract.symbol, 1.0)
        return SimpleNamespace(
            contract=contract,
            orderStatus=SimpleNamespace(status=self._status, filled=filled,
                                        remaining=qty - filled, avgFillPrice=order.lmtPrice),
            isDone=lambda: True)

    def cancelOrder(self, order):
        self.cancelled.append(order)

    def sleep(self, secs=0.0):
        self.slept += float(secs or 0.0)


def _block(symbol, side, split, group="tier_balanced", version="Balanced"):
    return RoutePlan("fa_block", version, symbol, side, sum(split.values()), group, "",
                     None, dict(split))


def _account_inputs():
    return [
        {"account": A1, "version": "Balanced", "net_liq": 1_000_000.0, "positions": {},
         "prices": {"SPY": SELL_PX, "IVV": BUY_PX, "VTI": BUY_PX, "QQQ": SELL_PX}},
        {"account": A2, "version": "Balanced", "net_liq": 1_000_000.0, "positions": {},
         "prices": {"SPY": SELL_PX, "IVV": BUY_PX, "VTI": BUY_PX, "QQQ": SELL_PX}},
    ]


def _targets():
    return {"Balanced": SimpleNamespace(as_of="2026-08-19", prices={}, weights={})}


def _target_gateway():
    return lx.TargetGateway(
        name="PAPER-TEST", host="127.0.0.1", port=4002,
        clientid_consumer="paperbot_live_fa_block_exec",
        master_account="DF8922141", pin_account=A1,
        enrollment={A1: "Balanced", A2: "Balanced"})


@pytest.fixture
def armed_env(monkeypatch):
    """Isolate the loop mechanics: code gate open, margin clear, backup a no-op, dedup FRESH,
    no live quotes (so the limit falls back to the account_inputs reference price)."""
    monkeypatch.setattr(config, "READONLY", False)
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(lx, "margin_preflight_over_split", lambda *a, **k: (True, ""))
    monkeypatch.setattr(rebalance_execute, "backup_fa_groups", lambda ib: "fake_backup.xml")
    monkeypatch.setattr(order_router, "already_present",
                        lambda *a, **k: order_router.LegState.FRESH)
    monkeypatch.setattr(lx, "_quotes_cache", {})


def _run(ib, routes, *, permit):
    return lx.execute_fa_block_routes(ib, routes, _account_inputs(), _targets(),
                                      _target_gateway(), permit=permit, summaries={})


# ========================================================================================
# (a) SELLS ALWAYS FIRST
# ========================================================================================
def test_sells_are_placed_before_buys_regardless_of_route_order(armed_env):
    ib = _FakeIB(cash={A1: 1_000_000.0, A2: 1_000_000.0})
    # The engine emits the BUY first (routes are sorted by symbol) — the phase gate must still
    # place the SELL first.
    routes = [_block("IVV", "BUY", {A1: 20, A2: 20}),
              _block("SPY", "SELL", {A1: 10, A2: 10})]
    result = _run(ib, routes, permit=True)

    assert [a for a, _s in ib.actions] == ["SELL", "BUY"]
    assert ib.actions[0] == ("SELL", "SPY")
    assert ib.actions[1] == ("BUY", "IVV")
    assert result["n_sell_blocks"] == 1 and result["n_buy_blocks"] == 1
    # The realized-cash re-read happened BETWEEN the phases (not before the sells).
    assert ib.account_summary_reads == 1
    assert ib.slept >= lx.CASH_SETTLE_SEC


def test_multiple_sells_all_precede_every_buy(armed_env):
    ib = _FakeIB(cash={A1: 1_000_000.0, A2: 1_000_000.0})
    routes = [_block("IVV", "BUY", {A1: 4, A2: 4}),
              _block("SPY", "SELL", {A1: 3, A2: 3}),
              _block("VTI", "BUY", {A1: 2, A2: 2}),
              _block("QQQ", "SELL", {A1: 1, A2: 1})]
    _run(ib, routes, permit=True)
    sides = [a for a, _s in ib.actions]
    assert sides == ["SELL", "SELL", "BUY", "BUY"], sides


# ========================================================================================
# (b) BUY RE-SIZED DOWN to realized cash — quantity AND group split recomputed
# ========================================================================================
def test_resize_scales_buy_block_down_to_realized_cash():
    buy = _block("IVV", "BUY", {A1: 20, A2: 20})            # 20 x $50 = $1,000 planned each
    resized, dropped, detail = lx.resize_buy_routes_to_realized_cash(
        [(buy, BUY_PX)], {A1: 1_000.0, A2: 2_000.0})

    assert dropped == []
    (new_route, limit), = resized
    assert limit == BUY_PX
    # A1's budget is 1,000 * (1 - 1%) = 990 -> floor(20 * 0.99) = 19 shares ($950 <= $990).
    # A2 has ample cash and keeps its full 20. NOTHING is rounded up.
    assert new_route.per_account_split == {A1: 19, A2: 20}
    assert new_route.total_qty == 39
    # The engine's original route is untouched (dataclasses.replace, not mutation).
    assert buy.per_account_split == {A1: 20, A2: 20} and buy.total_qty == 40
    assert detail[A1]["sized_notional"] <= detail[A1]["budget"] + 1e-6
    assert detail[A1]["adjustments"] == [{"symbol": "IVV", "orig_qty": 20, "new_qty": 19}]


def test_armed_run_writes_the_resized_split_and_places_the_resized_block(armed_env):
    # A1 realizes only $1,000 (short); A2 realizes plenty.
    ib = _FakeIB(cash={A1: 1_000.0, A2: 1_000_000.0})
    routes = [_block("SPY", "SELL", {A1: 10, A2: 10}),
              _block("IVV", "BUY", {A1: 20, A2: 20})]
    result = _run(ib, routes, permit=True)

    buy_order = next(o for _c, o in ib.placed if o.action == "BUY")
    assert float(buy_order.totalQuantity) == 39           # 19 + 20, re-sized DOWN from 40
    # The replaceFA that preceded the buy carried the RE-SIZED split, not the plan's.
    assert ib.replace_fa_calls == 2                        # one per block, in lockstep
    assert "<amount>19</amount>" in ib.replace_fa_splits[-1]
    assert result["buy_results"][0]["split"] == {A1: 19, A2: 20}


def test_resize_never_rounds_up():
    buy = _block("IVV", "BUY", {A1: 1}, group="tier_balanced")
    # $49.99 of cash cannot buy a $50 share (and the 1% buffer makes it tighter still).
    resized, dropped, _detail = lx.resize_buy_routes_to_realized_cash(
        [(buy, BUY_PX)], {A1: 49.99})
    assert resized == []
    assert [r.symbol for r, _l in dropped] == ["IVV"]


def test_block_dropped_when_every_account_scales_to_zero(armed_env):
    ib = _FakeIB(cash={A1: 0.0, A2: 0.0})
    routes = [_block("SPY", "SELL", {A1: 10, A2: 10}),
              _block("IVV", "BUY", {A1: 20, A2: 20})]
    result = _run(ib, routes, permit=True)
    assert result["dropped_buy_blocks"] == ["IVV"]
    assert [a for a, _s in ib.actions] == ["SELL"]          # no buy block ever placed
    assert ib.replace_fa_calls == 1                         # only the sell block's group write


# ========================================================================================
# (c) MISSING / UNREADABLE CASH -> contributes ZERO, reported
# ========================================================================================
def test_missing_cash_contributes_zero_and_is_reported():
    buy = _block("IVV", "BUY", {A1: 20, A2: 20})
    resized, _dropped, detail = lx.resize_buy_routes_to_realized_cash(
        [(buy, BUY_PX)], {A1: None, A2: 2_000.0})          # A1's TotalCashValue unreadable
    (new_route, _limit), = resized
    assert A1 not in new_route.per_account_split            # ZERO — fail closed
    assert new_route.per_account_split == {A2: 20}
    assert detail[A1]["cash_read_ok"] is False
    assert detail[A1]["realized_cash"] is None
    assert detail[A1]["sized_notional"] == 0.0

    report = lx.uninvested_proceeds_report(
        {A1: 1_000.0, A2: 1_000.0}, detail, {A2: 1_000.0})
    a1_rows = [r for r in report if r["account"] == A1]
    assert len(a1_rows) == 1
    assert a1_rows[0]["reason"] == "CASH_UNREADABLE"
    assert a1_rows[0]["dollars_uninvested"] == pytest.approx(1_000.0)
    assert [r["account"] for r in report] == [A1]           # A2 redeployed fully -> not flagged


def test_read_realized_cash_returns_none_for_missing_tag():
    ib = _FakeIB(cash={A1: 1_234.5, A2: None})
    assert lx.read_realized_cash(ib, [A1, A2]) == {A1: 1_234.5, A2: None}


def test_read_realized_cash_fails_every_account_closed_on_read_error():
    class _Boom(_FakeIB):
        def accountSummary(self, *_a, **_k):
            raise RuntimeError("gateway went away")
    assert lx.read_realized_cash(_Boom(), [A1, A2]) == {A1: None, A2: None}


def test_armed_run_with_unreadable_cash_places_no_buy_for_that_account(armed_env):
    ib = _FakeIB(cash={A1: None, A2: 1_000_000.0})
    routes = [_block("SPY", "SELL", {A1: 10, A2: 10}),
              _block("IVV", "BUY", {A1: 20, A2: 20})]
    result = _run(ib, routes, permit=True)

    assert result["realized_cash"][A1] is None
    buy_order = next(o for _c, o in ib.placed if o.action == "BUY")
    assert float(buy_order.totalQuantity) == 20             # A2's shares only
    assert result["buy_results"][0]["split"] == {A2: 20}
    reasons = {r["account"]: r["reason"] for r in result["uninvested"]}
    assert reasons[A1] == "CASH_UNREADABLE"


# ========================================================================================
# (d) NO NETTING across accounts
# ========================================================================================
def test_no_netting_one_accounts_surplus_never_funds_another():
    buy = _block("IVV", "BUY", {A1: 20, A2: 20})
    resized, _dropped, detail = lx.resize_buy_routes_to_realized_cash(
        [(buy, BUY_PX)], {A1: 0.0, A2: 10_000_000.0})       # A2 is flush, A1 has nothing
    (new_route, _limit), = resized
    assert new_route.per_account_split == {A2: 20}          # A1 buys NOTHING
    assert new_route.total_qty == 20                        # A2 is NOT topped up to cover A1
    assert detail[A1]["sized_notional"] == 0.0
    assert detail[A2]["sized_notional"] == pytest.approx(20 * BUY_PX)


def test_no_netting_across_multiple_buy_blocks_of_one_account():
    # One account, two buy blocks: its OWN cash is shared across its own legs and nothing else.
    b1 = _block("IVV", "BUY", {A1: 10, A2: 10})             # $500 each
    b2 = _block("VTI", "BUY", {A1: 10, A2: 10})             # $500 each
    resized, _dropped, detail = lx.resize_buy_routes_to_realized_cash(
        [(b1, BUY_PX), (b2, BUY_PX)], {A1: 505.0, A2: 2_000.0})
    per_symbol = {r.symbol: r.per_account_split for r, _l in resized}
    # A1's budget is 505 * 0.99 = 499.95 -> the proportional whole-share FLOOR gives it 4+4=8
    # shares TOTAL across BOTH of its own legs, never 20. (The scaler only ever trims, so 8 is
    # the conservative answer — it NEVER rounds up to reach the budget exactly.)
    a1_total = sum(split.get(A1, 0) for split in per_symbol.values())
    assert a1_total == 8
    assert detail[A1]["sized_notional"] <= detail[A1]["budget"] + 1e-6
    # A2 is unaffected by A1's shortfall.
    assert sum(split.get(A2, 0) for split in per_symbol.values()) == 20


# ========================================================================================
# (e) UNKNOWN ROUTE SIDE -> FAIL CLOSED, whole run refused
# ========================================================================================
def test_unknown_route_side_refuses_the_whole_run(armed_env):
    ib = _FakeIB(cash={A1: 1_000_000.0, A2: 1_000_000.0})
    routes = [_block("SPY", "SELL", {A1: 10, A2: 10}),
              _block("IVV", "SHORT", {A1: 20, A2: 20}),      # not BUY and not SELL
              _block("VTI", "BUY", {A1: 5, A2: 5})]
    result = _run(ib, routes, permit=True)

    assert result["refused"] is True
    assert "UNKNOWN side" in result["refused_reason"]
    # Nothing at all happened — not even the mandatory backup, let alone a write or an order.
    assert ib.replace_fa_calls == 0
    assert ib.placed == []
    assert ib.account_summary_reads == 0
    assert result["backup"] == ""
    assert result["replace_fa_writes"] == 0
    assert result["placed_fills"] == []


@pytest.mark.parametrize("bad_side", ["", None, "sell_short", "buy ", "COVER"])
def test_unknown_route_side_variants_all_fail_closed(armed_env, bad_side):
    ib = _FakeIB(cash={A1: 1_000_000.0})
    routes = [_block("IVV", bad_side, {A1: 5, A2: 5})]
    result = _run(ib, routes, permit=True)
    assert result["refused"] is True
    assert ib.placed == [] and ib.replace_fa_calls == 0


@pytest.mark.parametrize("good_side", ["BUY", "SELL", "buy", "sell"])
def test_known_sides_are_not_refused(armed_env, good_side):
    ib = _FakeIB(cash={A1: 1_000_000.0, A2: 1_000_000.0})
    result = _run(ib, [_block("IVV", good_side, {A1: 2, A2: 2})], permit=True)
    assert result["refused"] is False


# ========================================================================================
# (f) UNINVESTED-PROCEEDS EXCEPTION REPORT
# ========================================================================================
def test_uninvested_report_quiet_when_everything_redeployed():
    detail = {A1: {"cash_read_ok": True, "had_buy_legs": True, "planned_notional": 1_000.0,
                   "min_buy_limit": BUY_PX, "realized_cash": 5_000.0, "budget": 4_950.0,
                   "sized_notional": 1_000.0, "adjustments": []}}
    assert lx.uninvested_proceeds_report({A1: 1_000.0}, detail, {A1: 1_000.0}) == []


def test_uninvested_report_ignores_sub_one_share_remainder():
    # A $49 shortfall on a $50 share is the irreducible whole-share remainder, NOT an exception.
    detail = {A1: {"cash_read_ok": True, "had_buy_legs": True, "planned_notional": 1_000.0,
                   "min_buy_limit": BUY_PX, "realized_cash": 5_000.0, "budget": 4_950.0,
                   "sized_notional": 951.0, "adjustments": []}}
    assert lx.uninvested_proceeds_report({A1: 1_000.0}, detail, {A1: 951.0}) == []


def test_uninvested_report_fires_when_a_whole_share_went_unbought():
    detail = {A1: {"cash_read_ok": True, "had_buy_legs": True, "planned_notional": 1_000.0,
                   "min_buy_limit": BUY_PX, "realized_cash": 1_000.0, "budget": 990.0,
                   "sized_notional": 950.0, "adjustments": []}}
    report = lx.uninvested_proceeds_report({A1: 1_000.0}, detail, {A1: 950.0})
    assert [r["reason"] for r in report] == ["BUY_SHORT_OF_REALIZED_CASH"]
    assert report[0]["dollars_uninvested"] == pytest.approx(50.0)


def test_uninvested_report_fires_when_account_sold_with_no_buy_route():
    detail = {A1: {"cash_read_ok": True, "had_buy_legs": False, "planned_notional": 0.0,
                   "min_buy_limit": None, "realized_cash": 2_000.0, "budget": 1_980.0,
                   "sized_notional": 0.0, "adjustments": []}}
    report = lx.uninvested_proceeds_report({A1: 2_000.0}, detail, {})
    assert [r["reason"] for r in report] == ["NO_BUY_ROUTE"]
    assert report[0]["dollars_uninvested"] == pytest.approx(2_000.0)


def test_uninvested_report_ignores_accounts_that_raised_nothing():
    detail = {A1: {"cash_read_ok": False, "had_buy_legs": True, "planned_notional": 1_000.0,
                   "min_buy_limit": BUY_PX, "realized_cash": None, "budget": 0.0,
                   "sized_notional": 0.0, "adjustments": []}}
    assert lx.uninvested_proceeds_report({A1: 0.0}, detail, {}) == []


def test_uninvested_report_fires_e2e_when_a_buy_block_does_not_fill(armed_env):
    # Both accounts sell fine, but the BUY block comes back completely unfilled -> the raised
    # proceeds are sitting in cash and BOTH accounts must be flagged.
    ib = _FakeIB(cash={A1: 1_000_000.0, A2: 1_000_000.0}, fill_frac={"IVV": 0.0})
    routes = [_block("SPY", "SELL", {A1: 10, A2: 10}),
              _block("IVV", "BUY", {A1: 20, A2: 20})]
    result = _run(ib, routes, permit=True)
    reasons = {r["account"]: r["reason"] for r in result["uninvested"]}
    assert reasons == {A1: "BUY_SHORT_OF_REALIZED_CASH", A2: "BUY_SHORT_OF_REALIZED_CASH"}
    dollars = {r["account"]: r["dollars_uninvested"] for r in result["uninvested"]}
    assert dollars[A1] == pytest.approx(20 * BUY_PX)


def test_uninvested_report_clean_e2e_when_everything_fills(armed_env):
    ib = _FakeIB(cash={A1: 1_000_000.0, A2: 1_000_000.0})
    routes = [_block("SPY", "SELL", {A1: 10, A2: 10}),
              _block("IVV", "BUY", {A1: 20, A2: 20})]
    result = _run(ib, routes, permit=True)
    assert result["uninvested"] == []


# ========================================================================================
# (g) PREVIEW — still transmits nothing, writes nothing, reads no cash
# ========================================================================================
def test_preview_transmits_nothing_and_does_no_cash_read(capsys, monkeypatch):
    monkeypatch.setattr(lx, "margin_preflight_over_split", lambda *a, **k: (True, ""))
    monkeypatch.setattr(lx, "_quotes_cache", {})
    ib = _FakeIB(cash={A1: 1_000_000.0, A2: 1_000_000.0})
    routes = [_block("IVV", "BUY", {A1: 20, A2: 20}),
              _block("SPY", "SELL", {A1: 10, A2: 10})]
    result = _run(ib, routes, permit=False)

    assert ib.replace_fa_calls == 0                 # NO FA config written
    assert ib.placed == []                          # nothing transmitted
    assert ib.account_summary_reads == 0            # NO broker cash read in preview
    assert ib.cancelled == []
    assert result["backup"] == ""
    assert result["realized_cash"] == {}
    assert result["buy_resize"] == {}
    assert result["uninvested"] == []
    # ...but the phasing IS visible in the printed output.
    out = capsys.readouterr().out
    assert "PHASE PLAN" in out
    assert "PHASE SELL" in out and "PHASE BUY" in out
    assert out.index("PHASE SELL") < out.index("PHASE BUY")
    assert "RE-SIZE every buy block" in out


def test_preview_stays_safe_under_the_committed_config_defaults(monkeypatch):
    # The committed on-disk posture (READONLY/DRY_RUN True) is what makes a preview a preview.
    monkeypatch.setattr(lx, "margin_preflight_over_split", lambda *a, **k: (True, ""))
    monkeypatch.setattr(lx, "_quotes_cache", {})
    assert config.READONLY is True and config.DRY_RUN is True
    ib = _FakeIB(cash={A1: 1_000_000.0, A2: 1_000_000.0})
    permit, _why = lx.gate_state(armed=True)
    assert permit is False
    result = _run(ib, [_block("SPY", "SELL", {A1: 10, A2: 10})], permit=permit)
    assert ib.placed == [] and ib.replace_fa_calls == 0
    assert result["n_sell_blocks"] == 1


# ========================================================================================
# EXISTING-CALLER CONTRACT — the original summary keys must keep working.
# ========================================================================================
def test_summary_keeps_the_original_keys(armed_env):
    ib = _FakeIB(cash={A1: 1_000_000.0, A2: 1_000_000.0})
    routes = [_block("SPY", "SELL", {A1: 10, A2: 10}),
              _block("IVV", "BUY", {A1: 20, A2: 20}),
              RoutePlan("direct", "Balanced", "TFLO", "BUY", 5, None, "", A1, {A1: 5})]
    result = _run(ib, routes, permit=True)
    for key in ("n_blocks", "n_direct_skipped", "replace_fa_writes", "placed_fills", "backup"):
        assert key in result, key
    assert result["n_blocks"] == 2
    assert result["n_direct_skipped"] == 1
    assert result["replace_fa_writes"] == 2
    assert len(result["placed_fills"]) == 2
    assert result["backup"] == "fake_backup.xml"


def test_empty_routes_still_returns_the_original_keys():
    result = _run(_FakeIB(), [], permit=False)
    assert result["n_blocks"] == 0 and result["replace_fa_writes"] == 0
    assert result["placed_fills"] == [] and result["backup"] == ""


# ========================================================================================
# The cash gate is safe_execute's — not a private copy.
# ========================================================================================
def test_cash_gate_pieces_are_the_shared_safe_execute_ones():
    import safe_execute
    assert lx.CASH_SAFETY_BUFFER_PCT is safe_execute.CASH_SAFETY_BUFFER_PCT
    assert lx.CASH_SETTLE_SEC is safe_execute.CASH_SETTLE_SEC
    assert lx.PHASE_TERMINAL_TIMEOUT_SEC is safe_execute.PHASE_TERMINAL_TIMEOUT_SEC
    assert lx._total_cash_value is safe_execute._total_cash_value
    assert lx._scale_buys_to_cash is safe_execute._scale_buys_to_cash
    assert lx._TERMINAL_STATUSES is safe_execute._TERMINAL_STATUSES
