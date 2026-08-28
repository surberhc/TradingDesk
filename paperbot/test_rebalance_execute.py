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
  (d) THE PER-RUN ORDERREF STAMP (v0.34.0) is on every ref this lane builds — block, direct
      and laddered — so a second run of the same leg is NEW WORK (the 2026-07-28 root cause),
      a sell and a buy of one symbol in one run stay distinct, and the run id lands in the
      durable ledger record.

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


# --- ARM-GATE UNIFICATION (Step 2, #64): armed_session flips BEFORE the branch decision and
# --- RESTORES the flags AFTER the ledger write (flip-and-restore-in-finally). --------------
def test_armed_session_flips_before_permit_and_restores_after_ledger(monkeypatch):
    """An armed, offline/mocked execute_armed proves the Step-2 refactor's contract:
      (a) the ARMED branch is taken — `permit` handed to the armed body is True — i.e. the
          in-process flag flip happened BEFORE the branch decision;
      (b) `_ledger` records mode REBALANCE_EXEC_ARMED with the gate dict showing
          readonly=False / dry_run=False (the flip is LIVE when the ledger row is written);
      (c) config.READONLY / config.DRY_RUN are RESTORED to their prior True values AFTER the
          run — the flip can no longer leak past the batch to process exit.
    The gateway lock and the whole _run_armed_session body are stubbed offline (no broker, no
    order, nothing transmitted); only the arm-gate wrapping is exercised."""
    import ledger
    import rebalance_run as rr

    # Prior committed-safe posture (the on-disk default the flip must restore to).
    monkeypatch.setattr(config, "READONLY", True)
    monkeypatch.setattr(config, "DRY_RUN", True)

    # Stub the tier-model load so no real strategy data is needed.
    import pandas as pd
    fake_t = SimpleNamespace(as_of=pd.Timestamp("2026-06-30"),
                             weights=pd.Series({"SPY": 1.0}),
                             prices=pd.Series({"SPY": 100.0}))
    monkeypatch.setattr(rr, "_targets_by_version", lambda: {"Balanced": fake_t})

    # Neutralize the gateway lock — this is the SAME seam the gateway_lock suite patches, and it
    # is UNCHANGED by Step 2 (the outer lock stayed in rebalance_execute). Yield a dummy holder.
    from contextlib import contextmanager

    @contextmanager
    def _free_lock(*_a, **_k):
        yield {"purpose": "test-free-lock"}
    monkeypatch.setattr(rx, "gateway_lock", _free_lock)

    # Capture the audit-ledger record the run writes.
    captured: dict = {}
    monkeypatch.setattr(ledger, "record_run", lambda rec: captured.update(rec))

    # Spy the armed body: record the `permit` it is handed and the LIVE flag state at that
    # moment, then write the REAL ledger record (so the gate dict reflects the flipped flags).
    seen: dict = {}

    def _fake_body(armed, only_account, only_tier, permit, why, targets):
        seen["permit"] = permit
        seen["readonly_live"] = config.READONLY
        seen["dry_run_live"] = config.DRY_RUN
        rx._ledger(armed, [], [], [], "", halted=False, halt_reason="")
        return 0
    monkeypatch.setattr(rx, "_run_armed_session", _fake_body)

    rc = rx.execute_armed(armed=True)

    # (a) flip happened BEFORE the permit/branch decision -> armed branch (permit True).
    assert rc == 0
    assert seen["permit"] is True
    assert seen["readonly_live"] is False and seen["dry_run_live"] is False
    # (b) ledger row labeled ARMED with the flipped gate.
    assert captured["mode"] == "REBALANCE_EXEC_ARMED"
    assert captured["gate"]["readonly"] is False
    assert captured["gate"]["dry_run"] is False
    assert captured["gate"]["permitted"] is True
    # (c) flags RESTORED after the run — the flip-and-restore improvement.
    assert config.READONLY is True
    assert config.DRY_RUN is True


def test_dry_run_never_enters_armed_session_no_flip(monkeypatch):
    """The mirror guarantee: an UNARMED run must NOT flip the flags at all (it never enters
    armed_session). permit is False (dry branch), the ledger row is REBALANCE_EXEC_DRYRUN, and
    the flags are the committed defaults throughout and after."""
    import ledger
    import rebalance_run as rr

    monkeypatch.setattr(config, "READONLY", True)
    monkeypatch.setattr(config, "DRY_RUN", True)

    import pandas as pd
    fake_t = SimpleNamespace(as_of=pd.Timestamp("2026-06-30"),
                             weights=pd.Series({"SPY": 1.0}),
                             prices=pd.Series({"SPY": 100.0}))
    monkeypatch.setattr(rr, "_targets_by_version", lambda: {"Balanced": fake_t})

    from contextlib import contextmanager

    @contextmanager
    def _free_lock(*_a, **_k):
        yield {"purpose": "test-free-lock"}
    monkeypatch.setattr(rx, "gateway_lock", _free_lock)

    captured: dict = {}
    monkeypatch.setattr(ledger, "record_run", lambda rec: captured.update(rec))

    seen: dict = {}

    def _fake_body(armed, only_account, only_tier, permit, why, targets):
        seen["permit"] = permit
        seen["readonly_live"] = config.READONLY
        seen["dry_run_live"] = config.DRY_RUN
        rx._ledger(armed, [], [], [], "", halted=False, halt_reason="")
        return 0
    monkeypatch.setattr(rx, "_run_armed_session", _fake_body)

    rc = rx.execute_armed(armed=False)

    assert rc == 0
    assert seen["permit"] is False                      # dry branch (never armed)
    assert seen["readonly_live"] is True and seen["dry_run_live"] is True   # NO flip
    assert captured["mode"] == "REBALANCE_EXEC_DRYRUN"
    assert config.READONLY is True and config.DRY_RUN is True


# --- BANNER HONESTY: the safety banner prints INSIDE execute_armed (after the flip), so an
# --- armed run's banner reflects the transmit-capable state; a dry run's stays "DRY-RUN". --
def _offline_execute(monkeypatch):
    """Wire the SAME offline seams the Step-2 tests use (tier models + free gateway lock +
    stubbed armed body), so execute_armed runs end-to-end with no broker/order/ledger side
    effects. Callers set the flag posture and call rx.execute_armed(...)."""
    import ledger
    import rebalance_run as rr

    monkeypatch.setattr(config, "READONLY", True)
    monkeypatch.setattr(config, "DRY_RUN", True)

    import pandas as pd
    fake_t = SimpleNamespace(as_of=pd.Timestamp("2026-06-30"),
                             weights=pd.Series({"SPY": 1.0}),
                             prices=pd.Series({"SPY": 100.0}))
    monkeypatch.setattr(rr, "_targets_by_version", lambda: {"Balanced": fake_t})

    from contextlib import contextmanager

    @contextmanager
    def _free_lock(*_a, **_k):
        yield {"purpose": "test-free-lock"}
    monkeypatch.setattr(rx, "gateway_lock", _free_lock)

    monkeypatch.setattr(ledger, "record_run", lambda rec: None)

    def _fake_body(armed, only_account, only_tier, permit, why, targets):
        return 0
    monkeypatch.setattr(rx, "_run_armed_session", _fake_body)


def test_armed_banner_says_can_transmit(monkeypatch, capsys):
    """An ARMED run's banner is printed INSIDE execute_armed AFTER the in-process flip, so it
    honestly reports the transmit-capable state — not the pre-flip READONLY/DRY_RUN=True."""
    _offline_execute(monkeypatch)
    rc = rx.execute_armed(armed=True)
    out = capsys.readouterr().out
    assert rc == 0
    assert "ARMED EXECUTOR: this run CAN transmit" in out
    assert "DRY-RUN review" not in out


def test_dry_run_banner_says_dry_review(monkeypatch, capsys):
    """An UNARMED run never flips the flags, so its banner still reads DRY-RUN review."""
    _offline_execute(monkeypatch)
    rc = rx.execute_armed(armed=False)
    out = capsys.readouterr().out
    assert rc == 0
    assert "DRY-RUN review" in out
    assert "ARMED EXECUTOR: this run CAN transmit" not in out


# ============================================================================
# (d) PER-RUN ORDER-REF STAMP (v0.34.0) — SHARED with the FA-block lane.
#
# The ref used to end at the model `as_of` (for a monthly model, a MONTH stamp), so
# order_router.place's dedup read a SECOND run of the same account/group + symbol + side as
# "already done" and sent nothing — the 2026-07-28 root cause. Both lanes now mint ONE run
# stamp per run from safe_execute._run_id and put it on every ref.
# ============================================================================
def test_rebalance_lane_uses_the_shared_safe_execute_run_id():
    # ONE convention desk-wide — not a second wall-clock format grown locally here.
    import safe_execute
    assert rx._run_id is safe_execute._run_id


def test_build_all_stamps_every_ref_with_the_runs_id():
    # _build_all covers BOTH route kinds (block + direct); every ref it builds must carry the
    # run stamp, so the DRY-RUN log shows exactly what the armed path of that run would send.
    import pandas as pd
    routes = [_block("Balanced", "TFLO", "tier_balanced", {"DU8922143": 15, "DU8922144": 15}),
              _direct("Conservative", "TFLO", "DU8922142", 10)]
    targets = {"Balanced": SimpleNamespace(as_of="2026-08-19",
                                           prices=pd.Series({"TFLO": 50.10}),
                                           weights=pd.Series({"TFLO": 1.0})),
               "Conservative": SimpleNamespace(as_of="2026-08-19",
                                               prices=pd.Series({"TFLO": 50.10}),
                                               weights=pd.Series({"TFLO": 1.0}))}
    ai = [{"account": "DU8922142", "version": "Conservative", "net_liq": 100_000.0,
           "positions": {}, "prices": {"TFLO": 50.10}},
          {"account": "DU8922143", "version": "Balanced", "net_liq": 100_000.0,
           "positions": {}, "prices": {"TFLO": 50.10}},
          {"account": "DU8922144", "version": "Balanced", "net_liq": 100_000.0,
           "positions": {}, "prices": {"TFLO": 50.10}}]

    built = rx._build_all(None, routes, ai, targets, quotes={},
                          run_id="20260819T090000")
    assert len(built) == 2
    for b in built:
        assert b.order_ref.endswith(":20260819T090000")
        assert b.order.orderRef == b.order_ref
    # Block keys on the GROUP, direct keys on the ACCOUNT — both still readable in the ref.
    assert built[0].order_ref == "paperbot:tier_balanced:2026-08-19:BUY:TFLO:20260819T090000"
    assert built[1].order_ref == "paperbot:DU8922142:2026-08-19:BUY:TFLO:20260819T090000"

    # A SECOND run of the identical route set produces DIFFERENT refs — new work, not
    # "already done".
    again = rx._build_all(None, routes, ai, targets, quotes={}, run_id="20260819T143000")
    assert [b.order_ref for b in again] != [b.order_ref for b in built]
    for b, a in zip(built, again):
        assert b.order_ref.rsplit(":", 1)[0] == a.order_ref.rsplit(":", 1)[0]


def test_direct_ladder_ref_carries_the_run_stamp(monkeypatch):
    # The laddered direct path derives its gate ref itself — it must use the SAME stamp, or
    # the ladder's dedup would key on a different string than the run's other legs.
    seen = {}

    def _capture(ib, **kw):
        seen.update(kw)
        return {"transmitted": 0, "fills": []}
    monkeypatch.setattr(order_router, "place_laddered", _capture)
    route = _direct("Conservative", "TFLO", "DU8922142", 10)
    q = _quote(symbol="TFLO")
    rx._place_direct_laddered(None, route, q, "2026-08-19", armed=False,
                              run_id="20260819T090000")
    assert seen["order_ref"] == "paperbot:DU8922142:2026-08-19:BUY:TFLO:20260819T090000"
    # Omitted run_id -> the historical base ref, unchanged (the morning lane relies on this).
    seen.clear()
    rx._place_direct_laddered(None, route, q, "2026-08-19", armed=False)
    assert seen["order_ref"] == "paperbot:DU8922142:2026-08-19:BUY:TFLO"


def test_sell_and_buy_of_the_same_symbol_in_one_run_stay_distinct():
    # The sell and the buy of one symbol share the run stamp; `side` keeps them apart, so
    # neither can dedup the other.
    sell = order_router.build_fa_block("TFLO", "SELL", 30, 50.0, "tier_balanced", "",
                                       "2026-08-19", ib=None, run_id="20260819T090000")
    buy = order_router.build_fa_block("TFLO", "BUY", 30, 50.0, "tier_balanced", "",
                                      "2026-08-19", ib=None, run_id="20260819T090000")
    assert sell.order_ref != buy.order_ref
    assert sell.order_ref.rsplit(":", 1)[1] == buy.order_ref.rsplit(":", 1)[1]


def test_ledger_record_carries_the_run_id(monkeypatch):
    # AUDIT: the run id must land in the DURABLE record, so an orderRef seen at IBKR joins
    # back to the run that produced it (and vice versa).
    import ledger
    captured = {}
    monkeypatch.setattr(ledger, "record_run", lambda rec: captured.update(rec))
    rx._ledger(False, [], [], [], "", halted=False, halt_reason="",
               run_id="20260819T090000")
    assert captured["run_id"] == "20260819T090000"


# ============================================================================
# (e) THE DAILY P&L IS AN AUDIT FIGURE, NOT A BREAKER (v0.43.0).
#
# There is NO automated daily-loss halt on this desk — removed 2026-08-25 by owner
# decision. This lane still reads each account's daily P&L, best-effort, and hands it to
# risk_manager.evaluate purely so the run's ledger record carries the day's figure. An
# unreadable P&L is 0.0 and refuses nothing. (The v0.41.0 strict reader
# execution_engine.read_daily_pnl / DailyPnlUnavailable existed only to feed the removed
# breaker and went with it, along with the tests that asserted that wiring.)
# ============================================================================
class _PnlFakeIB:
    """Serves reqPnL/cancelPnL only. Anything else on it raises."""

    def __init__(self, pnl_by_account=None, raise_on_req=False):
        self._pnl = dict(pnl_by_account or {})
        self.raise_on_req = raise_on_req
        self.requested: list[str] = []
        self.cancelled: list[str] = []

    def reqPnL(self, account):
        self.requested.append(account)
        if self.raise_on_req:
            raise RuntimeError("no market-data subscription for this account")
        return SimpleNamespace(dailyPnL=self._pnl[account])

    def sleep(self, *a, **k):
        return None

    def cancelPnL(self, account):
        self.cancelled.append(account)


def _pnl_case(orders=None, net_liq=100_000.0):
    """One enrolled account with a plan that WOULD trade -> the risk guard runs on it."""
    import pandas as pd
    targets = {"Conservative": SimpleNamespace(
        as_of="2026-08-25", prices=pd.Series({"BIL": 91.0}),
        weights=pd.Series({"BIL": 1.0}))}
    account_inputs = [{"account": "DU8922142", "version": "Conservative",
                       "net_liq": net_liq, "positions": {}, "prices": {"BIL": 91.0}}]
    plans = {"DU8922142": SimpleNamespace(
        account="DU8922142",
        orders=orders if orders is not None else {"BIL": 10})}
    return account_inputs, plans, targets


def test_engine_daily_pnl_is_best_effort_and_never_raises():
    # The ONE reader is best-effort by design now: it feeds an audit record, not a breaker,
    # so an unreadable figure is 0.0 rather than an exception that could refuse an account.
    import execution_engine
    assert execution_engine._daily_pnl(_PnlFakeIB(raise_on_req=True), "DU8922142") == 0.0
    assert execution_engine._daily_pnl(
        _PnlFakeIB({"DU8922142": float("nan")}), "DU8922142") == 0.0
    ib = _PnlFakeIB({"DU8922142": -1234.5})
    assert execution_engine._daily_pnl(ib, "DU8922142") == -1234.5
    assert ib.cancelled == ["DU8922142"]          # the subscription is released


def test_an_unreadable_daily_pnl_no_longer_refuses_the_account(monkeypatch):
    # THE DECISION: an unreadable P&L used to HALT the account so the -2% breaker could not
    # be blind-sided. With no breaker there is nothing to be blind about — the account
    # trades normally and 0.0 is recorded.
    import risk_manager
    seen = {}

    def _spy(nav, daily_pnl, positions, orders, target, **kw):
        seen["daily_pnl"] = daily_pnl
        return risk_manager.RiskReport(halted=False, halt_reason="", order_verdicts=[],
                                       batch_reasons=[], approved=[])

    monkeypatch.setattr(rx.risk_manager, "evaluate", _spy)
    ai, plans, targets = _pnl_case()
    halted, reason = rx.evaluate_risk_guards(_PnlFakeIB(raise_on_req=True), ai, plans,
                                             targets)
    assert (halted, reason) == (False, "")
    assert seen["daily_pnl"] == 0.0


def test_a_catastrophic_daily_loss_does_not_halt_the_lane():
    # End-to-end through the REAL risk_manager: -30% on the day used to trip the removed
    # -2% breaker. Nothing halts on P&L any more.
    ai, plans, targets = _pnl_case()
    halted, reason = rx.evaluate_risk_guards(_PnlFakeIB({"DU8922142": -30_000.0}), ai,
                                             plans, targets)
    assert (halted, reason) == (False, "")


def test_an_account_with_no_orders_is_not_pnl_polled():
    # No trade -> nothing to guard -> no broker call (unchanged skip semantics).
    ai, plans, targets = _pnl_case(orders={})
    ib = _PnlFakeIB(raise_on_req=True)
    halted, reason = rx.evaluate_risk_guards(ib, ai, plans, targets)
    assert (halted, reason) == (False, "")
    assert ib.requested == []


# ============================================================================
# (f) THE CORP-ACTION GUARD IS WIRED INTO THIS LANE (v0.41.0).
#
# reconcile.classify_untracked needs the strategy's tradeable universe to tell a spinoff /
# rename / client holding / sweep (ALIEN, FRACTIONAL, SWEEP -> parked for review) apart
# from a symbol the model genuinely dropped (ROTATE_OUT -> sell). With universe=None every
# one of them collapsed to UNTRACKED, which ALWAYS breaches the band and produces
# delta = 0 - held: a FULL LIQUIDATION of a holding nobody decided to sell.
# ============================================================================
def _alien_case():
    import pandas as pd
    targets = {"Conservative": SimpleNamespace(
        as_of="2026-08-25", prices=pd.Series({"BIL": 91.0}),
        weights=pd.Series({"BIL": 1.0}), version="Conservative")}
    account_inputs = [{"account": "DU8922142", "version": "Conservative",
                       "net_liq": 100_000.0,
                       "positions": {"BIL": 500.0, "GDX": 100.0},
                       "prices": {"BIL": 91.0, "GDX": 30.0}}]
    return account_inputs, targets


def test_build_preview_threads_the_universe_so_an_alien_is_not_liquidated():
    import rebalance_run
    ai, targets = _alien_case()
    plan = rebalance_run.build_preview(ai, targets, universe={"BIL"})["plans"][0]
    assert "GDX" not in plan.orders                       # NOT sold
    assert any(ln.symbol == "GDX" and ln.status == "ALIEN" for ln in plan.lines)


def test_without_a_universe_the_same_alien_is_fully_liquidated():
    # The pre-fix behavior, pinned so the difference is not theoretical.
    import rebalance_run
    ai, targets = _alien_case()
    plan = rebalance_run.build_preview(ai, targets)["plans"][0]
    assert plan.orders.get("GDX") == -100                 # 0 - held == full liquidation


def test_executor_preview_passes_a_real_universe(monkeypatch):
    # The wiring itself: this lane's preview call must carry a non-None universe.
    import rebalance_run
    seen = {}

    def _spy(account_inputs, targets, **kw):
        seen.update(kw)
        return {"plans": [], "blocks": [], "routes": []}

    monkeypatch.setattr(rebalance_run, "build_preview", _spy)
    ai, targets = _alien_case()
    rx.guarded_preview(ai, targets, tier_groups={"Conservative": "tier_cons"})
    assert seen["universe"]                               # a non-empty set, not None
    assert "BIL" in seen["universe"]


def test_executor_preview_refuses_when_the_universe_cannot_be_resolved(monkeypatch):
    # FAIL CLOSED: no universe -> the corp-action guard is off -> refuse, rather than run
    # the preview with None and let a human arm what it shows.
    import recon_report
    monkeypatch.setattr(recon_report, "_strategy_universe", lambda: None)
    ai, targets = _alien_case()
    with pytest.raises(recon_report.CorpActionGuardUnavailable):
        rx.guarded_preview(ai, targets, tier_groups={"Conservative": "tier_cons"})
