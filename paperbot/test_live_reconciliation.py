"""
test_live_reconciliation.py — the S0 live-account reconciliation / corp-action guard
(armed-live prereq #3) acceptance matrix, from docs/S0_LIVE_RECONCILIATION_SPEC.md §4.

SYNTHETIC data only (fake accounts, prices, weights, a small synthetic universe). No
broker, no gateway, no orders. Proves the invariant (SPEC §2):

  On a live account, every held symbol is reconciled against BOTH the model target AND
  the strategy's known universe. The engine auto-trades only symbols the strategy knows —
  INCLUDING a legitimate rotation-out to 0% (ROTATE_OUT -> SELL) — and NEVER auto-
  liquidates an alien / corp-action / manual holding (ALIEN -> review, no order). A
  fractional-only DRIP stub (FRACTIONAL) neither trades nor perpetually alerts. A
  whitelisted cash/money-market sweep (SWEEP) is not ALIEN. Paper/backtester behavior is
  unchanged: universe=None reproduces the old single UNTRACKED status bit-for-bit.

Run:
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe -m pytest test_live_reconciliation.py -q
"""
from __future__ import annotations

import pandas as pd
import pytest

import config
import rebalance_engine as eng
import reconcile
import strategy_target


# A small synthetic universe — the set of symbols the (fake) strategy can ever hold. Kept
# independent of config.ALL_TICKERS so these tests pin the GUARD LOGIC, not the universe's
# current membership.
UNIVERSE = {"SPY", "BND", "TLT", "GLD"}


def make_target(weights: dict, prices: dict, version: str = "Balanced") -> strategy_target.Target:
    """A strategy_target.Target from plain dicts — no backtester, no data load."""
    return strategy_target.Target(
        weights=pd.Series(weights, dtype="float64"),
        prices=pd.Series(prices, dtype="float64"),
        as_of=pd.Timestamp("2026-07-20"),
        price_date=pd.Timestamp("2026-07-20"),
        version=version,
    )


def _status(lines, symbol: str) -> str:
    return next(ln.status for ln in lines if ln.symbol == symbol)


# =============================================================================
# 0. strategy.universe() accessor (reviewer decision Q2)
# =============================================================================
def test_strategy_universe_accessor_equals_all_tickers():
    from strategies import config as s_config
    from strategies.all_weather import AdaptiveAllWeather, universe

    expected = set(s_config.ALL_TICKERS)
    assert universe() == expected                       # module-level helper
    assert AdaptiveAllWeather().universe() == expected  # instance accessor matches


def test_universe_is_superset_of_every_sleeve_group():
    # Every group the portfolio assembler can draw a ticker from must be inside universe().
    from strategies import config as s_config
    from strategies.all_weather import universe

    uni = universe()
    sleeve_groups = (s_config.EQUITY_CORE + s_config.SECTORS + s_config.DEFENSIVE_ASSETS
                     + s_config.REAL_ASSETS)
    assert set(sleeve_groups) <= uni
    # the best-tbill fallback ticker is inside the universe too (never emits an alien)
    assert s_config.BENCHMARK_TBILL in uni


def test_strategy_base_universe_refuses_without_declaration():
    from strategies.base import StrategyBase, MarketState, TargetWeights

    class _Bare(StrategyBase):
        def warmup(self, prices, macro, start, end): ...
        def on_data(self, state): ...

    with pytest.raises(NotImplementedError):
        _Bare().universe()


# =============================================================================
# 1. universe=None reproduces today's UNTRACKED classification (behavior-preserving)
# =============================================================================
def test_universe_none_preserves_untracked():
    target = make_target({"SPY": 1.0}, {"SPY": 100.0, "ZZZ": 50.0})
    lines = reconcile.reconcile(target, 1_000_000, {"SPY": 9850, "ZZZ": 10},
                                tolerance_w=0.03, investable=985_000)
    assert _status(lines, "ZZZ") == "UNTRACKED"     # unchanged legacy bucket
    # and the refined statuses never appear when no universe is supplied
    assert all(ln.status not in ("ROTATE_OUT", "ALIEN", "FRACTIONAL", "SWEEP")
               for ln in lines)


def test_classify_untracked_none_is_untracked():
    assert reconcile.classify_untracked("ANY", 10.0, None) == "UNTRACKED"


# =============================================================================
# 2. the four-way split (SPEC §3.A / §4 rows)
# =============================================================================
def test_rotate_out_known_ticker_dropped_to_zero_sells():
    # Model rotates a KNOWN ticker (TLT ∈ universe) to 0%, whole shares held -> SELL.
    target = make_target({"SPY": 1.0}, {"SPY": 100.0, "TLT": 90.0})
    lines = reconcile.reconcile(target, 1_000_000, {"SPY": 9850, "TLT": 50},
                                tolerance_w=0.03, investable=985_000, universe=UNIVERSE)
    assert _status(lines, "TLT") == "ROTATE_OUT"

    plan = eng.plan_account("DU0001", "Balanced", 1_000_000, {"SPY": 9850, "TLT": 50},
                            target, band_pct=0.03, universe=UNIVERSE)
    assert plan.needs_rebalance is True
    assert plan.orders.get("TLT") == -50            # full SELL of the dropped ticker
    assert plan.alien_lines == []


def test_alien_spinoff_symbol_is_reviewed_not_sold():
    # A spun-off symbol the strategy never knew (∉ universe), 10 shares -> ALIEN, no order.
    target = make_target({"SPY": 1.0}, {"SPY": 100.0, "SPNOFF": 40.0})
    lines = reconcile.reconcile(target, 1_000_000, {"SPY": 9850, "SPNOFF": 10},
                                tolerance_w=0.03, investable=985_000, universe=UNIVERSE)
    assert _status(lines, "SPNOFF") == "ALIEN"

    plan = eng.plan_account("DU0002", "Balanced", 1_000_000, {"SPY": 9850, "SPNOFF": 10},
                            target, band_pct=0.03, universe=UNIVERSE)
    assert "SPNOFF" not in plan.orders               # NEVER auto-traded
    assert [ln.symbol for ln in plan.alien_lines] == ["SPNOFF"]
    assert plan.needs_rebalance is False             # alien alone does not breach the band


def test_ticker_rename_old_is_alien_new_is_missing_no_churn():
    # Rename: the model now wants NEWCO (∈ universe, weight>0 -> MISSING, BUY) while the
    # old renamed holding OLDCO (∉ universe) is still on the books -> ALIEN (no SELL), so
    # the rename is not churned into a taxable round-trip.
    target = make_target({"SPY": 0.5, "NEWCO": 0.5},
                         {"SPY": 100.0, "NEWCO": 100.0, "OLDCO": 100.0})
    UNI = UNIVERSE | {"NEWCO"}   # NEWCO is the strategy's known (renamed-to) symbol
    positions = {"SPY": 4925, "OLDCO": 4925}         # holds SPY on-target + the old symbol
    plan = eng.plan_account("DU0003", "Balanced", 1_000_000, positions, target,
                            band_pct=0.03, universe=UNI)
    assert _status(plan.lines, "OLDCO") == "ALIEN"
    assert _status(plan.lines, "NEWCO") == "MISSING"
    assert "OLDCO" not in plan.orders                # the old symbol is NOT sold
    assert plan.orders.get("NEWCO", 0) > 0           # the new symbol is bought
    assert [ln.symbol for ln in plan.alien_lines] == ["OLDCO"]


def test_fractional_drip_stub_is_cleared_not_suppressed():
    # v0.50.0 REVERSAL of the old suppress rule. A DRIP leaves 0.6 shares of a holding the
    # model has DROPPED. That is a position we no longer want, so it breaches the band on
    # its own and is sold in full -- rather than sitting until the account happens to trade
    # for some other reason. Sub-1-share, so the exit is the whole 0.6, not int(0.6) == 0.
    target = make_target({"SPY": 1.0}, {"SPY": 100.0, "BND": 80.0})
    lines = reconcile.reconcile(target, 1_000_000, {"SPY": 9850, "BND": 0.6},
                                tolerance_w=0.03, investable=985_000, universe=UNIVERSE)
    assert _status(lines, "BND") == "FRACTIONAL"

    plan = eng.plan_account("DU0004", "Balanced", 1_000_000, {"SPY": 9850, "BND": 0.6},
                            target, band_pct=0.03, universe=UNIVERSE)
    assert plan.needs_rebalance is True              # the stub IS a reason to trade
    assert plan.orders == {"BND": pytest.approx(-0.6)}
    assert plan.alien_lines == []


def test_a_sub_share_of_an_UNKNOWN_symbol_is_swept_too_documented_consequence():
    # DOCUMENTED CONSEQUENCE of routing every stub through FRACTIONAL. classify_untracked
    # checks the truncation seam BEFORE universe membership, so a sub-1-share spinoff of a
    # symbol we do not recognise lands in FRACTIONAL, not ALIEN -- and is therefore sold on
    # sight like any other stub. A whole-share alien is still protected for human review
    # (see test_an_alien_holding_is_still_never_auto_traded_fraction_or_not); only the
    # sub-share case is swept. If that is ever wrong, the fix is to split FRACTIONAL by
    # universe membership, NOT to re-suppress every stub.
    target = make_target({"SPY": 1.0}, {"SPY": 100.0, "WEIRD": 30.0})
    plan = eng.plan_account("DU0007", "Balanced", 1_000_000, {"SPY": 9850, "WEIRD": 0.4},
                            target, band_pct=0.03, universe=UNIVERSE)
    assert _status(plan.lines, "WEIRD") == "FRACTIONAL"
    assert plan.needs_rebalance is True
    assert plan.orders == {"WEIRD": pytest.approx(-0.4)}


def test_fractional_alien_stub_is_fractional_not_alien():
    # A sub-1-share stub of an UNKNOWN symbol is still FRACTIONAL (can't trade a sub-share
    # lot either way) — the truncation seam is checked before universe membership.
    target = make_target({"SPY": 1.0}, {"SPY": 100.0})
    lines = reconcile.reconcile(target, 1_000_000, {"SPY": 9850, "WEIRD": 0.4},
                                tolerance_w=0.03, investable=985_000, universe=UNIVERSE)
    assert _status(lines, "WEIRD") == "FRACTIONAL"


def test_cash_sweep_is_whitelisted_out_of_alien():
    # A money-market sweep fund shows as a position -> SWEEP (whitelisted), not ALIEN.
    target = make_target({"SPY": 1.0}, {"SPY": 100.0, "MMFXX": 1.0})
    lines = reconcile.reconcile(target, 1_000_000, {"SPY": 9850, "MMFXX": 5000},
                                tolerance_w=0.03, investable=985_000, universe=UNIVERSE,
                                whitelist={"MMFXX"})
    assert _status(lines, "MMFXX") == "SWEEP"
    assert not eng.band_breached(lines, 1_000_000, target, band_pct=0.03)


def test_cash_symbol_never_alien():
    # The synthetic CASH bucket symbol is never ALIEN even without an explicit whitelist.
    from investable import CASH_SYMBOL
    assert reconcile.classify_untracked(CASH_SYMBOL, 123.0, UNIVERSE) == "SWEEP"


def test_config_sweep_whitelist_honored_by_plan_account(monkeypatch):
    monkeypatch.setattr(config, "SWEEP_WHITELIST", {"MMFXX"})
    target = make_target({"SPY": 1.0}, {"SPY": 100.0, "MMFXX": 1.0})
    plan = eng.plan_account("DU0009", "Balanced", 1_000_000, {"SPY": 9850, "MMFXX": 5000},
                            target, band_pct=0.03, universe=UNIVERSE)
    assert _status(plan.lines, "MMFXX") == "SWEEP"
    assert "MMFXX" not in plan.orders
    assert plan.alien_lines == []


# =============================================================================
# 3. gate interactions (SPEC §3.B / §4 rows)
# =============================================================================
def test_alien_plus_genuine_drift_rebalances_and_leaves_alien():
    # An alien holding present AND a genuine model drift on another sleeve: the drift
    # rebalances normally; the alien is left in place and collected for review.
    target = make_target({"SPY": 0.5, "BND": 0.5}, {"SPY": 100.0, "BND": 100.0,
                                                    "SPNOFF": 30.0})
    # SPY on target (4925), BND badly under (breaches), plus a stray alien.
    positions = {"SPY": 4925, "BND": 1000, "SPNOFF": 10}
    plan = eng.plan_account("DU0005", "Balanced", 1_000_000, positions, target,
                            band_pct=0.03, universe=UNIVERSE)
    assert plan.needs_rebalance is True
    assert plan.orders.get("BND", 0) > 0             # genuine drift rebalances
    assert "SPNOFF" not in plan.orders               # alien untouched
    assert [ln.symbol for ln in plan.alien_lines] == ["SPNOFF"]


def test_alien_only_cycle_is_not_a_band_breach():
    # An account whose ONLY off-model line is an alien holding does NOT breach — it is
    # 'needs review', never a false 'band breach, no routes' page.
    target = make_target({"SPY": 1.0}, {"SPY": 100.0, "SPNOFF": 30.0})
    lines = reconcile.reconcile(target, 1_000_000, {"SPY": 9850, "SPNOFF": 500},
                                tolerance_w=0.03, investable=985_000, universe=UNIVERSE)
    # SPNOFF is 500 sh * $30 = $15k = 1.5% of NAV; a big alien must STILL not breach.
    assert not eng.band_breached(lines, 1_000_000, target, band_pct=0.03)


def test_same_alien_next_cycle_still_not_sold():
    # Re-running an unreviewed alien holding classifies ALIEN again — still no auto-sell.
    target = make_target({"SPY": 1.0}, {"SPY": 100.0, "SPNOFF": 30.0})
    positions = {"SPY": 9850, "SPNOFF": 10}
    for _ in range(2):
        plan = eng.plan_account("DU0006", "Balanced", 1_000_000, positions, target,
                                band_pct=0.03, universe=UNIVERSE)
        assert "SPNOFF" not in plan.orders
        assert [ln.symbol for ln in plan.alien_lines] == ["SPNOFF"]


def test_split_is_matched_by_weight_unchanged():
    # A split doubles shares AND halves price -> weight unchanged -> MATCHED (never the
    # untracked branch), with or without a universe.
    target = make_target({"SPY": 1.0}, {"SPY": 50.0})   # post-split price
    # investable 985,000 / 50 = 19,700 target shares; hold exactly that -> MATCHED.
    lines = reconcile.reconcile(target, 1_000_000, {"SPY": 19_700},
                                tolerance_w=0.03, investable=985_000, universe=UNIVERSE)
    assert _status(lines, "SPY") == "MATCHED"


# =============================================================================
# 4. build_plan: ALIEN produces no route; ROTATE_OUT does (end-to-end)
# =============================================================================
def test_build_plan_alien_yields_no_route_but_is_collected():
    target = make_target({"SPY": 1.0}, {"SPY": 100.0, "SPNOFF": 30.0})
    inputs = [{"account": "DU0007", "version": "Balanced", "net_liq": 1_000_000,
               "positions": {"SPY": 9850, "SPNOFF": 10}}]
    out = eng.build_plan(inputs, {"Balanced": target}, band_pct=0.03, universe=UNIVERSE)
    assert out["routes"] == []                       # alien never routes
    assert [ln.symbol for ln in out["plans"][0].alien_lines] == ["SPNOFF"]


def test_build_plan_rotate_out_routes_a_sell():
    target = make_target({"SPY": 1.0}, {"SPY": 100.0, "TLT": 90.0})
    inputs = [{"account": "DU0008", "version": "Conservative", "net_liq": 1_000_000,
               "positions": {"SPY": 9850, "TLT": 200}}]
    out = eng.build_plan(inputs, {"Conservative": target}, band_pct=0.03, universe=UNIVERSE)
    sells = [r for r in out["routes"] if r.symbol == "TLT" and r.side == "SELL"]
    assert len(sells) == 1
    assert sells[0].total_qty == 200
