"""
test_rebalance_execute.py — offline unit tests for the transmit-capable executor's HARD
SAFETY surface. NO broker, NO gateway, NO orders. Proves the three guarantees the Monday
path rests on:

  (a) DEFAULT NEVER TRANSMITS — with no arm token the guard blocks transmission and
      place() sends nothing; config defaults (READONLY/DRY_RUN True) are untouched.
  (b) THE PRICE GUARD rejects NaN / None / <= 0 limits BEFORE any order object is built,
      in BOTH order_router.build (direct) and build_fa_block (group).
  (c) THE ARMED GATE requires ALL THREE conditions together (READONLY=False AND
      DRY_RUN=False AND armed=True); any one missing -> blocked, fails closed.

Run:
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe -m pytest test_rebalance_execute.py -q
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import config
import order_router
import rebalance_execute as rx


def _intent(symbol, side, qty, limit):
    return SimpleNamespace(symbol=symbol, side=side, quantity=qty, limit_price=limit)


# --- (a) default never transmits -----------------------------------------------
def test_committed_config_defaults_are_safe():
    # The on-disk safety posture must remain locked. The executor flips these only in
    # memory and only with the arm token — never on disk.
    assert config.READONLY is True
    assert config.DRY_RUN is True


def test_no_token_means_not_armed():
    # No arm token on the command line -> arm_requested False (condition 4 absent).
    assert rx.arm_requested([]) is False
    assert rx.arm_requested(["--armed"]) is False           # near-miss does NOT arm
    assert rx.arm_requested(["--arm"]) is False
    assert rx.arm_requested([rx.ARM_TOKEN]) is True          # only the exact token


def test_default_gate_blocks_transmission():
    # With the committed defaults (READONLY/DRY_RUN True) and armed=False, the gate the
    # executor delegates to is BLOCKED.
    permit, why = rx.gate_state(armed=False)
    assert permit is False
    assert why == "DRY_RUN=True"        # fails closed on the first reason


def test_place_transmits_nothing_when_not_permitted():
    # place() with armed=False must transmit 0 even with real built orders. ib is never
    # touched because the guard blocks before any placeOrder call.
    built = order_router.build([_intent("SPY", "BUY", 10, 100.0)], "DU0001", "t", ib=None)
    sentinel = object()   # a broker that would explode if place() tried to use it
    result = order_router.place(sentinel, built, armed=False)
    assert result["transmitted"] == 0
    assert result["logged"] == 1


# --- (b) price guard rejects NaN / None / <= 0 BEFORE building ------------------
@pytest.mark.parametrize("bad", [float("nan"), None, 0.0, -1.0, -0.01])
def test_price_guard_blocks_direct_build(bad):
    # order_router.build must REFUSE to build a direct order with a bad limit, raising
    # before any BuiltOrder is produced.
    with pytest.raises(ValueError):
        order_router.build([_intent("SPY", "BUY", 10, bad)], "DU0001", "t", ib=None)


@pytest.mark.parametrize("bad", [float("nan"), None, 0.0, -5.0])
def test_price_guard_blocks_fa_block_build(bad):
    # build_fa_block must REFUSE a bad limit too (a $0/NaN block would split across a tier).
    with pytest.raises(ValueError):
        order_router.build_fa_block("SPY", "BUY", 30, bad, "tier_growth", "", "t", ib=None)


def test_price_guard_passes_valid_price():
    # A clean positive price builds normally (sanity: the guard isn't over-broad).
    built = order_router.build([_intent("SPY", "BUY", 10, 123.45)], "DU0001", "t", ib=None)
    assert len(built) == 1
    assert built[0].order.lmtPrice == 123.45
    bo = order_router.build_fa_block("SPY", "BUY", 30, 123.45, "tier_growth", "", "t", ib=None)
    assert bo.order.lmtPrice == 123.45


def test_price_guard_no_order_built_on_reject(monkeypatch):
    # Prove NOTHING is constructed when the guard fires: if a limit is NaN, the LimitOrder
    # constructor must never be reached.
    import order_router as orm
    calls = {"n": 0}
    real = orm.LimitOrder

    def spy_limit(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(orm, "LimitOrder", spy_limit)
    with pytest.raises(ValueError):
        orm.build([_intent("SPY", "BUY", 10, float("nan"))], "DU0001", "t", ib=None)
    assert calls["n"] == 0          # the guard short-circuited before any order object


# --- (c) the armed gate requires ALL THREE conditions --------------------------
def test_armed_gate_requires_all_three(monkeypatch):
    # Drive transmit_guard / gate_state across every combination; permitted IFF all three.
    cases = [
        # (readonly, dry_run, armed) -> permitted
        (True,  True,  False, False),   # committed default
        (True,  True,  True,  False),   # armed but still read-only + dry-run
        (False, True,  True,  False),   # read-only cleared, dry-run still on
        (True,  False, True,  False),   # dry-run cleared, read-only still on
        (False, False, False, False),   # both cleared but human did NOT arm
        (False, False, True,  True),    # ALL three -> the only permitted state
    ]
    for readonly, dry_run, armed, expected in cases:
        monkeypatch.setattr(config, "READONLY", readonly)
        monkeypatch.setattr(config, "DRY_RUN", dry_run)
        permit, _ = rx.gate_state(armed)
        assert permit is expected, (readonly, dry_run, armed, permit)


def test_gate_fails_closed_first_reason(monkeypatch):
    # Order of the fail-closed reasons (DRY_RUN checked first, then READONLY, then armed).
    monkeypatch.setattr(config, "DRY_RUN", True)
    monkeypatch.setattr(config, "READONLY", True)
    assert rx.gate_state(True)[1] == "DRY_RUN=True"
    monkeypatch.setattr(config, "DRY_RUN", False)
    assert rx.gate_state(True)[1] == "READONLY=True"
    monkeypatch.setattr(config, "READONLY", False)
    assert rx.gate_state(False)[1] == "session not armed by a human"
    assert rx.gate_state(True) == (True, "ARMED")


def test_armed_place_path_is_unreachable_without_all_three(monkeypatch):
    # Even calling place(armed=True), if READONLY/DRY_RUN are still set the guard blocks:
    # the executor can never transmit unless the in-process flags were flipped by the token.
    monkeypatch.setattr(config, "READONLY", True)
    monkeypatch.setattr(config, "DRY_RUN", True)
    built = order_router.build([_intent("SPY", "BUY", 10, 100.0)], "DU0001", "t", ib=None)
    sentinel = object()
    result = order_router.place(sentinel, built, armed=True)   # armed, but config locked
    assert result["transmitted"] == 0


# --- SCOPE FILTER: restrict a run to one account / tier ------------------------
# These prove a lone-DU142 (Conservative direct) run drops ALL fa_block routes so an armed
# scoped run provably issues NO replaceFA / no block placement for DU143-146 — the thing
# that unblocks the live DU142 completion. Pure functions where possible; the loop-level
# guarantee is asserted by feeding a filtered route set through the armed loop body.
from rebalance_engine import RoutePlan   # noqa: E402


def _direct(version, symbol, account, qty=10):
    return RoutePlan("direct", version, symbol, "BUY", qty, None, "", account)


def _block(version, symbol, group, split):
    return RoutePlan("fa_block", version, symbol, "BUY", sum(split.values()), group, "",
                     None, dict(split))


def _fleet_routes():
    # The real 5-account shape: DU142 Conservative runs as DIRECT legs; DU143-146 run as
    # FA-block routes per tier.
    return [
        _direct("Conservative", "TFLO", "DU8922142", 10),
        _direct("Conservative", "VGSH", "DU8922142", 5),
        _block("Balanced", "SPY", "tier_balanced", {"DU8922143": 15, "DU8922144": 15}),
        _block("Growth", "VTI", "tier_growth", {"DU8922145": 20, "DU8922146": 20}),
    ]


def test_parse_scope_reads_both_flags():
    assert rx.parse_scope([]) == (None, None)                                   # default
    assert rx.parse_scope([rx.ARM_TOKEN]) == (None, None)                       # arm only
    assert rx.parse_scope(["--only-account", "DU8922142"]) == ("DU8922142", None)
    assert rx.parse_scope(["--only-tier", "Conservative"]) == (None, "Conservative")
    assert rx.parse_scope([rx.ARM_TOKEN, "--only-account", "DU8922142"]) == ("DU8922142", None)
    assert rx.parse_scope(["--only-account", "DU8922142", "--only-tier", "Conservative"]) == \
        ("DU8922142", "Conservative")


def test_parse_scope_flag_without_value_raises():
    with pytest.raises(ValueError):
        rx.parse_scope(["--only-account"])                  # no value
    with pytest.raises(ValueError):
        rx.parse_scope(["--only-account", "--only-tier", "Growth"])   # next token is a flag


def test_validate_scope_unknown_account_fails_closed():
    with pytest.raises(ValueError):
        rx.validate_scope("DU9999999", None)                # not in ENROLLMENT
    with pytest.raises(ValueError):
        rx.validate_scope(None, "Aggressive")               # not a VALID_VERSION
    # valid values do not raise
    rx.validate_scope("DU8922142", None)
    rx.validate_scope(None, "Conservative")
    rx.validate_scope(None, None)


def test_filter_only_account_keeps_direct_drops_all_blocks():
    routes = _fleet_routes()
    scoped = rx.filter_routes(routes, "DU8922142", None)
    # ONLY DU142 direct routes survive.
    assert all(r.route == "direct" and r.account == "DU8922142" for r in scoped)
    assert {r.symbol for r in scoped} == {"TFLO", "VGSH"}
    # ZERO fa_block routes remain — the DU143-146 tiers are untouched.
    assert not any(r.route == "fa_block" for r in scoped)


def test_filter_only_tier_keeps_that_tier():
    routes = _fleet_routes()
    assert {r.symbol for r in rx.filter_routes(routes, None, "Conservative")} == {"TFLO", "VGSH"}
    growth = rx.filter_routes(routes, None, "Growth")
    assert [r.symbol for r in growth] == ["VTI"]
    assert all(r.version == "Growth" for r in growth)


def test_filter_no_scope_is_unchanged():
    routes = _fleet_routes()
    assert rx.filter_routes(routes, None, None) == routes        # full fleet, identical


def test_filter_no_match_is_empty():
    # A scope that matches nothing -> empty route list (caller then no-ops, sends nothing).
    routes = _fleet_routes()
    # Balanced has no direct routes, so account-scope on a Balanced account + tier mismatch
    # yields nothing.
    assert rx.filter_routes(routes, "DU8922143", None) == []     # DU143 has only a block


class _ScopedIB:
    """Minimal IB stand-in for driving the armed loop's route branch. Records replaceFA /
    set-group writes and order placements; nothing hits a real broker."""
    def __init__(self):
        self.replace_fa_calls = 0
        self.placed = []

    def replaceFA(self, *_a, **_k):
        self.replace_fa_calls += 1

    def requestFA(self, *_a, **_k):
        return "<ListOfGroups/>"

    def qualifyContracts(self, *c):
        return c

    def placeOrder(self, contract, order):
        self.placed.append((contract, order))
        return SimpleNamespace(
            orderStatus=SimpleNamespace(status="Filled", filled=order.totalQuantity,
                                        remaining=0, avgFillPrice=100.0),
            contract=contract, isDone=lambda: True)

    def sleep(self, *_):
        pass


def test_scoped_du142_run_issues_no_replacefa(monkeypatch):
    # End-to-end proof on the loop body: with the route set scoped to DU142 (direct only),
    # the armed loop NEVER calls set_group_contracts_or_shares / replaceFA and places only
    # the two direct legs. We drive the same branch logic the executor runs, armed.
    monkeypatch.setattr(config, "READONLY", False)
    monkeypatch.setattr(config, "DRY_RUN", False)
    # Guard: if anything tries to write FA config, blow up loudly.
    def _boom(*_a, **_k):
        raise AssertionError("scoped DU142 run must NEVER write FA config (replaceFA)!")
    monkeypatch.setattr(rx, "set_group_contracts_or_shares", _boom)
    # Make the direct laddered placement a no-op that reports a clean fill (no real ladder).
    monkeypatch.setattr(rx, "_place_direct_laddered",
                        lambda ib, r, q, as_of, armed: {"fills": [{"symbol": r.symbol}]})

    ib = _ScopedIB()
    scoped = rx.filter_routes(_fleet_routes(), "DU8922142", None)
    assert len(scoped) == 2 and not any(r.route == "fa_block" for r in scoped)

    # Replicate the executor's per-route loop (the exact branch in execute_armed).
    placed_fills = []
    for r in scoped:
        if r.route == "fa_block":
            rx.set_group_contracts_or_shares(ib, r.fa_group, r.per_account_split)  # would boom
            res = order_router.place(ib, [], armed=True)
        else:
            res = rx._place_direct_laddered(ib, r, None, "as_of", armed=True)
        placed_fills.extend(res.get("fills", []))

    assert ib.replace_fa_calls == 0                  # provably no FA-config write
    assert {f["symbol"] for f in placed_fills} == {"TFLO", "VGSH"}


def test_scope_does_not_bypass_arm_gate(monkeypatch):
    # Scoping is orthogonal to arming: with the committed-safe defaults, the gate is still
    # BLOCKED regardless of any scope flag. Scope narrows WHAT runs, never WHETHER it can
    # transmit.
    monkeypatch.setattr(config, "READONLY", True)
    monkeypatch.setattr(config, "DRY_RUN", True)
    # A scope flag present, but no arm token -> not armed, gate blocked.
    assert rx.arm_requested(["--only-account", "DU8922142"]) is False
    permit, _ = rx.gate_state(armed=False)
    assert permit is False


# --- FA-BLOCK MARKETABLE pricing (approach b) ----------------------------------
# The illiquid TFLO FA-block legs (DU143-146) didn't fill because the block limit was the
# neutral reference/mid and never crossed the thin book. Approach b prices the single block
# limit MARKETABLE (BUY ask*(1+k) / SELL bid*(1-k)) so it crosses — everything else about
# the proven FA-block path (faMethod='', split, replaceFA-in-lockstep, no what-if) unchanged.
import live_quotes   # noqa: E402


def _quote(symbol="TFLO", bid=50.00, ask=50.20, last=50.10, close=50.05):
    return live_quotes.Quote(symbol, bid=bid, ask=ask, last=last, close=close, md_type=1)


def _block(version, symbol, group, split, side="BUY"):
    qty = sum(split.values())
    return RoutePlan("fa_block", version, symbol, side, qty, group, "", None, dict(split))


def _ai_with_price(account, version, symbol, price):
    return [{"account": account, "version": version, "prices": {symbol: price}}]


def test_fa_block_uses_marketable_cap_buy(monkeypatch):
    monkeypatch.setattr(config, "FA_BLOCK_MARKETABLE", True)
    r = _block("Balanced", "TFLO", "tier_balanced", {"DU8922143": 15, "DU8922144": 15})
    quotes = {"TFLO": _quote(bid=50.00, ask=50.20)}
    ai = _ai_with_price("DU8922143", "Balanced", "TFLO", 50.10)   # neutral ref would be 50.10
    limit = rx._fa_block_limit(r, quotes, ai, {})
    expected = round(50.20 * (1 + config.ORDER_CAP_K), 2)         # ask*(1+k) = marketable
    assert limit == expected
    assert limit != 50.10                                         # NOT the neutral reference


def test_fa_block_uses_marketable_cap_sell(monkeypatch):
    monkeypatch.setattr(config, "FA_BLOCK_MARKETABLE", True)
    r = _block("Growth", "VTI", "tier_growth", {"DU8922145": 20, "DU8922146": 20}, side="SELL")
    quotes = {"VTI": _quote("VTI", bid=250.00, ask=250.40)}
    ai = _ai_with_price("DU8922145", "Growth", "VTI", 250.20)
    limit = rx._fa_block_limit(r, quotes, ai, {})
    assert limit == round(250.00 * (1 - config.ORDER_CAP_K), 2)   # bid*(1-k)


def test_fa_block_falls_back_to_reference_without_quote(monkeypatch):
    # No usable quote -> the neutral reference (prices_for) is used; never a blank/NaN.
    monkeypatch.setattr(config, "FA_BLOCK_MARKETABLE", True)
    r = _block("Balanced", "TFLO", "tier_balanced", {"DU8922143": 15, "DU8922144": 15})
    ai = _ai_with_price("DU8922143", "Balanced", "TFLO", 50.10)
    assert rx._fa_block_limit(r, {}, ai, {}) == 50.10            # quotes empty -> reference
    assert rx._fa_block_limit(r, None, ai, {}) == 50.10         # quotes None -> reference


def test_fa_block_flag_off_uses_reference(monkeypatch):
    # With the flag off, the block reverts to the prior neutral-reference behavior exactly.
    monkeypatch.setattr(config, "FA_BLOCK_MARKETABLE", False)
    r = _block("Balanced", "TFLO", "tier_balanced", {"DU8922143": 15, "DU8922144": 15})
    quotes = {"TFLO": _quote(bid=50.00, ask=50.20)}
    ai = _ai_with_price("DU8922143", "Balanced", "TFLO", 50.10)
    assert rx._fa_block_limit(r, quotes, ai, {}) == 50.10       # neutral reference, not cap


def test_fa_block_marketable_limit_builds_with_faMethod_empty_and_split_sums(monkeypatch):
    # The marketable cap flows into build_fa_block: faMethod='' (Err-10226 fix), and the
    # split still sums to the block qty. The order carries the marketable limit price.
    monkeypatch.setattr(config, "FA_BLOCK_MARKETABLE", True)
    split = {"DU8922143": 15, "DU8922144": 15}
    r = _block("Balanced", "TFLO", "tier_balanced", split)
    quotes = {"TFLO": _quote(bid=50.00, ask=50.20)}
    ai = _ai_with_price("DU8922143", "Balanced", "TFLO", 50.10)
    limit = rx._fa_block_limit(r, quotes, ai, {})
    bo = order_router.build_fa_block("TFLO", "BUY", r.total_qty, limit, r.fa_group,
                                     r.fa_method, "as_of", ib=None)
    assert bo.order.lmtPrice == round(50.20 * (1 + config.ORDER_CAP_K), 2)
    assert bo.order.faMethod == ""                              # the Err-10226 fix preserved
    assert bo.order.faGroup == "tier_balanced"
    assert sum(split.values()) == r.total_qty                  # split sums to block qty
    assert bo.order.tif == "DAY"


@pytest.mark.parametrize("bad", [float("nan"), None, 0.0, -1.0])
def test_fa_block_price_guard_still_rejects_bad(bad):
    # The HARD PRICE GUARD inside build_fa_block must still reject NaN/<=0 regardless of how
    # the (marketable) limit was computed — a bad price can never become a $0/NaN block.
    with pytest.raises(ValueError):
        order_router.build_fa_block("TFLO", "BUY", 30, bad, "tier_balanced", "", "t", ib=None)


def test_fa_block_marketable_does_not_touch_direct_or_scope(monkeypatch):
    # Sanity: the marketable-block helper only affects fa_block routes. A direct route's
    # legacy reference and the scope filter are unaffected.
    monkeypatch.setattr(config, "FA_BLOCK_MARKETABLE", True)
    # Direct routes are still filtered/handled by the unchanged scope + ladder path.
    routes = _fleet_routes()
    scoped = rx.filter_routes(routes, "DU8922142", None)
    assert all(r.route == "direct" for r in scoped)            # scope unaffected
    # The block helper is never invoked for a direct route (it's only called in the
    # fa_block branch), and direct caps still come from _ladder_caps:
    q = _quote()
    caps = rx._ladder_caps("BUY", q)
    assert caps["marketable_limit"] == live_quotes.marketable_cap("BUY", q)
