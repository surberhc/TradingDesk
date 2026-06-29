"""
test_order_ladder.py — offline unit tests for the LADDERED order-execution router.
NO broker, NO gateway, NO real orders. Everything is mocked. Proves:

  (1) INSTRUMENT CLASSIFICATION — seed sets, the live spread-width heuristic, options by
      sec_type, and the safe default (illiquid) when nothing else applies.
  (2) ORDER BUILDERS — each emits the correct ib_async base-`Order` fields:
        MIDPRICE (orderType="MIDPRICE", cap as lmtPrice, DAY, US-equity only),
        Adaptive (algoStrategy="Adaptive", adaptivePriority TagValue, DAY — never GTC),
        REL (orderType="REL", cap as auxPrice — the options-safe peg),
        marketable_limit (capped LMT). transmit stays False at build time.
  (3) THE PRICE GUARD still rejects NaN / None / <= 0 on EVERY rung's cap, before any
      Order object is built.
  (4) THE LADDER LOOP — escalates on no-fill, re-places ONLY the unfilled remainder,
      terminates at the final (marketable) rung, respects the cap on every rung, and
      NEVER calls whatIfOrder.

Run:
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe -m pytest test_order_ladder.py -q
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import config
import live_quotes
import order_router as orm


# --- mock broker -------------------------------------------------------------
class _OrderStatus:
    def __init__(self, status="Submitted", filled=0.0, remaining=0.0, avg=0.0):
        self.status = status
        self.filled = filled
        self.remaining = remaining
        self.avgFillPrice = avg


# Sentinel for a REJECTED order: the live TFLO/VGSH case — terminal/done, filled=0 AND
# remaining=0, status=ValidationError. The exact false-success footgun.
def reject(status="ValidationError"):
    return ("reject", status)


class _Trade:
    """A minimal ib_async Trade stand-in. A script entry is either a fill quantity (int) or
    reject(status) for a terminal NON-FILL (filled=0, remaining=0, isDone True)."""
    def __init__(self, contract, order, entry):
        self.contract = contract
        self.order = order
        ordered = float(order.totalQuantity)
        if isinstance(entry, tuple) and entry and entry[0] == "reject":
            # Rejected/errored: terminal, NOTHING filled, remaining 0 (the trap).
            self._done = True
            self.orderStatus = _OrderStatus(status=entry[1], filled=0.0, remaining=0.0,
                                            avg=0.0)
        else:
            filled = min(float(entry), ordered)
            self._done = filled >= ordered
            self.orderStatus = _OrderStatus(
                status="Filled" if filled >= ordered else "Submitted",
                filled=filled, remaining=ordered - filled,
                avg=float(order.lmtPrice or order.auxPrice or 0.0))

    def isDone(self):
        return self._done


class MockIB:
    """Records placeOrder/cancelOrder calls; whatIfOrder raises if ever touched (it would
    hang in reality). `fill_script` is a list of per-rung entries: a fill quantity, or
    reject(status) for a terminal non-fill."""
    def __init__(self, fill_script):
        self.fill_script = list(fill_script)
        self.placed = []      # (contract, order)
        self.cancelled = []   # orders
        self.what_if_calls = 0
        self.global_cancel_calls = 0   # the footgun: mass-cancels resting orders

    def reqMarketDataType(self, *_):
        pass

    def qualifyContracts(self, *contracts):
        for c in contracts:
            c.conId = 111
        return contracts

    def placeOrder(self, contract, order):
        entry = self.fill_script.pop(0) if self.fill_script else 0.0
        t = _Trade(contract, order, entry)
        self.placed.append((contract, order))
        return t

    def cancelOrder(self, order):
        self.cancelled.append(order)

    def sleep(self, *_):
        pass

    def whatIfOrder(self, *_a, **_k):
        self.what_if_calls += 1
        raise AssertionError("whatIfOrder must NEVER be called in the ladder (it hangs).")

    def reqGlobalCancel(self, *_a, **_k):
        # The one footgun: a global cancel would mass-cancel our resting GTC orders. The
        # ladder/executor must NEVER call this. Tracked so a regression test can assert it.
        self.global_cancel_calls += 1


def _quote(bid=50.0, ask=50.20, last=50.10, close=50.05):
    return live_quotes.Quote("TFLO", bid=bid, ask=ask, last=last, close=close, md_type=1)


def _armed_caps(side="BUY"):
    cap = live_quotes.marketable_cap(side, _quote())
    return {"marketable_limit": cap, "midprice": cap, "adaptive": cap, "rel": cap}


@pytest.fixture(autouse=True)
def _arm(monkeypatch):
    """Open the gate IN MEMORY for the ladder-placement tests only (config on disk is left
    locked). Each test that needs transmission passes armed=True; this flips READONLY/
    DRY_RUN so transmit_guard permits. Tests that assert the guard are explicit."""
    monkeypatch.setattr(config, "READONLY", False)
    monkeypatch.setattr(config, "DRY_RUN", False)


# --- (1) classification ------------------------------------------------------
def test_classify_seed_sets():
    assert orm.classify_instrument("TFLO") == config.INSTRUMENT_CLASS_ILLIQUID_ETF
    assert orm.classify_instrument("VGSH") == config.INSTRUMENT_CLASS_ILLIQUID_ETF
    assert orm.classify_instrument("SPY") == config.INSTRUMENT_CLASS_LIQUID_ETF
    assert orm.classify_instrument("rsp") == config.INSTRUMENT_CLASS_LIQUID_ETF  # case-insens


def test_classify_option_by_sec_type():
    assert orm.classify_instrument("SPXW", sec_type="OPT") == config.INSTRUMENT_CLASS_INDEX_OPTION
    assert orm.classify_instrument("SPXW", sec_type="opt") == config.INSTRUMENT_CLASS_INDEX_OPTION


def test_classify_spread_heuristic():
    wide = config.ILLIQUID_SPREAD_THRESHOLD + 0.01
    tight = config.ILLIQUID_SPREAD_THRESHOLD / 10
    assert orm.classify_instrument("XYZ", relative_spread=wide) == config.INSTRUMENT_CLASS_ILLIQUID_ETF
    assert orm.classify_instrument("XYZ", relative_spread=tight) == config.INSTRUMENT_CLASS_LIQUID_ETF


def test_classify_unknown_defaults_illiquid():
    # No seed membership, no spread -> safe full ladder (illiquid), not a blind cross.
    assert orm.classify_instrument("XYZ") == config.INSTRUMENT_CLASS_ILLIQUID_ETF
    assert orm.classify_instrument("XYZ", relative_spread=float("nan")) == \
        config.INSTRUMENT_CLASS_ILLIQUID_ETF


def test_ladder_for_terminates_on_completing_rung():
    # Every ladder must END on a fill-completing rung so it always terminates: either a
    # marketable-limit cross, or Adaptive(Urgent) (fastest completion) for the liquid tier.
    def is_terminal(rung):
        if rung["order_type"] == "marketable_limit":
            return True
        return rung["order_type"] == "adaptive" and rung.get("priority") == "Urgent"
    for klass in (config.INSTRUMENT_CLASS_LIQUID_ETF,
                  config.INSTRUMENT_CLASS_ILLIQUID_ETF,
                  config.INSTRUMENT_CLASS_INDEX_OPTION):
        ladder = orm.ladder_for(klass)
        assert ladder, klass
        assert is_terminal(ladder[-1]), (klass, ladder[-1])
    # Unknown class falls back to the (terminating) illiquid ladder.
    assert orm.ladder_for("NOPE")[-1]["order_type"] == "marketable_limit"


def test_illiquid_ladder_is_the_research_spec():
    # MIDPRICE -> Adaptive(Patient) -> Adaptive(Urgent) -> marketable-limit cap.
    rungs = orm.ladder_for(config.INSTRUMENT_CLASS_ILLIQUID_ETF)
    assert [r["order_type"] for r in rungs] == \
        ["midprice", "adaptive", "adaptive", "marketable_limit"]
    assert rungs[1]["priority"] == "Patient"
    assert rungs[2]["priority"] == "Urgent"


def test_option_ladder_never_midprice():
    # Options: capped LMT / REL only — MIDPRICE is unsupported, no scheduler algos.
    rungs = orm.ladder_for(config.INSTRUMENT_CLASS_INDEX_OPTION)
    kinds = [r["order_type"] for r in rungs]
    assert "midprice" not in kinds
    assert "rel" in kinds


# --- (2) order builders emit correct ib_async Order fields -------------------
def test_build_midprice_fields():
    o = orm.build_midprice("TFLO", "BUY", 100, 50.35, account="DU1")
    assert o.orderType == "MIDPRICE"
    assert o.lmtPrice == 50.35          # cap is the worst-case limit
    assert o.action == "BUY"
    assert o.totalQuantity == 100
    assert o.tif == "DAY"               # never GTC
    # BUG 1 FIX: MIDPRICE must NOT be outsideRth — IBKR rejects MIDPRICE outside RTH
    # (Warning 321), and the flag made it eligible outside RTH even during RTH -> rejected.
    assert o.outsideRth is False
    assert o.transmit is False          # build never arms
    assert o.account == "DU1"


def test_all_rung_builders_outside_rth_false():
    # BUG 1 FIX, generalized: every rung builder defaults outsideRth=False (we operate
    # during regular trading hours; outsideRth=True broke MIDPRICE and risks Adaptive too).
    builders = [
        orm.build_marketable_limit("SPY", "BUY", 10, 100.0),
        orm.build_midprice("TFLO", "BUY", 10, 50.0),
        orm.build_adaptive("TFLO", "BUY", 10, 50.0, priority="Patient"),
        orm.build_rel("SPXW", "BUY", 1, 2.5),
        orm.build_gtc_limit("TFLO", "BUY", 10, 50.0),
    ]
    for o in builders:
        assert o.outsideRth is False, o.orderType


def test_build_adaptive_fields():
    o = orm.build_adaptive("TFLO", "SELL", 40, 49.85, priority="Patient")
    assert o.orderType == "LMT"
    assert o.algoStrategy == "Adaptive"
    assert len(o.algoParams) == 1
    tv = o.algoParams[0]
    assert tv.tag == "adaptivePriority" and tv.value == "Patient"
    assert o.tif == "DAY"               # Adaptive FORBIDS GTC
    assert o.outsideRth is False        # BUG 1 FIX: safe default, no outside-RTH eligibility
    assert o.lmtPrice == 49.85


def test_build_adaptive_rejects_bad_priority():
    with pytest.raises(ValueError):
        orm.build_adaptive("TFLO", "BUY", 10, 50.0, priority="Aggressive")


def test_build_rel_fields_options_safe():
    o = orm.build_rel("SPXW", "BUY", 1, 2.55)
    assert o.orderType == "REL"
    assert o.auxPrice == 2.55           # cap is the HARD limit on a REL peg
    # REL carries no explicit lmtPrice — ib_async leaves the field at its UNSET sentinel
    # (~1.8e308), not 0/None. The cap lives in auxPrice.
    assert o.lmtPrice > 1e300 or o.lmtPrice in (0.0, None)
    assert o.tif == "DAY"


def test_build_marketable_limit_fields():
    o = orm.build_marketable_limit("SPY", "BUY", 10, 100.10)
    assert o.orderType == "LMT"
    assert o.lmtPrice == 100.10
    assert o.algoStrategy in (None, "")


def test_build_fa_block_seam():
    # When opted in (the future FA-block path), faGroup/faMethod ride on the order and no
    # single account is set — the seam the executor uses for block legs.
    o = orm.build_midprice("TFLO", "BUY", 30, 50.35, fa_group="tier_cons", fa_method="")
    assert o.faGroup == "tier_cons"
    assert o.faMethod == ""
    assert o.account in (None, "")


# --- (3) price guard on every rung's cap -------------------------------------
@pytest.mark.parametrize("bad", [float("nan"), None, 0.0, -1.0])
@pytest.mark.parametrize("builder", [
    orm.build_midprice, orm.build_adaptive, orm.build_rel, orm.build_marketable_limit])
def test_price_guard_rejects_bad_cap_every_builder(builder, bad):
    with pytest.raises(ValueError):
        builder("TFLO", "BUY", 10, bad)


def test_build_rung_guards_cap():
    rung = {"order_type": "midprice"}
    with pytest.raises(ValueError):
        orm.build_rung(rung, "TFLO", "BUY", 10, float("nan"))


def test_build_rung_unknown_type():
    with pytest.raises(ValueError):
        orm.build_rung({"order_type": "twap"}, "TFLO", "BUY", 10, 50.0)


# --- (4) the ladder loop -----------------------------------------------------
def test_ladder_fills_on_first_rung_no_escalation():
    ib = MockIB(fill_script=[100])       # rung 1 fills fully
    res = orm.place_laddered(
        ib, symbol="TFLO", side="BUY", total_qty=100, caps=_armed_caps(),
        instrument_class=config.INSTRUMENT_CLASS_ILLIQUID_ETF, account="DU1",
        armed=True, rung_seconds=2, poll=0)
    assert res["remaining"] == 0
    assert res["rungs_used"] == 1
    assert len(ib.placed) == 1
    assert ib.cancelled == []            # nothing to cancel when fully filled
    assert ib.what_if_calls == 0


def test_ladder_escalates_and_replaces_only_remainder():
    # rung1 fills 40 of 100, rung2 fills the remaining 60. Two placements; the SECOND must
    # be sized to the unfilled remainder (60), and the first order is cancelled.
    ib = MockIB(fill_script=[40, 60])
    res = orm.place_laddered(
        ib, symbol="TFLO", side="BUY", total_qty=100, caps=_armed_caps(),
        instrument_class=config.INSTRUMENT_CLASS_ILLIQUID_ETF, account="DU1",
        armed=True, rung_seconds=2, poll=0)
    assert res["remaining"] == 0
    assert res["rungs_used"] == 2
    assert res["filled"] == 100
    qtys = [float(o.totalQuantity) for _, o in ib.placed]
    assert qtys == [100, 60]             # re-placed ONLY the unfilled 60
    assert len(ib.cancelled) == 1        # residual of rung1 cancelled before escalation
    # rung1 is MIDPRICE, rung2 is Adaptive(Patient) per the illiquid ladder.
    assert ib.placed[0][1].orderType == "MIDPRICE"
    assert ib.placed[1][1].algoStrategy == "Adaptive"
    assert ib.what_if_calls == 0


def test_ladder_terminates_at_final_rung_when_never_filled(monkeypatch):
    # No rung ever fills: with the GTC-remainder layer OFF the ladder must walk ALL rungs
    # of the illiquid recipe (4) and STOP — never loop forever — leaving the remainder
    # reported as incomplete (the resting layer is exercised in its own tests below).
    monkeypatch.setattr(config, "LADDER_REST_REMAINDER", False)
    ib = MockIB(fill_script=[0, 0, 0, 0])
    res = orm.place_laddered(
        ib, symbol="TFLO", side="BUY", total_qty=100, caps=_armed_caps(),
        instrument_class=config.INSTRUMENT_CLASS_ILLIQUID_ETF, account="DU1",
        armed=True, rung_seconds=1, poll=0)
    assert res["rungs_used"] == 4        # exactly the number of rungs, no more
    assert res["remaining"] == 100
    assert res["rested"] is False
    assert len(ib.placed) == 4
    # The terminal active rung is the marketable cap.
    assert ib.placed[-1][1].orderType == "LMT"
    assert ib.placed[-1][1].algoStrategy in (None, "")
    assert ib.what_if_calls == 0


def test_ladder_respects_cap_on_every_rung():
    cap = _armed_caps()["midprice"]
    ib = MockIB(fill_script=[0, 0, 0, 0])
    orm.place_laddered(
        ib, symbol="TFLO", side="BUY", total_qty=100, caps=_armed_caps(),
        instrument_class=config.INSTRUMENT_CLASS_ILLIQUID_ETF, account="DU1",
        armed=True, rung_seconds=1, poll=0)
    for _, o in ib.placed:
        worst = o.lmtPrice if o.orderType != "REL" else o.auxPrice
        assert worst == cap             # cap enforced as worst-case on every rung


def test_ladder_dry_run_transmits_nothing():
    # Gate BLOCKED (armed=False) -> builds rung-1 to prove the recipe, transmits nothing.
    ib = MockIB(fill_script=[100])
    res = orm.place_laddered(
        ib, symbol="TFLO", side="BUY", total_qty=100, caps=_armed_caps(),
        instrument_class=config.INSTRUMENT_CLASS_ILLIQUID_ETF, account="DU1",
        armed=False, rung_seconds=1, poll=0)
    assert res["transmitted"] == 0
    assert res["remaining"] == 100
    assert ib.placed == []              # NOTHING sent
    assert ib.what_if_calls == 0


def test_ladder_bad_cap_raises_before_send():
    # A NaN cap on the first rung must raise inside the builder before any placeOrder.
    ib = MockIB(fill_script=[100])
    caps = {"marketable_limit": float("nan"), "midprice": float("nan"),
            "adaptive": float("nan"), "rel": float("nan")}
    with pytest.raises(ValueError):
        orm.place_laddered(
            ib, symbol="TFLO", side="BUY", total_qty=100, caps=caps,
            instrument_class=config.INSTRUMENT_CLASS_ILLIQUID_ETF, account="DU1",
            armed=True, rung_seconds=1, poll=0)
    assert ib.placed == []


def test_liquid_etf_ladder_crosses_first():
    # Liquid ETF: rung1 is the marketable cross; if it fills, no algo rung is reached.
    ib = MockIB(fill_script=[10])
    res = orm.place_laddered(
        ib, symbol="SPY", side="BUY", total_qty=10,
        caps=_armed_caps(), instrument_class=config.INSTRUMENT_CLASS_LIQUID_ETF,
        account="DU1", armed=True, rung_seconds=1, poll=0)
    assert res["rungs_used"] == 1
    assert ib.placed[0][1].orderType == "LMT"
    assert ib.placed[0][1].algoStrategy in (None, "")


# --- (5) GTC-remainder layer: "ladder while connected, rest when gone" -------
def test_ladder_rests_unfilled_remainder_as_gtc():
    # No rung fills: after the 4 active rungs, the remainder is left RESTING as a plain
    # GTC LMT at the cap (NOT cancelled, NOT reported failed) so it survives disconnect.
    ib = MockIB(fill_script=[0, 0, 0, 0, 0])
    res = orm.place_laddered(
        ib, symbol="TFLO", side="BUY", total_qty=100, caps=_armed_caps(),
        instrument_class=config.INSTRUMENT_CLASS_ILLIQUID_ETF, account="DU1",
        order_ref="paperbot:DU1:t:BUY:TFLO", armed=True, rung_seconds=1, poll=0)
    assert res["rested"] is True
    assert res["resting_qty"] == 100
    assert res["fills"][0]["rested_gtc"] is True
    # 4 active rungs + 1 resting GTC = 5 placements; the last is a plain GTC LMT.
    assert len(ib.placed) == 5
    rest = ib.placed[-1][1]
    assert rest.orderType == "LMT"
    assert rest.tif == "GTC"                 # the disconnect-survival tif
    assert rest.algoStrategy in (None, "")   # plain LMT — Adaptive/MIDPRICE can't be GTC
    assert rest.lmtPrice == _armed_caps()["marketable_limit"]   # cap respected
    assert rest.orderRef == "paperbot:DU1:t:BUY:TFLO"           # deterministic ref kept
    assert ib.what_if_calls == 0
    assert ib.global_cancel_calls == 0


def test_ladder_rest_remainder_only_remainder_qty():
    # Partial fills along the way -> the resting GTC carries ONLY the still-unfilled qty.
    ib = MockIB(fill_script=[30, 20, 10, 0, 0])   # 60 filled, 40 left to rest
    res = orm.place_laddered(
        ib, symbol="TFLO", side="BUY", total_qty=100, caps=_armed_caps(),
        instrument_class=config.INSTRUMENT_CLASS_ILLIQUID_ETF, account="DU1",
        armed=True, rung_seconds=1, poll=0)
    assert res["rested"] is True
    assert res["filled"] == 60
    assert res["remaining"] == 40
    assert float(ib.placed[-1][1].totalQuantity) == 40
    assert ib.placed[-1][1].tif == "GTC"


def test_ladder_no_rest_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "LADDER_REST_REMAINDER", False)
    ib = MockIB(fill_script=[0, 0, 0, 0])
    res = orm.place_laddered(
        ib, symbol="TFLO", side="BUY", total_qty=100, caps=_armed_caps(),
        instrument_class=config.INSTRUMENT_CLASS_ILLIQUID_ETF, account="DU1",
        armed=True, rung_seconds=1, poll=0)
    assert res["rested"] is False
    assert len(ib.placed) == 4                # no extra GTC placement
    assert all(o.tif != "GTC" for _, o in ib.placed)


def test_ladder_full_fill_no_resting():
    ib = MockIB(fill_script=[100])
    res = orm.place_laddered(
        ib, symbol="TFLO", side="BUY", total_qty=100, caps=_armed_caps(),
        instrument_class=config.INSTRUMENT_CLASS_ILLIQUID_ETF, account="DU1",
        armed=True, rung_seconds=1, poll=0)
    assert res["rested"] is False
    assert res["remaining"] == 0
    assert all(o.tif != "GTC" for _, o in ib.placed)


def test_build_gtc_limit_fields():
    o = orm.build_gtc_limit("TFLO", "BUY", 40, 50.35, account="DU1",
                            order_ref="paperbot:DU1:t:BUY:TFLO")
    assert o.orderType == "LMT"
    assert o.tif == "GTC"
    assert o.lmtPrice == 50.35
    assert o.algoStrategy in (None, "")      # plain LMT
    assert o.transmit is False               # build never arms
    assert o.orderRef == "paperbot:DU1:t:BUY:TFLO"


@pytest.mark.parametrize("bad", [float("nan"), None, 0.0, -1.0])
def test_build_gtc_limit_guards_cap(bad):
    with pytest.raises(ValueError):
        orm.build_gtc_limit("TFLO", "BUY", 40, bad)


def test_rest_remainder_bad_cap_raises(monkeypatch):
    # If somehow the rest cap is bad, the GTC builder's PRICE GUARD must raise before send.
    ib = MockIB(fill_script=[0, 0, 0, 0])
    caps = dict(_armed_caps())
    caps["marketable_limit"] = float("nan")   # the rest uses the marketable cap
    # The active marketable rung also uses this cap, so the guard fires on rung-4 build.
    with pytest.raises(ValueError):
        orm.place_laddered(
            ib, symbol="TFLO", side="BUY", total_qty=100, caps=caps,
            instrument_class=config.INSTRUMENT_CLASS_ILLIQUID_ETF, account="DU1",
            armed=True, rung_seconds=1, poll=0)


# --- (6) S5 server-side seam: conditional + OCA builders (scaffolding) -------
def test_build_price_condition_fields():
    c = orm.build_price_condition(trigger_conid=416904, price=4000.0, is_more=False,
                                  exchange="CBOE", trigger_method=2)
    from ib_async import PriceCondition
    assert isinstance(c, PriceCondition)
    assert c.conId == 416904
    assert c.price == 4000.0
    assert c.isMore is False
    assert c.exch == "CBOE"
    assert c.triggerMethod == 2


def test_build_price_condition_guards_conid_and_price():
    with pytest.raises(ValueError):
        orm.build_price_condition(trigger_conid=0, price=4000.0, is_more=False, exchange="CBOE")
    with pytest.raises(ValueError):
        orm.build_price_condition(trigger_conid=1, price=float("nan"), is_more=False, exchange="CBOE")


def test_build_time_condition_fields():
    c = orm.build_time_condition(time="20260630 15:45:00", is_more=True)
    from ib_async import TimeCondition
    assert isinstance(c, TimeCondition)
    assert c.time == "20260630 15:45:00"
    assert c.isMore is True
    with pytest.raises(ValueError):
        orm.build_time_condition(time="")


def test_build_conditional_order_attaches_conditions():
    pc = orm.build_price_condition(trigger_conid=416904, price=4000.0, is_more=False,
                                   exchange="CBOE")
    o = orm.build_conditional_order("SPXW", "SELL", 1, 2.55, [pc])
    assert o.orderType == "LMT"
    assert o.lmtPrice == 2.55
    assert o.tif == "GTC"                    # staged trigger rests at IB
    assert len(o.conditions) == 1
    assert o.conditions[0] is pc
    assert o.conditionsCancelOrder is False  # False = ACTIVATE on condition
    assert o.transmit is False               # NOT armed by build


def test_build_conditional_order_cancel_semantics_and_guard():
    pc = orm.build_price_condition(trigger_conid=416904, price=4000.0, is_more=True,
                                   exchange="CBOE")
    o = orm.build_conditional_order("SPXW", "SELL", 1, 2.55, [pc],
                                    conditions_cancel_order=True)
    assert o.conditionsCancelOrder is True   # True = CANCEL working order on condition
    with pytest.raises(ValueError):
        orm.build_conditional_order("SPXW", "SELL", 1, 2.55, [])   # no conditions
    with pytest.raises(ValueError):
        orm.build_conditional_order("SPXW", "SELL", 1, float("nan"), [pc])  # bad cap


def test_apply_oca_group_tags_basket():
    a = orm.build_gtc_limit("SPXW", "SELL", 1, 2.5)
    b = orm.build_gtc_limit("SPXW", "SELL", 1, 2.6)
    orm.apply_oca_group([a, b], "s5_cover_2026q3", oca_type=1)
    assert a.ocaGroup == b.ocaGroup == "s5_cover_2026q3"
    assert a.ocaType == b.ocaType == 1       # 1 = cancel-remaining-with-block (overfill-safe)
    with pytest.raises(ValueError):
        orm.apply_oca_group([a], "")          # empty group
    with pytest.raises(ValueError):
        orm.apply_oca_group([a], "g", oca_type=9)   # bad oca_type


# --- (7) the footgun guard: NEVER reqGlobalCancel ----------------------------
def test_place_laddered_never_global_cancels():
    # A full no-fill ladder (which now rests a GTC) must NEVER mass-cancel resting orders.
    ib = MockIB(fill_script=[0, 0, 0, 0, 0])
    orm.place_laddered(
        ib, symbol="TFLO", side="BUY", total_qty=100, caps=_armed_caps(),
        instrument_class=config.INSTRUMENT_CLASS_ILLIQUID_ETF, account="DU1",
        armed=True, rung_seconds=1, poll=0)
    assert ib.global_cancel_calls == 0
    assert ib.what_if_calls == 0


def test_order_router_source_has_no_global_cancel():
    # Regression: the order-router / executor source must not call reqGlobalCancel anywhere
    # (it is the only thing that mass-cancels our resting GTC orders). Guard the source.
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    for fname in ("order_router.py", "rebalance_execute.py"):
        with open(os.path.join(here, fname), encoding="utf-8") as fh:
            src = fh.read()
        assert "reqGlobalCancel" not in src, f"{fname} must NEVER call reqGlobalCancel"


# --- (8) BUG 2 regression: a rejected rung must ESCALATE, not false-report FILLED ----
# Live DU142 reproduction: a MIDPRICE rung-1 returns status=ValidationError with filled=0
# AND remaining=0. The old loop read remaining==0 as FILLED and stopped. The fix: complete
# ONLY when CUMULATIVE filled >= target; a terminal non-fill escalates the remainder.
def test_rejected_midprice_escalates_not_false_filled():
    # rung1 (MIDPRICE) REJECTED, rung2 (Adaptive Patient) fills the full 100.
    ib = MockIB(fill_script=[reject("ValidationError"), 100])
    res = orm.place_laddered(
        ib, symbol="TFLO", side="BUY", total_qty=100, caps=_armed_caps(),
        instrument_class=config.INSTRUMENT_CLASS_ILLIQUID_ETF, account="DU1",
        armed=True, rung_seconds=1, poll=0)
    # It must NOT have stopped at rung 1; the next rung was attempted.
    assert res["rungs_used"] >= 2
    assert len(ib.placed) >= 2
    assert ib.placed[0][1].orderType == "MIDPRICE"          # the rejected rung
    assert ib.placed[1][1].algoStrategy == "Adaptive"       # escalated to next rung
    # Reported filled reflects ACTUAL fills (100), and it is genuinely complete.
    assert res["filled"] == 100
    assert res["remaining"] == 0
    assert res["rested"] is False
    # A rejected order is NOT a residual to cancel (nothing rests) — no cancel for rung1.
    assert ib.what_if_calls == 0
    assert ib.global_cancel_calls == 0


def test_fully_rejected_ladder_rests_gtc_never_false_filled():
    # EVERY active rung rejected (the worst TFLO/VGSH case). The ladder must NOT report
    # "FILLED filled=0" — it must report filled=0 and REST the genuine remainder as GTC.
    ib = MockIB(fill_script=[reject(), reject(), reject(), reject(), 0])
    res = orm.place_laddered(
        ib, symbol="TFLO", side="BUY", total_qty=100, caps=_armed_caps(),
        instrument_class=config.INSTRUMENT_CLASS_ILLIQUID_ETF, account="DU1",
        order_ref="paperbot:DU1:t:BUY:TFLO", armed=True, rung_seconds=1, poll=0)
    assert res["filled"] == 0                  # NOTHING actually filled
    assert res["remaining"] == 100             # the FULL target is still unfilled
    assert res["rested"] is True               # rested as GTC, NOT reported FILLED
    assert res["resting_qty"] == 100
    # 4 active rungs all rejected + 1 GTC rest = 5 placements; last is a GTC LMT for 100.
    assert len(ib.placed) == 5
    rest = ib.placed[-1][1]
    assert rest.tif == "GTC"
    assert float(rest.totalQuantity) == 100
    assert rest.orderRef == "paperbot:DU1:t:BUY:TFLO"


def test_rejected_first_then_partial_tracks_cumulative():
    # rung1 rejected (0), rung2 partial (40), rung3 fills the remaining 60.
    ib = MockIB(fill_script=[reject("Rejected"), 40, 60])
    res = orm.place_laddered(
        ib, symbol="TFLO", side="BUY", total_qty=100, caps=_armed_caps(),
        instrument_class=config.INSTRUMENT_CLASS_ILLIQUID_ETF, account="DU1",
        armed=True, rung_seconds=1, poll=0)
    assert res["filled"] == 100
    assert res["remaining"] == 0
    assert res["rested"] is False
    # rung2 re-placed only the unfilled remainder (100), rung3 only the remaining 60.
    qtys = [float(o.totalQuantity) for _, o in ib.placed]
    assert qtys == [100, 100, 60]              # rung1=100(rejected), rung2=100, rung3=60


def test_partial_fill_then_rejected_rests_only_unfilled():
    # rung1 fills 30, the rest of the active rungs reject -> rest only the unfilled 70.
    ib = MockIB(fill_script=[30, reject(), reject(), reject(), 0])
    res = orm.place_laddered(
        ib, symbol="TFLO", side="BUY", total_qty=100, caps=_armed_caps(),
        instrument_class=config.INSTRUMENT_CLASS_ILLIQUID_ETF, account="DU1",
        armed=True, rung_seconds=1, poll=0)
    assert res["filled"] == 30
    assert res["remaining"] == 70
    assert res["rested"] is True
    assert float(ib.placed[-1][1].totalQuantity) == 70   # GTC rests ONLY the unfilled 70


@pytest.mark.parametrize("status", ["ValidationError", "Rejected", "Cancelled",
                                    "ApiCancelled", "Inactive"])
def test_all_terminal_nonfill_statuses_escalate(status):
    # Each terminal non-fill status on rung1 must escalate to rung2, never report FILLED.
    ib = MockIB(fill_script=[reject(status), 100])
    res = orm.place_laddered(
        ib, symbol="TFLO", side="BUY", total_qty=100, caps=_armed_caps(),
        instrument_class=config.INSTRUMENT_CLASS_ILLIQUID_ETF, account="DU1",
        armed=True, rung_seconds=1, poll=0)
    assert res["rungs_used"] >= 2
    assert res["filled"] == 100
