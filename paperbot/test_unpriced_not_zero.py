"""
test_unpriced_not_zero.py — "I could not price this" must never mean "the model wants
none of this", and a guard that cannot see must refuse rather than report clear.

THE TWO DEFECTS THESE TESTS PIN (both proven by execution before the fix, v0.41.0):

  1. reconcile sized a weight-bearing symbol it could not price to target_shares = 0 —
     the SAME value "the model wants none of this" produces. rebalance_engine then emitted
     delta = 0 - held, a FULL LIQUIDATION of a position the model wanted to keep:

         plan.orders = {'AAA': -100, 'BBB': 58}      # AAA is a 50%-weight model holding

     The mirror case is quieter and more common: a model symbol the account does NOT hold,
     with no price, contributed a 0-share delta and 0.0 of trade weight, so the account was
     reported "In-spec — already conform, nothing to trade" while holding none of that
     sleeve.

  2. risk_manager.price_of turned a missing price into 0.0 and passed a NaN straight into
     comparisons that are then ALL False. Either one disables every price-based guard for
     the whole account:

         held LEGACY has a price:  batch_reasons: ['liquid reserve -20.00% < required ...']
         held LEGACY has NO price: batch_reasons: []            approved: ['SPY']

And the owner's pricing rule (v0.42.0): on the EXECUTION path IBKR is the price source.
A stale stored daily close is not a price you can trade at and is never substituted for a
quote IBKR would not give.

SYNTHETIC data only — no broker, no gateway, no orders.
Run:
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe -m pytest ^
    "C:\\TradingDesk\\paperbot\\test_unpriced_not_zero.py" -v
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
import pytest

import crm_outofspec
import execution_engine
import investable as _investable
import live_quotes
import rebalance_engine as eng
import reconcile
import risk_manager
import strategy_target


NAV = 100_000.0


def make_target(weights: dict, prices: dict, version: str = "Balanced"):
    return strategy_target.Target(
        weights=pd.Series(weights, dtype="float64"),
        prices=pd.Series(prices, dtype="float64"),
        as_of=pd.Timestamp("2026-08-25"),
        price_date=pd.Timestamp("2026-08-25"),
        version=version,
    )


@dataclass
class O:
    """execution_engine.IntendedOrder stand-in (risk_manager duck-types it)."""
    symbol: str
    side: str
    quantity: float
    limit_price: float | None = None


# =========================================================================================
# THE PRICE GATE ITSELF
# =========================================================================================
def test_usable_price_never_returns_zero_or_nan():
    # The whole defect class in one assertion: every "we could not read a price" input maps
    # to the SAME answer, and that answer is not a number.
    for bad in (None, float("nan"), 0.0, -1.0, "", "abc", [], object()):
        assert _investable.usable_price(bad) is None
    assert _investable.usable_price(12.5) == 12.5
    assert _investable.usable_price("12.5") == 12.5
    # Re-exported for the sizing callers; must be the SAME function, not a copy that drifts.
    assert reconcile.usable_price is _investable.usable_price


# =========================================================================================
# DEFECT 1 — a weight-bearing unpriced holding must NOT be sized to zero (= sell it all)
# =========================================================================================
def test_unpriced_model_holding_produces_no_sell_leg():
    # AAA is a 50%-weight model holding, 100 shares held, and IBKR gave no quote for it.
    # BEFORE: plan.orders == {'AAA': -100, ...} — a full liquidation.
    target = make_target({"AAA": 0.5, "BBB": 0.5}, {"BBB": 100.0})
    plan = eng.plan_account("U1", "Balanced", NAV, {"AAA": 100.0}, target,
                            prices={"BBB": 100.0}, universe={"AAA", "BBB"},
                            strict_prices=True)

    assert "AAA" not in plan.orders, (
        f"an unpriced 50%-weight model holding must never produce a leg; got {plan.orders}")
    assert plan.orders.get("AAA", 0) != -100
    line = next(ln for ln in plan.lines if ln.symbol == "AAA")
    assert line.status == reconcile.UNPRICED
    assert line.priced is False
    # target_shares pinned to what is held -> the delta is exactly 0 even for a consumer
    # that has never heard of the UNPRICED status.
    assert line.target_shares - int(line.actual_shares) == 0


def test_unpriced_HELD_model_holding_blocks_the_account_and_names_the_symbol():
    # It is HELD: its value is inside NetLiq but unknown, so every sibling's target would be
    # sized against a base we cannot break down -> fail closed, exactly as the held-aside
    # carve-out already does. Nothing at all is emitted.
    target = make_target({"AAA": 0.5, "BBB": 0.5}, {"BBB": 100.0})
    plan = eng.plan_account("U1", "Balanced", NAV, {"AAA": 100.0}, target,
                            prices={"BBB": 100.0}, universe={"AAA", "BBB"},
                            strict_prices=True)

    assert plan.orders == {}
    assert plan.blocked is True
    assert any("AAA" in r for r in plan.blocked_reasons), plan.blocked_reasons


def test_unpriced_UNHELD_model_symbol_does_not_read_as_in_spec():
    # THE MIRROR CASE — the quiet one. The account holds none of AAA, is otherwise perfectly
    # on model, and BEFORE read: needs_rebalance=False, orders={} -> "In-spec, nothing to
    # trade" while holding none of a 50% sleeve.
    target = make_target({"AAA": 0.5, "BBB": 0.5}, {"BBB": 100.0})
    plan = eng.plan_account("U2", "Balanced", NAV, {"BBB": 492.0}, target,
                            prices={"BBB": 100.0}, universe={"AAA", "BBB"},
                            strict_prices=True)

    assert plan.needs_attention is True, "an account missing an unpriceable sleeve is NOT in spec"
    assert plan.has_unpriced is True
    assert any("AAA" in r for r in plan.unpriced_reasons), plan.unpriced_reasons
    # ISOLATE, DO NOT BENCH: nothing of ours is tied up in a symbol we hold none of, so the
    # account is NOT blocked — the rest of the book can still be rebalanced safely.
    assert plan.blocked is False


def test_unpriced_UNHELD_symbol_still_lets_the_rest_of_the_account_trade():
    # Same account, but BBB is now badly off model. The unpriced AAA sleeve must not bench
    # the BBB correction — being too aggressive here would turn one defect into another.
    target = make_target({"AAA": 0.5, "BBB": 0.5}, {"BBB": 100.0})
    plan = eng.plan_account("U3", "Balanced", NAV, {"BBB": 100.0}, target,
                            prices={"BBB": 100.0}, universe={"AAA", "BBB"},
                            strict_prices=True)

    assert plan.orders.get("BBB", 0) > 0, "the priceable sleeve must still rebalance"
    assert "AAA" not in plan.orders
    assert plan.has_unpriced is True


def test_out_of_spec_scan_flags_the_unpriced_account():
    # The machine-readable verdict a human surface reads must say NOT in spec.
    target = make_target({"AAA": 0.5, "BBB": 0.5}, {"BBB": 100.0})
    plan = eng.plan_account("U2", "Balanced", NAV, {"BBB": 492.0}, target,
                            prices={"BBB": 100.0}, universe={"AAA", "BBB"},
                            strict_prices=True)
    verdicts = crm_outofspec.verdicts_from_plans(
        [plan], [{"account": "U2", "version": "Balanced", "net_liq": NAV}])

    assert verdicts[0]["out_of_spec"] is True
    assert verdicts[0]["unpriced"] is True
    assert verdicts[0]["n_legs"] == 0          # not in spec AND nothing it can do about it


# --- NaN must be handled identically to a missing key ------------------------------------
@pytest.mark.parametrize("prices", [
    {"BBB": 100.0},                              # AAA absent  (batch rail's shape)
    {"AAA": float("nan"), "BBB": 100.0},         # AAA NaN     (s0_live_* / fa-block shape)
    {"AAA": 0.0, "BBB": 100.0},                  # AAA zero    (the conflation itself)
])
def test_nan_and_missing_and_zero_are_the_same_answer(prices):
    target = make_target({"AAA": 0.5, "BBB": 0.5}, {"BBB": 100.0})
    plan = eng.plan_account("U1", "Balanced", NAV, {"AAA": 100.0}, target,
                            prices=prices, universe={"AAA", "BBB"}, strict_prices=True)

    assert plan.orders == {}
    assert any("AAA" in r for r in plan.blocked_reasons)
    line = next(ln for ln in plan.lines if ln.symbol == "AAA")
    assert line.status == reconcile.UNPRICED and line.priced is False


# --- THE CONTROL: legitimate rotation must STILL sell ------------------------------------
def test_weight_zero_with_a_good_price_still_sells():
    # The distinction that must survive the fix: the model genuinely wants none of OLD, and
    # we have a real quote for it. That is rotation, and it MUST still produce its sell.
    target = make_target({"NEW": 1.0}, {"NEW": 100.0, "OLD": 50.0})
    plan = eng.plan_account("U4", "Balanced", NAV, {"OLD": 200.0}, target,
                            prices={"NEW": 100.0, "OLD": 50.0},
                            universe={"NEW", "OLD"}, strict_prices=True)

    assert plan.orders.get("OLD") == -200, f"rotation must still sell; got {plan.orders}"
    line = next(ln for ln in plan.lines if ln.symbol == "OLD")
    assert line.status == "ROTATE_OUT" and line.priced is True
    assert plan.blocked is False and plan.has_unpriced is False


def test_offline_callers_are_unchanged_by_strict_prices():
    # strict_prices defaults False: the backtester / offline readouts still use the model's
    # own price series, which is its legitimate job. Only execution rails opt in.
    target = make_target({"AAA": 0.5, "BBB": 0.5}, {"AAA": 25.0, "BBB": 100.0})
    plan = eng.plan_account("U5", "Balanced", NAV, {"AAA": 100.0}, target,
                            prices={"BBB": 100.0}, universe={"AAA", "BBB"})

    line = next(ln for ln in plan.lines if ln.symbol == "AAA")
    assert line.priced is True and line.status != reconcile.UNPRICED
    assert plan.blocked is False


# =========================================================================================
# DEFECT 2 — one unpriced/NaN holding must not disable every price-based guard
# =========================================================================================
def _levered_book(legacy_prices: dict):
    """A plainly levered account: 1200 shares of LEGACY at 100 on a 100k NAV, plus a BUY."""
    target = make_target({}, legacy_prices)
    return risk_manager.evaluate(NAV, 0.0, {"LEGACY": 1200.0},
                                 [O("SPY", "BUY", 1, 100.0)], target)


def test_levered_book_is_vetoed_when_a_holding_is_priced():
    # The control: with a good price the no-leverage guard fires, as it always has.
    rep = _levered_book({"LEGACY": 100.0, "SPY": 100.0})
    assert rep.batch_reasons and "liquid reserve" in rep.batch_reasons[0]
    assert rep.approved == []


@pytest.mark.parametrize("legacy_prices, label", [
    ({"SPY": 100.0}, "missing"),
    ({"LEGACY": float("nan"), "SPY": 100.0}, "NaN"),
    ({"LEGACY": 0.0, "SPY": 100.0}, "zero"),
])
def test_levered_book_is_STILL_vetoed_when_a_holding_is_unpriced(legacy_prices, label):
    # BEFORE: batch_reasons == [] and approved == ['SPY'] — the leverage guard was OFF.
    # NOW: the guard refuses, and its reason names the symbol a human has to go get a price
    # for. A guard that cannot see must refuse, not report clear.
    rep = _levered_book(legacy_prices)
    assert rep.batch_reasons, f"the guard must refuse when it cannot value LEGACY ({label})"
    assert any("LEGACY" in r for r in rep.batch_reasons), rep.batch_reasons
    assert rep.approved == [], f"nothing may be approved past a blind guard ({label})"


def test_nan_priced_leg_does_not_disable_the_per_order_guards():
    # BEFORE: an oversized order carrying a NaN price was APPROVED — halted=False,
    # batch=[], verdict ok=True — because every comparison against NaN is False.
    target = make_target({}, {"XXX": float("nan")})
    rep = risk_manager.evaluate(NAV, 0.0, {}, [O("XXX", "BUY", 6000, None)], target)

    assert rep.approved == [], "a NaN-priced leg must never be approved"
    assert rep.order_verdicts[0].ok is False
    assert any("XXX" in r for r in rep.order_verdicts[0].reasons)


def test_oversized_order_with_good_prices_is_still_vetoed_the_same_way():
    # The control for the above: the real guard still fires with its real message.
    # NOTE: this used to also require "per-position cap" in the reasons. That cap was
    # REMOVED 2026-08-25 by owner decision, so the surviving guard here is the
    # single-order-notional sanity check, which must still fire on its own.
    target = make_target({}, {"SPY": 100.0})
    rep = risk_manager.evaluate(NAV, 0.0, {}, [O("SPY", "BUY", 6000, 100.0)], target)
    reasons = " ".join(rep.order_verdicts[0].reasons)
    assert "exceeds NAV" in reasons
    assert "per-position cap" not in reasons


def test_one_unpriceable_leg_does_not_veto_its_priceable_siblings():
    # Do not bench unnecessarily: the per-ORDER guards fail closed per leg, so a good order
    # alongside an unpriceable one is still evaluated on its own merits.
    target = make_target({}, {"GOOD": 100.0})
    rep = risk_manager.evaluate(NAV, 0.0, {},
                                [O("GOOD", "BUY", 10, 100.0), O("BAD", "BUY", 10, None)],
                                target)
    by_sym = {v.symbol: v for v in rep.order_verdicts}
    assert by_sym["GOOD"].ok is True
    assert by_sym["BAD"].ok is False
    assert [o.symbol for o in rep.approved] == ["GOOD"]


def test_zero_share_holding_does_not_bench_the_account():
    # A fully-sold position sits in `resulting` with 0.0 shares. It contributes nothing to
    # any total whatever its price, so refusing over it would be benching for no reason.
    target = make_target({}, {"SPY": 100.0})
    rep = risk_manager.evaluate(NAV, 0.0, {"CLOSED": 0.0}, [O("SPY", "BUY", 10, 100.0)],
                                target)
    assert rep.batch_reasons == []
    assert [o.symbol for o in rep.approved] == ["SPY"]


def test_a_symbol_priced_only_by_its_own_limit_is_not_treated_as_unpriced():
    # Also "do not bench unnecessarily": a symbol we are actively trading at a known limit
    # price IS priceable, even with no entry in the target's price series.
    target = make_target({}, {})
    rep = risk_manager.evaluate(NAV, 0.0, {"SPY": 10.0}, [O("SPY", "BUY", 10, 100.0)],
                                target)
    assert rep.batch_reasons == []
    assert [o.symbol for o in rep.approved] == ["SPY"]


# =========================================================================================
# THE PRICING RULE — live IBKR quote only on the execution path, and never a silent drop
# =========================================================================================
def _q(sym, **kw):
    kw.setdefault("bid", None)
    kw.setdefault("ask", None)
    kw.setdefault("last", None)
    kw.setdefault("close", None)
    return live_quotes.Quote(symbol=sym, md_type=1, **kw)


def test_execution_prices_counts_and_surfaces_every_dropped_symbol(capsys):
    # BEFORE: the batch rail's price loop dropped a symbol with NO tally, NO counter and NO
    # warning. Now every drop is returned AND printed, naming the symbol.
    quotes = {"GOOD": _q("GOOD", last=100.0)}
    prices, unquoted = live_quotes.execution_prices(quotes, ["GOOD", "NOQUOTE", "ALSOBAD"])

    assert prices == {"GOOD": 100.0}
    assert unquoted == ["ALSOBAD", "NOQUOTE"]
    live_quotes.report_unquoted(unquoted)
    out = capsys.readouterr().out
    assert "NOQUOTE" in out and "ALSOBAD" in out and "2 symbol(s)" in out


def test_execution_prices_never_substitutes_a_stored_close():
    # The owner's rule: IBKR is the price source. A symbol IBKR will not quote is absent
    # from the map — the model's stored close is not consulted here at all.
    quotes = {"STALE": _q("STALE")}          # a quote object with nothing usable in it
    prices, unquoted = live_quotes.execution_prices(quotes, ["STALE"])
    assert prices == {} and unquoted == ["STALE"]


def test_strict_prices_ignores_the_model_close_on_the_execution_path():
    # The same account, planned twice off the SAME target. Offline: the model's close prices
    # AAA and it reconciles normally. Execution: no live quote means no price, full stop.
    target = make_target({"AAA": 1.0}, {"AAA": 25.0})
    offline = eng.plan_account("U6", "Balanced", NAV, {"AAA": 100.0}, target,
                               prices={}, universe={"AAA"})
    strict = eng.plan_account("U6", "Balanced", NAV, {"AAA": 100.0}, target,
                              prices={}, universe={"AAA"}, strict_prices=True)

    assert next(ln for ln in offline.lines if ln.symbol == "AAA").priced is True
    assert next(ln for ln in strict.lines if ln.symbol == "AAA").priced is False


# --- the single-account rail carries the same rule ---------------------------------------
def test_compute_intended_orders_does_not_liquidate_an_unpriced_model_holding():
    # execution_engine had the identical conflation: `target_shares = 0  # not in target
    # (or no price) -> close the position`. With a quotes dict present this is an execution
    # path, so the stored close is not substituted and NO order may be produced for AAA.
    target = make_target({"AAA": 0.5, "BBB": 0.5}, {"AAA": 25.0, "BBB": 100.0})
    orders = execution_engine.compute_intended_orders(
        NAV, {"AAA": 100.0}, target, quotes={"BBB": _q("BBB", last=100.0)})

    assert "AAA" not in {o.symbol for o in orders}, (
        f"an unquoted model holding must not be liquidated; got "
        f"{[(o.symbol, o.side, o.quantity) for o in orders]}")


def test_compute_intended_orders_still_rotates_out_of_a_quoted_zero_weight_holding():
    # The control: weight 0 with a live quote is rotation and must still sell.
    target = make_target({"NEW": 1.0}, {"NEW": 100.0, "OLD": 50.0})
    orders = execution_engine.compute_intended_orders(
        NAV, {"OLD": 200.0}, target,
        quotes={"NEW": _q("NEW", last=100.0), "OLD": _q("OLD", last=50.0)})

    old = next(o for o in orders if o.symbol == "OLD")
    assert old.side == "SELL" and old.quantity == 200


def test_compute_intended_orders_never_emits_a_zero_limit_price():
    # The other zero-for-unpriced hole in the same function: `limit = ... else 0.0`.
    target = make_target({"AAA": 1.0}, {"AAA": float("nan")})
    orders = execution_engine.compute_intended_orders(NAV, {"AAA": 100.0}, target,
                                                      quotes={})
    assert all(o.limit_price and o.limit_price > 0 for o in orders)
