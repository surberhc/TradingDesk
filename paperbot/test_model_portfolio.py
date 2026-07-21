"""
test_model_portfolio.py — pure offline unit tests for the Model Portfolios sleeve module.

SYNTHETIC data only (fake accounts, weights, prices, positions). No broker, no gateway,
no orders transmitted. Run (per CLAUDE.md, from the paperbot folder):
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe -m pytest -q test_model_portfolio.py

Covers the five things the sleeve module must get right:
  * policy weight validation   -> sum-to-one, rejects bad/unknown/out-of-range policies
  * sleeve sizing math         -> capital base = net_liq * weight (pure)
  * model order-field setting  -> modelCode + account + transmit=False stamped correctly
  * drift/rebalance targets    -> per-model integer share deltas hit targets
  * fungibility / overlap      -> S0 and S8 both hold SPY in ONE account, computed per model
"""
from __future__ import annotations

import pytest

import model_portfolio as mp


# =============================================================================
# 1. POLICY WEIGHT VALIDATION
# =============================================================================
def test_valid_75_25_policy_passes():
    mp.validate_policy(mp.EXAMPLE_POLICY_75_25)          # must not raise
    # and the fluent form returns self
    assert mp.EXAMPLE_POLICY_75_25.validate() is mp.EXAMPLE_POLICY_75_25


def test_single_sleeve_100pct_is_valid():
    mp.validate_policy(mp.EXAMPLE_POLICY_S0_ONLY)        # 100% one sleeve is well-formed


def test_weights_must_sum_to_one():
    bad = mp.AllocationPolicy("DU9", {mp.MODEL_S0: 0.6, mp.MODEL_S8: 0.3})   # sums to 0.9
    with pytest.raises(ValueError, match="sum to"):
        mp.validate_policy(bad)


def test_weights_slightly_off_within_tol_ok():
    # 0.7500001 + 0.2499999 = 1.0 within POLICY_WEIGHT_TOL -> accepted (float slop).
    ok = mp.AllocationPolicy("DU9", {mp.MODEL_S0: 0.7500001, mp.MODEL_S8: 0.2499999})
    mp.validate_policy(ok)


def test_unknown_model_code_rejected():
    bad = mp.AllocationPolicy("DU9", {"NOT_A_MODEL": 1.0})
    with pytest.raises(ValueError, match="unknown modelCode"):
        mp.validate_policy(bad)


def test_negative_weight_rejected():
    bad = mp.AllocationPolicy("DU9", {mp.MODEL_S0: 1.2, mp.MODEL_S8: -0.2})
    with pytest.raises(ValueError, match="out of range"):
        mp.validate_policy(bad)


def test_nan_weight_rejected():
    bad = mp.AllocationPolicy("DU9", {mp.MODEL_S0: float("nan"), mp.MODEL_S8: 1.0})
    with pytest.raises(ValueError, match="not finite"):
        mp.validate_policy(bad)


def test_empty_policy_rejected():
    with pytest.raises(ValueError, match="no sleeve weights"):
        mp.validate_policy(mp.AllocationPolicy("DU9", {}))


def test_account_policy_map_key_mismatch_rejected():
    # map key must equal the policy's own .account (copy-paste guard).
    bad_map = {"DU_WRONG": mp.EXAMPLE_POLICY_75_25}
    with pytest.raises(ValueError, match="!= policy.account"):
        mp.validate_account_policies(bad_map)


def test_example_account_policies_all_valid():
    mp.validate_account_policies(mp.EXAMPLE_ACCOUNT_POLICIES)   # must not raise


def test_fa_master_recognized():
    assert mp.is_fa_master(mp.FA_MASTER_ACCOUNT) is True
    assert mp.is_fa_master("DU8922142") is False


# =============================================================================
# 2. SLEEVE SIZING MATH
# =============================================================================
def test_sleeve_capital_75_25():
    caps = mp.sleeve_capital(1_000_000.0, mp.EXAMPLE_POLICY_75_25)
    assert caps[mp.MODEL_S0] == pytest.approx(750_000.0)
    assert caps[mp.MODEL_S8] == pytest.approx(250_000.0)
    # the sleeve bases sum back to the account net-liq.
    assert sum(caps.values()) == pytest.approx(1_000_000.0)


def test_sleeve_capital_zero_nlv():
    caps = mp.sleeve_capital(0.0, mp.EXAMPLE_POLICY_75_25)
    assert caps == {mp.MODEL_S0: 0.0, mp.MODEL_S8: 0.0}


def test_sleeve_capital_negative_nlv_rejected():
    with pytest.raises(ValueError, match="non-negative"):
        mp.sleeve_capital(-5.0, mp.EXAMPLE_POLICY_75_25)


def test_sleeve_capital_validates_policy():
    bad = mp.AllocationPolicy("DU9", {mp.MODEL_S0: 0.6, mp.MODEL_S8: 0.3})
    with pytest.raises(ValueError):
        mp.sleeve_capital(1_000_000.0, bad)


# =============================================================================
# 3. MODEL ORDER-FIELD SETTING (modelCode + account + transmit=False)
# =============================================================================
def test_build_model_order_sets_model_and_account():
    built = mp.build_model_limit_order(
        "SPY", "BUY", 100, 250.0,
        account="DU8922142", model_code=mp.MODEL_S0, as_of="2026-07-20")
    o = built.order
    assert o.modelCode == mp.MODEL_S0
    assert o.account == "DU8922142"
    assert o.transmit is False          # this module NEVER arms
    assert o.lmtPrice == pytest.approx(250.0)
    assert o.action == "BUY"
    assert o.totalQuantity == 100
    assert o.tif == "DAY"
    assert built.symbol == "SPY"
    # deterministic ref includes the model, so overlapping-symbol legs never collide.
    assert built.order_ref == "paperbot:DU8922142:S0_ALLWEATHER:2026-07-20:BUY:SPY"


def test_two_sleeves_same_symbol_distinct_refs():
    # S0 and S8 both trade SPY in the SAME account -> distinct order refs (fungibility).
    b_s0 = mp.build_model_limit_order("SPY", "BUY", 10, 250.0, account="DU8922142",
                                      model_code=mp.MODEL_S0, as_of="2026-07-20")
    b_s8 = mp.build_model_limit_order("SPY", "BUY", 10, 250.0, account="DU8922142",
                                      model_code=mp.MODEL_S8, as_of="2026-07-20")
    assert b_s0.order_ref != b_s8.order_ref
    assert b_s0.order.modelCode == mp.MODEL_S0
    assert b_s8.order.modelCode == mp.MODEL_S8


def test_build_model_order_rejects_bad_price():
    # HARD PRICE GUARD (reused from order_router) fires before any order object is built.
    with pytest.raises(ValueError):
        mp.build_model_limit_order("SPY", "BUY", 100, 0.0, account="DU8922142",
                                   model_code=mp.MODEL_S0, as_of="2026-07-20")
    with pytest.raises(ValueError):
        mp.build_model_limit_order("SPY", "BUY", 100, float("nan"), account="DU8922142",
                                   model_code=mp.MODEL_S0, as_of="2026-07-20")


def test_build_model_order_rejects_unknown_model():
    with pytest.raises(ValueError, match="unknown modelCode"):
        mp.build_model_limit_order("SPY", "BUY", 100, 250.0, account="DU8922142",
                                   model_code="TYPO_MODEL", as_of="2026-07-20")


# =============================================================================
# 4. DRIFT / REBALANCE-TARGET MATH
# =============================================================================
def test_share_targets_floor_math():
    # one sleeve, 100% SPY @ $250, capital 750,000, reserve 0 -> floor(750000/250)=3000.
    targets = mp.model_share_targets(
        {mp.MODEL_S0: 750_000.0},
        {mp.MODEL_S0: {"SPY": 1.0}},
        {"SPY": 250.0},
        cash_reserve_pct=0.0)
    assert targets[mp.MODEL_S0]["SPY"] == 3000


def test_share_targets_applies_reserve():
    # reserve 1.5% -> investable 750000*0.985 = 738750 ; floor(738750/250)=2955.
    targets = mp.model_share_targets(
        {mp.MODEL_S0: 750_000.0},
        {mp.MODEL_S0: {"SPY": 1.0}},
        {"SPY": 250.0},
        cash_reserve_pct=0.015)
    assert targets[mp.MODEL_S0]["SPY"] == 2955


def test_deltas_buy_from_flat():
    deltas = mp.model_share_deltas(
        {mp.MODEL_S0: 750_000.0},
        {mp.MODEL_S0: {"SPY": 1.0}},
        {mp.MODEL_S0: {}},               # flat -> full BUY
        {"SPY": 250.0},
        cash_reserve_pct=0.0)
    assert deltas[mp.MODEL_S0] == {"SPY": 3000}


def test_deltas_sell_down_to_target():
    # hold 3500, target 3000 -> SELL 500.
    deltas = mp.model_share_deltas(
        {mp.MODEL_S0: 750_000.0},
        {mp.MODEL_S0: {"SPY": 1.0}},
        {mp.MODEL_S0: {"SPY": 3500}},
        {"SPY": 250.0},
        cash_reserve_pct=0.0)
    assert deltas[mp.MODEL_S0] == {"SPY": -500}


def test_deltas_dropped_symbol_fully_sold():
    # hold GLD but the model has 0 weight on it -> fully sold, regardless of target list.
    deltas = mp.model_share_deltas(
        {mp.MODEL_S0: 750_000.0},
        {mp.MODEL_S0: {"SPY": 1.0}},
        {mp.MODEL_S0: {"SPY": 3000, "GLD": 40}},
        {"SPY": 250.0, "GLD": 180.0},
        cash_reserve_pct=0.0)
    assert deltas[mp.MODEL_S0]["GLD"] == -40
    assert "SPY" not in deltas[mp.MODEL_S0]      # SPY on target -> no delta


def test_band_suppresses_small_drift_per_model():
    # target 3000, hold 2995 -> trade 5*250=1250 vs 750000 capital = 0.17% < 3% band -> suppressed.
    deltas = mp.model_share_deltas(
        {mp.MODEL_S0: 750_000.0},
        {mp.MODEL_S0: {"SPY": 1.0}},
        {mp.MODEL_S0: {"SPY": 2995}},
        {"SPY": 250.0},
        cash_reserve_pct=0.0, band_pct=0.03)
    assert deltas[mp.MODEL_S0] == {}             # inside band -> nothing


def test_band_lets_large_drift_through_per_model():
    # target 3000, hold 1000 -> trade 2000*250=500000 vs 750000 = 66% >> band -> emitted.
    deltas = mp.model_share_deltas(
        {mp.MODEL_S0: 750_000.0},
        {mp.MODEL_S0: {"SPY": 1.0}},
        {mp.MODEL_S0: {"SPY": 1000}},
        {"SPY": 250.0},
        cash_reserve_pct=0.0, band_pct=0.03)
    assert deltas[mp.MODEL_S0] == {"SPY": 2000}


def test_missing_price_yields_zero_target_no_crash():
    targets = mp.model_share_targets(
        {mp.MODEL_S0: 750_000.0},
        {mp.MODEL_S0: {"SPY": 1.0}},
        {},                              # no price for SPY
        cash_reserve_pct=0.0)
    assert targets[mp.MODEL_S0]["SPY"] == 0


# =============================================================================
# 5. FUNGIBILITY — S0 and S8 both hold SPY in ONE account, computed per model
# =============================================================================
def test_overlapping_spy_computed_per_model():
    # One account, 75/25 across S0/S8. BOTH sleeves target SPY; S0 also holds AGG.
    # Targets are computed INDEPENDENTLY per model and must never be netted together.
    caps = mp.sleeve_capital(1_000_000.0, mp.EXAMPLE_POLICY_75_25)   # S0=750k, S8=250k
    weights = {
        mp.MODEL_S0: {"SPY": 0.5, "AGG": 0.5},
        mp.MODEL_S8: {"SPY": 1.0},
    }
    current = {
        mp.MODEL_S0: {"SPY": 1000, "AGG": 2000},
        mp.MODEL_S8: {"SPY": 500},
    }
    prices = {"SPY": 250.0, "AGG": 100.0}
    deltas = mp.model_share_deltas(caps, weights, current, prices, cash_reserve_pct=0.0)

    # S0 SPY: 0.5*750000/250 = 1500 target; hold 1000 -> +500.
    assert deltas[mp.MODEL_S0]["SPY"] == 500
    # S0 AGG: 0.5*750000/100 = 3750 target; hold 2000 -> +1750.
    assert deltas[mp.MODEL_S0]["AGG"] == 1750
    # S8 SPY: 1.0*250000/250 = 1000 target; hold 500 -> +500 (SEPARATE from S0's SPY).
    assert deltas[mp.MODEL_S8]["SPY"] == 500
    # the two SPY sleeves are independent rows, not netted into one.
    assert deltas[mp.MODEL_S0]["SPY"] == 500 and deltas[mp.MODEL_S8]["SPY"] == 500


def test_plan_account_sleeves_end_to_end():
    weights = {mp.MODEL_S0: {"SPY": 1.0}, mp.MODEL_S8: {"SPY": 1.0}}
    current = {mp.MODEL_S0: {}, mp.MODEL_S8: {}}
    prices = {"SPY": 250.0}
    plan = mp.plan_account_sleeves(
        "DU8922142", 1_000_000.0, mp.EXAMPLE_POLICY_75_25,
        weights, current, prices, cash_reserve_pct=0.0)
    assert plan.account == "DU8922142"
    assert plan.sleeve_capital[mp.MODEL_S0] == pytest.approx(750_000.0)
    assert plan.sleeve_capital[mp.MODEL_S8] == pytest.approx(250_000.0)
    # S0: floor(750000/250)=3000 ; S8: floor(250000/250)=1000.
    assert plan.deltas[mp.MODEL_S0] == {"SPY": 3000}
    assert plan.deltas[mp.MODEL_S8] == {"SPY": 1000}


# =============================================================================
# 6. PURE per-model position parser (the read-wrapper's testable core)
# =============================================================================
def test_parse_model_positions():
    class _FakeContract:
        def __init__(self, sym): self.symbol = sym
    rows = [
        ("DU8922142", mp.MODEL_S0, _FakeContract("SPY"), 1500, 240.0),
        ("DU8922142", mp.MODEL_S8, _FakeContract("SPY"), 500, 248.0),
    ]
    parsed = mp.parse_model_positions(rows)
    assert len(parsed) == 2
    assert parsed[0].account == "DU8922142"
    assert parsed[0].model_code == mp.MODEL_S0
    assert parsed[0].position == 1500.0
    assert parsed[1].model_code == mp.MODEL_S8      # same symbol, separate model row
    assert parsed[0].contract.symbol == "SPY"
