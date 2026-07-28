"""test_ledger.py — offline unit tests for the CRM sleeve-ledger core (conductor #42/#43).

Pure/offline: no broker, no gateway, no I/O. Runs with zero infra:
    cd crm
    "C:\\TradingDesk-Local\\venv\\Scripts\\python.exe" -m pytest -q
"""
from __future__ import annotations

from datetime import datetime

import pytest

import domain
from sleeve_ledger import (
    POS_EPS,
    Instrument,
    SleeveLedgerEntry,
    SleeveLedger,
    build_group_sleeve_map,
    attribute_block_fill,
    NettingConflict,
    detect_netting,
    net_and_split,
    InstrumentReconStatus,
    ReconResult,
    reconcile_account,
)


NOW = datetime(2026, 7, 24, 15, 30, 0)


# ===========================================================================
# 1) Instrument identity / equality / serialization  (§3.5)
# ===========================================================================
def test_equity_equality_by_symbol_and_sec_type():
    a = Instrument.stock("SPY")
    # A con_id/multiplier a caller filled in must NOT change equity identity.
    b = Instrument(symbol="SPY", sec_type="STK", con_id=756733, multiplier=1.0)
    assert a == b
    assert hash(a) == hash(b)
    assert a != Instrument.stock("QQQ")


def test_two_spx_options_with_different_strikes_are_distinct():
    o1 = Instrument.option("SPX", "20260814", 5000.0, "C", con_id=1)
    o2 = Instrument.option("SPX", "20260814", 5010.0, "C", con_id=2)
    assert o1 != o2
    assert hash(o1) != hash(o2)
    # Same full tuple → equal.
    o1b = Instrument.option("SPX", "20260814", 5000.0, "C", con_id=1)
    assert o1 == o1b
    # An option is never equal to a like-named stock.
    assert o1 != Instrument.stock("SPX")


def test_option_and_stock_can_coexist_as_dict_keys():
    d = {}
    d[Instrument.stock("SPX")] = 1
    d[Instrument.option("SPX", "20260814", 5000.0, "C")] = 2
    d[Instrument.option("SPX", "20260814", 5010.0, "P")] = 3
    assert len(d) == 3


def test_instrument_key_roundtrip_stock_and_option():
    for inst in (
        Instrument.stock("PDBC"),
        Instrument.option("SPX", "20260814", 5000.5, "C", con_id=898044756),
        Instrument.option("SPY", "20260101", 400.0, "P", con_id=None, multiplier=100.0),
    ):
        assert Instrument.from_key(inst.key()) == inst


def test_instrument_to_dict_from_dict_roundtrip():
    inst = Instrument.option("SPX", "20260814", 5000.0, "C", con_id=42, multiplier=100.0)
    assert Instrument.from_dict(inst.to_dict()) == inst
    stk = Instrument.stock("GLD")
    assert Instrument.from_dict(stk.to_dict()) == stk


# ===========================================================================
# 2) apply_delta / attribute_fill  (§3.5 / §7.2)
# ===========================================================================
def test_get_or_create_entry_starts_empty_at_version_zero():
    led = SleeveLedger()
    e = led.entry("DU141", "S0-Balanced", target_weight=0.75)
    assert e.ledger_version == 0
    assert e.attributed_cash == 0.0
    assert e.attributed_positions == {}
    assert e.target_weight == 0.75
    # get-or-create returns the SAME row; target_weight only set on create.
    e2 = led.entry("DU141", "S0-Balanced", target_weight=0.1)
    assert e2 is e
    assert e2.target_weight == 0.75


def test_apply_delta_buy_then_bump_and_cash_sign():
    led = SleeveLedger()
    spy = Instrument.stock("SPY")
    e = led.apply_delta("DU141", "S0-Balanced", spy, 10, -1000.0)
    assert e.attributed_positions[spy] == 10
    assert e.attributed_cash == -1000.0
    assert e.ledger_version == 1
    led.apply_delta("DU141", "S0-Balanced", spy, 5, -500.0)
    assert e.attributed_positions[spy] == 15
    assert e.ledger_version == 2


def test_apply_delta_deletes_position_at_zero():
    led = SleeveLedger()
    spy = Instrument.stock("SPY")
    led.apply_delta("DU141", "S0-Balanced", spy, 10, -1000.0)
    e = led.apply_delta("DU141", "S0-Balanced", spy, -10, 1000.0)
    assert spy not in e.attributed_positions      # closed line holds nothing
    assert e.attributed_cash == 0.0
    assert e.ledger_version == 2


def test_apply_delta_deletes_within_epsilon():
    led = SleeveLedger()
    spy = Instrument.stock("SPY")
    led.apply_delta("DU141", "S0", spy, 1.0, 0.0)
    e = led.apply_delta("DU141", "S0", spy, -1.0 + POS_EPS / 2, 0.0)
    assert spy not in e.attributed_positions


def test_apply_delta_reconciled_flag_sets_timestamp():
    led = SleeveLedger()
    spy = Instrument.stock("SPY")
    e = led.apply_delta("DU141", "S0", spy, 1, -10.0, now=NOW)
    assert e.last_reconciled_at is None            # a plain fill is not a reconcile
    e = led.apply_delta("DU141", "S0", spy, 1, -10.0, now=NOW, reconciled=True)
    assert e.last_reconciled_at == NOW


def test_attribute_fill_cash_math_stock():
    led = SleeveLedger()
    spy = Instrument.stock("SPY")
    # BUY 10 @ 100, commission 1.0 → cash = -(10*100*1) - 1 = -1001
    cash = led.attribute_fill("DU141", "S0", spy, 10, 100.0, commission=1.0)
    assert cash == pytest.approx(-1001.0)
    e = led.entry("DU141", "S0")
    assert e.attributed_positions[spy] == 10
    assert e.attributed_cash == pytest.approx(-1001.0)


def test_attribute_fill_cash_math_sell_adds_cash():
    led = SleeveLedger()
    spy = Instrument.stock("SPY")
    # SELL 5 (qty -5) @ 100, commission 1.0 → cash = -(-5*100*1) - 1 = +499
    cash = led.attribute_fill("DU141", "S0", spy, -5, 100.0, commission=1.0)
    assert cash == pytest.approx(499.0)


def test_attribute_fill_option_multiplier():
    led = SleeveLedger()
    opt = Instrument.option("SPX", "20260814", 5000.0, "C", multiplier=100.0)
    # BUY 1 @ 0.68, commission 0 → cash = -(1 * 0.68 * 100) = -68
    cash = led.attribute_fill("DU141", "S8-Overlay", opt, 1, 0.68)
    assert cash == pytest.approx(-68.0)


# ===========================================================================
# 3) build_group_sleeve_map / attribute_block_fill  (§7.1 / §7.2 / §13.3)
# ===========================================================================
def test_build_group_sleeve_map_from_registry():
    gsm = build_group_sleeve_map()
    assert gsm["tier_balanced"] == "S0-Balanced"
    assert gsm["s8_overlay"] == "S8-Overlay"
    # one group == one sleeve invariant: injective on the real registry
    assert len(gsm) == len(domain.SLEEVE_REGISTRY)


def test_build_group_sleeve_map_raises_on_collision():
    reg = {
        "A": domain.Sleeve("A", "adaptive_all_weather", "Balanced", "dup_group"),
        "B": domain.Sleeve("B", "adaptive_all_weather", "Growth", "dup_group"),
    }
    with pytest.raises(ValueError, match="one group == one sleeve"):
        build_group_sleeve_map(reg)


def test_attribute_block_fill_pdbc_numbers_from_spec_13_1():
    # §13.1: BUY 2 PDBC @ 18.14, commission 0.368706, split 1 share to each of two accounts.
    # Each account should book cash ≈ -18.3244 (= 18.14 + half the commission).
    led = SleeveLedger()
    pdbc = Instrument.stock("PDBC")
    applied = attribute_block_fill(
        led,
        fa_group="tier_balanced",
        per_account_split={"DU8922143": 1, "DU8922144": 1},
        instrument=pdbc,
        price=18.14,
        commission_total=0.368706,
        side="BUY",
    )
    for acct in ("DU8922143", "DU8922144"):
        rec = applied[acct]
        assert rec["sleeve"] == "S0-Balanced"
        assert rec["signed_qty"] == 1
        assert rec["commission_share"] == pytest.approx(0.184353)
        assert rec["cash_delta"] == pytest.approx(-18.324353, abs=1e-6)
        e = led.entry(acct, "S0-Balanced")
        assert e.attributed_positions[pdbc] == 1
        assert e.attributed_cash == pytest.approx(-18.324353, abs=1e-6)


def test_attribute_block_fill_pro_rata_commission_uneven_split():
    led = SleeveLedger()
    spy = Instrument.stock("SPY")
    # 3 + 1 = 4 shares; commission 4.0 splits 3.0 / 1.0.
    applied = attribute_block_fill(
        led, fa_group="tier_balanced",
        per_account_split={"A": 3, "B": 1},
        instrument=spy, price=100.0, commission_total=4.0, side="BUY")
    assert applied["A"]["commission_share"] == pytest.approx(3.0)
    assert applied["B"]["commission_share"] == pytest.approx(1.0)


def test_attribute_block_fill_sell_side_signs_negative():
    led = SleeveLedger()
    spy = Instrument.stock("SPY")
    applied = attribute_block_fill(
        led, fa_group="tier_balanced",
        per_account_split={"A": 2}, instrument=spy, price=50.0, side="SELL")
    assert applied["A"]["signed_qty"] == -2
    # SELL adds cash: -(-2 * 50 * 1) = +100
    assert applied["A"]["cash_delta"] == pytest.approx(100.0)


def test_attribute_block_fill_unknown_group_raises():
    led = SleeveLedger()
    with pytest.raises(ValueError, match="no sleeve"):
        attribute_block_fill(led, fa_group="nope",
                             per_account_split={"A": 1},
                             instrument=Instrument.stock("SPY"), price=1.0)


# ===========================================================================
# blended views aggregation  (§3.5 / §7.4)
# ===========================================================================
def test_blended_positions_and_cash_sum_across_sleeves():
    led = SleeveLedger()
    spy = Instrument.stock("SPY")
    gld = Instrument.stock("GLD")
    led.apply_delta("DU141", "S0-Balanced", spy, 10, -1000.0)
    led.apply_delta("DU141", "S0-Growth", spy, 5, -500.0)     # same instrument, other sleeve
    led.apply_delta("DU141", "S0-Growth", gld, 3, -300.0)
    led.apply_delta("OTHER", "S0-Balanced", spy, 99, -1.0)    # different account, excluded
    blended = led.blended_positions("DU141")
    assert blended[spy] == 15          # 10 + 5 across the two sleeves
    assert blended[gld] == 3
    assert led.blended_cash("DU141") == pytest.approx(-1800.0)


def test_blended_positions_drops_cross_sleeve_net_zero():
    led = SleeveLedger()
    spy = Instrument.stock("SPY")
    led.apply_delta("DU141", "S0-Balanced", spy, 10, 0.0)
    led.apply_delta("DU141", "S0-Growth", spy, -10, 0.0)      # offsets to zero blended
    assert spy not in led.blended_positions("DU141")


def test_entries_for_account_and_all_entries():
    led = SleeveLedger()
    led.entry("A", "S0-Balanced")
    led.entry("A", "S0-Growth")
    led.entry("B", "S0-Balanced")
    assert len(led.entries_for_account("A")) == 2
    assert len(led.all_entries()) == 3


# ===========================================================================
# 4) Netting watch  (§7.3)
# ===========================================================================
def test_detect_netting_finds_shared_instrument():
    spy = Instrument.stock("SPY")
    gld = Instrument.stock("GLD")
    conflicts = detect_netting({
        "S0-Balanced": {spy: 10, gld: 4},
        "S0-Growth": {spy: -3},        # spy shared across two sleeves
    })
    assert len(conflicts) == 1
    c = conflicts[0]
    assert c.instrument == spy
    assert c.per_sleeve == {"S0-Balanced": 10, "S0-Growth": -3}
    assert c.net == 7


def test_detect_netting_ignores_single_sleeve_instruments():
    spy = Instrument.stock("SPY")
    gld = Instrument.stock("GLD")
    conflicts = detect_netting({
        "S0-Balanced": {spy: 10},
        "S0-Growth": {gld: 5},
    })
    assert conflicts == []


def test_net_and_split_computes_net_and_pro_rata_fractions():
    spy = Instrument.stock("SPY")
    c = NettingConflict(instrument=spy, per_sleeve={"S0-Balanced": 10, "S0-Growth": -3}, net=7)
    net, split_back = net_and_split(c)
    assert net == 7
    assert split_back["S0-Balanced"] == pytest.approx(10 / 7)
    assert split_back["S0-Growth"] == pytest.approx(-3 / 7)
    # fractions sum to 1.0 → applied to the net fill they reconstruct each requested delta
    assert sum(split_back.values()) == pytest.approx(1.0)
    assert split_back["S0-Balanced"] * net == pytest.approx(10)
    assert split_back["S0-Growth"] * net == pytest.approx(-3)


def test_net_and_split_fully_offsetting_is_a_wash():
    spy = Instrument.stock("SPY")
    c = NettingConflict(instrument=spy, per_sleeve={"S0-Balanced": 5, "S0-Growth": -5}, net=0)
    net, split_back = net_and_split(c)
    assert net == 0
    assert split_back == {}


# ===========================================================================
# 5) Reconciliation checksum  (§7.4) — produces §12.3 classifications
# ===========================================================================
def _seed_account(led, account="DU141", sleeve="S0-Balanced"):
    spy = Instrument.stock("SPY")
    led.apply_delta(account, sleeve, spy, 10, -1000.0)
    return spy


def test_reconcile_all_match_is_ok():
    led = SleeveLedger()
    spy = _seed_account(led)
    res = reconcile_account(led, "DU141", {spy: 10}, -1000.0)
    assert res.verdict == "OK"
    assert res.cash_status == "MATCH"
    assert all(s.status == "MATCH" for s in res.per_instrument)
    assert res.drift_instruments == []
    assert res.alien_instruments == []


def test_reconcile_ledger_drift_on_qty_mismatch():
    led = SleeveLedger()
    spy = _seed_account(led)
    # Broker shows 8, ledger attributes 10 → hard LEDGER_DRIFT.
    res = reconcile_account(led, "DU141", {spy: 8}, -1000.0)
    assert res.verdict == "DRIFT"
    assert [s.status for s in res.per_instrument] == ["LEDGER_DRIFT"]
    assert res.drift_instruments[0].instrument == spy


def test_reconcile_alien_when_broker_holds_unattributed_symbol():
    led = SleeveLedger()
    spy = _seed_account(led)
    aapl = Instrument.stock("AAPL")   # broker holds it, ledger attributes zero
    res = reconcile_account(led, "DU141", {spy: 10, aapl: 4}, -1000.0)
    assert res.verdict == "REVIEW"        # ALIEN, but no hard drift
    statuses = {s.instrument.symbol: s.status for s in res.per_instrument}
    assert statuses == {"SPY": "MATCH", "AAPL": "ALIEN"}
    assert res.alien_instruments[0].instrument == aapl


def test_reconcile_cash_drift():
    led = SleeveLedger()
    spy = _seed_account(led)
    # Positions match, but broker cash off by more than the $1 mechanical tol.
    res = reconcile_account(led, "DU141", {spy: 10}, -1005.0)
    assert res.cash_status == "CASH_DRIFT"
    assert res.verdict == "DRIFT"


def test_reconcile_cash_within_tol_is_match():
    led = SleeveLedger()
    spy = _seed_account(led)
    res = reconcile_account(led, "DU141", {spy: 10}, -1000.50)  # within $1
    assert res.cash_status == "MATCH"
    assert res.verdict == "OK"


def test_reconcile_drift_outranks_review():
    led = SleeveLedger()
    spy = _seed_account(led)
    aapl = Instrument.stock("AAPL")
    # ALIEN aapl AND a hard drift on spy → DRIFT wins over REVIEW.
    res = reconcile_account(led, "DU141", {spy: 8, aapl: 4}, -1000.0)
    assert res.verdict == "DRIFT"
    assert res.alien_instruments[0].instrument == aapl
    assert res.drift_instruments[0].instrument == spy


def test_reconcile_ledger_holds_broker_flat_is_drift():
    # Ledger attributes a position the broker no longer shows → LEDGER_DRIFT (not ALIEN).
    led = SleeveLedger()
    spy = _seed_account(led)
    res = reconcile_account(led, "DU141", {}, -1000.0)
    assert [s.status for s in res.per_instrument] == ["LEDGER_DRIFT"]
    assert res.verdict == "DRIFT"


def test_reconcile_pos_tol_absorbs_float_noise():
    led = SleeveLedger()
    spy = _seed_account(led)
    res = reconcile_account(led, "DU141", {spy: 10 + 1e-9}, -1000.0)
    assert res.verdict == "OK"


# ===========================================================================
# Serialization round-trips  (§8 transport boundary — no persistence)
# ===========================================================================
def test_entry_to_from_dict_roundtrip():
    e = SleeveLedgerEntry(
        account_id="DU141", sleeve_id="S8-Overlay", target_weight=0.25,
        attributed_positions={
            Instrument.stock("SPY"): 10,
            Instrument.option("SPX", "20260814", 5000.0, "C", con_id=42): 2,
        },
        attributed_cash=-1234.56,
        last_reconciled_at=NOW,
        ledger_version=7,
    )
    back = SleeveLedgerEntry.from_dict(e.to_dict())
    assert back.account_id == e.account_id
    assert back.sleeve_id == e.sleeve_id
    assert back.target_weight == e.target_weight
    assert back.attributed_positions == e.attributed_positions
    assert back.attributed_cash == e.attributed_cash
    assert back.last_reconciled_at == NOW
    assert back.ledger_version == 7


def test_entry_roundtrip_with_no_reconcile_time():
    e = SleeveLedgerEntry(account_id="A", sleeve_id="S0-Balanced")
    back = SleeveLedgerEntry.from_dict(e.to_dict())
    assert back.last_reconciled_at is None


def test_ledger_to_from_dict_roundtrip():
    led = SleeveLedger()
    spy = Instrument.stock("SPY")
    opt = Instrument.option("SPX", "20260814", 5000.0, "C", con_id=42)
    led.apply_delta("DU141", "S0-Balanced", spy, 10, -1000.0, now=NOW, reconciled=True)
    led.apply_delta("DU141", "S8-Overlay", opt, 2, -136.0)
    led.apply_delta("DU999", "S0-Growth", spy, 3, -300.0)

    back = SleeveLedger.from_dict(led.to_dict())
    assert len(back.all_entries()) == 3
    assert back.blended_positions("DU141") == {spy: 10, opt: 2}
    assert back.blended_cash("DU141") == pytest.approx(-1136.0)
    e = back.entry("DU141", "S0-Balanced")
    assert e.ledger_version == 1
    assert e.last_reconciled_at == NOW
    # A reconcile of the rehydrated ledger against matching broker truth is OK.
    res = reconcile_account(back, "DU141", {spy: 10, opt: 2}, -1136.0)
    assert res.verdict == "OK"
