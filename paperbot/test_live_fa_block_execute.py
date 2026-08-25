"""
test_live_fa_block_execute.py — offline unit tests for the FA-GROUP BLOCK executor's HARD
SAFETY surface. NO broker, NO gateway, NO orders. Mirrors test_rebalance_execute.py's
FA-group assertions for the parameterized-target block path:

  (a) DEFAULT NEVER TRANSMITS — no arm token -> not armed -> the gate blocks.
  (b) THE FULL ARMED GATE requires READONLY=False AND DRY_RUN=False AND armed=True AND the
      gateway probe reports NOT read-only (physically armed) — any one missing -> blocked.
  (c) THE ACCOUNT WALL applies over the WHOLE split — every split member must be enrolled.
  (d) THE GROUP WRITE on a live-shaped XML preserves OTHER groups byte-for-byte, rewrites only
      the named group, and FAILS CLOSED on missing/blank XML / missing group / missing accts.
  (e) faMethod="" is preserved on the built block (the Err-10226 fix).
  (f) DEDUP blocks a duplicate: place() transmits nothing for a WORKING orderRef.
  (g) e2e ARMED loop = EXACTLY one replaceFA + one block order; PREVIEW writes/sends nothing.
  (h) the whatIf seam is OFF by design (FA_BLOCK_WHATIF_ENABLED=False, stub raises).
  (i) the PER-RUN orderRef stamp (v0.34.0) — a second run of the same block is NEW WORK and
      sends, while the same run stamp still dedups a duplicate leg.

Run:
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe -m pytest test_live_fa_block_execute.py -q
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from types import SimpleNamespace

import pytest

import config
import live_fa_block_execute as lx
import order_router
import rebalance_execute
from rebalance_engine import RoutePlan


# A realistic multi-group live GROUPS XML (three tier groups, mixed membership).
LIVE_GROUPS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<ListOfGroups>
  <Group>
    <name>tier_conservative</name>
    <ListOfAccts varName="list">
      <Account><acct>DU8922142</acct><amount>3</amount></Account>
    </ListOfAccts>
    <defaultMethod>ContractsOrShares</defaultMethod>
  </Group>
  <Group>
    <name>tier_balanced</name>
    <ListOfAccts varName="list">
      <Account><acct>DU8922143</acct><amount>1</amount></Account>
      <Account><acct>DU8922144</acct><amount>1</amount></Account>
    </ListOfAccts>
    <defaultMethod>ContractsOrShares</defaultMethod>
  </Group>
  <Group>
    <name>tier_growth</name>
    <ListOfAccts varName="list">
      <Account><acct>DU8922145</acct><amount>2</amount></Account>
      <Account><acct>DU8922146</acct><amount>2</amount></Account>
    </ListOfAccts>
    <defaultMethod>ContractsOrShares</defaultMethod>
  </Group>
</ListOfGroups>"""


def _block(version, symbol, group, split, side="BUY"):
    return RoutePlan("fa_block", version, symbol, side, sum(split.values()), group, "",
                     None, dict(split))


def _direct(version, symbol, account, qty=10):
    return RoutePlan("direct", version, symbol, "BUY", qty, None, "", account,
                     {account: qty})


def _groups_map(xml: str) -> dict:
    """{group_name: canonical serialized element} for every <Group> in the XML."""
    root = ET.fromstring(xml)
    out = {}
    for grp in root.iter():
        if grp.tag.split("}")[-1].lower() != "group":
            continue
        name = next((c.text for c in grp.iter()
                     if c.tag.split("}")[-1].lower() == "name" and c.text), None)
        if name:
            out[name.strip()] = ET.tostring(grp, encoding="unicode")
    return out


# --- (a) default never transmits -----------------------------------------------
def test_committed_config_defaults_are_safe():
    assert config.READONLY is True
    assert config.DRY_RUN is True


def test_no_token_means_not_armed():
    assert lx.arm_requested([]) is False
    assert lx.arm_requested(["--armed"]) is False       # near-miss does NOT arm
    assert lx.arm_requested(["--arm"]) is False
    assert lx.arm_requested([lx.ARM_TOKEN]) is True      # only the exact token


def test_default_gate_blocks():
    permit, why = lx.gate_state(armed=False)
    assert permit is False
    assert why == "DRY_RUN=True"                         # fails closed on the first reason


# --- (b) full armed gate: all four together ------------------------------------
def test_gate_state_requires_all_three(monkeypatch):
    cases = [
        (True,  True,  False, False),
        (True,  True,  True,  False),
        (False, True,  True,  False),
        (True,  False, True,  False),
        (False, False, False, False),
        (False, False, True,  True),   # code gate open (probe checked separately below)
    ]
    for readonly, dry_run, armed, expected in cases:
        monkeypatch.setattr(config, "READONLY", readonly)
        monkeypatch.setattr(config, "DRY_RUN", dry_run)
        permit, _ = lx.gate_state(armed)
        assert permit is expected, (readonly, dry_run, armed, permit)


def test_transmission_permitted_needs_code_gate_and_armed_gateway(monkeypatch):
    tgt = lx.PAPER_GATEWAY
    fake_ib = object()

    # Code gate closed (committed defaults) -> blocked regardless of the probe.
    monkeypatch.setattr(config, "READONLY", True)
    monkeypatch.setattr(config, "DRY_RUN", True)
    monkeypatch.setattr(lx, "probe_target_readonly", lambda ib, target=tgt, timeout=15: False)
    permit, why = lx.transmission_permitted(fake_ib, armed=True, target=tgt)
    assert permit is False and "DRY_RUN" in why

    # Code gate open but the gateway is still READ-ONLY -> blocked (not physically armed).
    monkeypatch.setattr(config, "READONLY", False)
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(lx, "probe_target_readonly", lambda ib, target=tgt, timeout=15: True)
    permit, why = lx.transmission_permitted(fake_ib, armed=True, target=tgt)
    assert permit is False and "READ-ONLY" in why

    # Code gate open AND gateway write-enabled -> the ONLY permitted state.
    monkeypatch.setattr(lx, "probe_target_readonly", lambda ib, target=tgt, timeout=15: False)
    permit, why = lx.transmission_permitted(fake_ib, armed=True, target=tgt)
    assert permit is True

    # No connection to probe -> cannot confirm physically armed -> blocked.
    permit, why = lx.transmission_permitted(None, armed=True, target=tgt)
    assert permit is False and "no gateway connection" in why


# --- (c) account wall over the WHOLE split -------------------------------------
def test_account_wall_over_split_passes_when_all_enrolled():
    allowed = ["DU8922143", "DU8922144"]
    ok, reason = lx.account_wall_over_split({"DU8922143": 15, "DU8922144": 15}, allowed)
    assert ok is True and reason == ""


def test_account_wall_over_split_fails_closed_on_any_outsider():
    allowed = ["DU8922143", "DU8922144"]
    ok, reason = lx.account_wall_over_split(
        {"DU8922143": 15, "DU9999999": 15}, allowed)          # DU999 not enrolled
    assert ok is False
    assert "DU9999999" in reason


# --- (d) group write: preserve OTHER groups byte-for-byte + fail closed ---------
def test_group_write_preserves_other_groups_byte_for_byte():
    split = {"DU8922143": 15, "DU8922144": 15}
    new_xml, diff = lx.group_write_plan(LIVE_GROUPS_XML, "tier_balanced", split)

    before = _groups_map(LIVE_GROUPS_XML)
    after = _groups_map(new_xml)
    # OTHER groups are byte-for-byte identical (their element serialization is unchanged).
    assert after["tier_conservative"] == before["tier_conservative"]
    assert after["tier_growth"] == before["tier_growth"]
    # The TARGET group changed and now carries the new split amounts.
    assert after["tier_balanced"] != before["tier_balanced"]
    root = ET.fromstring(new_xml)
    amts = {}
    for grp in root.iter():
        if grp.tag.split("}")[-1].lower() != "group":
            continue
        name = next((c.text for c in grp.iter()
                     if c.tag.split("}")[-1].lower() == "name" and c.text), None)
        if name and name.strip() == "tier_balanced":
            acct = None
            for c in grp.iter():
                t = c.tag.split("}")[-1].lower()
                if t == "acct":
                    acct = (c.text or "").strip()
                elif t == "amount" and acct:
                    amts[acct] = int(c.text)
    assert amts == {"DU8922143": 15, "DU8922144": 15}
    # A human-readable diff is produced for review.
    assert "tier_balanced" in diff


def test_group_write_diff_nonempty_and_names_target():
    _new, diff = lx.group_write_plan(LIVE_GROUPS_XML, "tier_growth",
                                     {"DU8922145": 7, "DU8922146": 9})
    assert diff.strip() != ""
    assert "after replaceFA: tier_growth" in diff


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_group_write_fails_closed_on_blank(blank):
    with pytest.raises(RuntimeError):
        lx.group_write_plan(blank, "tier_balanced", {"DU8922143": 15, "DU8922144": 15})


def test_group_write_fails_closed_on_missing_group():
    with pytest.raises(RuntimeError):
        lx.group_write_plan(LIVE_GROUPS_XML, "tier_nonexistent", {"DU8922143": 15})


def test_group_write_fails_closed_on_missing_listofaccts():
    xml = ("<ListOfGroups><Group><name>tier_x</name>"
           "<defaultMethod>ContractsOrShares</defaultMethod></Group></ListOfGroups>")
    with pytest.raises(RuntimeError):
        lx.group_write_plan(xml, "tier_x", {"DU8922143": 15})


def test_compute_group_xml_is_the_shared_definition():
    # group_write_plan's new_xml is byte-identical to the armed writer's computed XML.
    split = {"DU8922143": 4, "DU8922144": 6}
    new_xml, _diff = lx.group_write_plan(LIVE_GROUPS_XML, "tier_balanced", split)
    shared = rebalance_execute.compute_group_contracts_or_shares_xml(
        LIVE_GROUPS_XML, "tier_balanced", split)
    assert new_xml == shared


# --- (e) faMethod="" preserved on the built block ------------------------------
def test_build_fa_block_faMethod_empty_and_split_sums():
    split = {"DU8922143": 15, "DU8922144": 15}
    bo = order_router.build_fa_block("SPY", "BUY", sum(split.values()), 100.0,
                                     "tier_balanced", "", "as_of", ib=None)
    assert bo.order.faMethod == ""            # the Err-10226 fix
    assert bo.order.faGroup == "tier_balanced"
    assert bo.order.totalQuantity == sum(split.values())
    assert bo.order.tif == "DAY"


@pytest.mark.parametrize("bad", [float("nan"), None, 0.0, -1.0])
def test_block_price_guard_still_rejects_bad(bad):
    with pytest.raises(ValueError):
        order_router.build_fa_block("SPY", "BUY", 30, bad, "tier_balanced", "", "t", ib=None)


# --- fake broker for the loop-level tests --------------------------------------
class _FakeIB:
    """Minimal IB stand-in: records replaceFA writes + placed orders; FRESH-by-default dedup
    reads (no open orders / no fills). requestFA returns the live-shaped groups XML."""
    def __init__(self, working_refs=None):
        self.replace_fa_calls = 0
        self.placed = []
        self._working_refs = set(working_refs or [])

    def requestFA(self, *_a, **_k):
        return LIVE_GROUPS_XML

    def replaceFA(self, *_a, **_k):
        self.replace_fa_calls += 1

    def reqAllOpenOrders(self):
        return [SimpleNamespace(order=SimpleNamespace(orderRef=ref)) for ref in self._working_refs]

    def reqExecutions(self, *_a, **_k):
        return []

    def accountSummary(self, *_a, **_k):
        """Ample REALIZED cash for every paper sub. The two-phase cash gate (conductor #64)
        re-reads TotalCashValue between the SELL and BUY phases; these tests are about the loop
        mechanics, not the gate, so every account reports far more cash than any block needs and
        the buy sizing passes through unchanged. The gate itself (short cash, unreadable cash,
        no netting, uninvested proceeds) is covered in test_live_fa_block_execute_phases.py."""
        return [SimpleNamespace(account=a, tag="TotalCashValue", value="100000000")
                for a in ("DU8922142", "DU8922143", "DU8922144", "DU8922145", "DU8922146",
                          "DU9999999")]

    def openTrades(self):
        return []

    def cancelOrder(self, _order):
        pass

    def qualifyContracts(self, *c):
        return c

    def placeOrder(self, contract, order):
        self.placed.append((contract, order))
        return SimpleNamespace(
            contract=contract,
            orderStatus=SimpleNamespace(status="Filled", filled=order.totalQuantity,
                                        remaining=0, avgFillPrice=100.0),
            isDone=lambda: True)

    def sleep(self, *_):
        pass


def _account_inputs():
    return [
        {"account": "DU8922143", "version": "Balanced", "net_liq": 1_000_000.0,
         "positions": {}, "prices": {"SPY": 100.0}},
        {"account": "DU8922144", "version": "Balanced", "net_liq": 1_000_000.0,
         "positions": {}, "prices": {"SPY": 100.0}},
    ]


def _targets():
    return {"Balanced": SimpleNamespace(as_of="2026-08-05", prices={}, weights={})}


def _pdt_rows(value="-1", accounts=("DU8922142", "DU8922143", "DU8922144", "DU8922145",
                                    "DU8922146", "DU9999999")):
    """accountSummary rows in the shape ib_async returns, carrying DayTradesRemaining.

    The PDT gate (v0.36.0) FAILS CLOSED on a missing tag, so every lane test that drives
    execute_fa_block_routes hands it REAL rows rather than {} — that keeps the real gate
    running in those tests instead of stubbing it out. '-1' is IBKR's "unlimited / not
    PDT-restricted" (VERIFIED read-only on the live 4003 master, U14438624, 2026-08-25)."""
    return {a: [SimpleNamespace(account=a, tag="DayTradesRemaining", value=str(value)),
                SimpleNamespace(account=a, tag="NetLiquidation", value="1000000")]
            for a in accounts}


def _e2e_target():
    return lx.TargetGateway(
        name="PAPER-TEST", host="127.0.0.1", port=4002,
        clientid_consumer="paperbot_live_fa_block_exec",
        master_account="DF8922141", pin_account="DU8922143",
        enrollment={"DU8922143": "Balanced", "DU8922144": "Balanced"})


# --- (f) dedup blocks a duplicate ----------------------------------------------
def test_dedup_blocks_working_block(monkeypatch):
    # A block whose orderRef is already WORKING at the broker must transmit NOTHING.
    monkeypatch.setattr(config, "READONLY", False)
    monkeypatch.setattr(config, "DRY_RUN", False)
    ref = f"paperbot:tier_balanced:as_of:BUY:SPY"
    ib = _FakeIB(working_refs={ref})
    bo = order_router.build_fa_block("SPY", "BUY", 30, 100.0, "tier_balanced", "", "as_of",
                                     ib=None)
    assert bo.order_ref == ref
    res = order_router.place(ib, [bo], armed=True)
    assert res["transmitted"] == 0                    # dedup skipped the working block
    assert ib.placed == []


# --- (g) e2e armed loop = exactly one replaceFA + one block --------------------
def test_e2e_armed_loop_one_replacefa_one_block(monkeypatch):
    monkeypatch.setattr(config, "READONLY", False)
    monkeypatch.setattr(config, "DRY_RUN", False)
    # Isolate the loop mechanics: margin clear, backup a no-op, dedup FRESH.
    monkeypatch.setattr(lx, "margin_preflight_over_split", lambda *a, **k: (True, ""))
    monkeypatch.setattr(rebalance_execute, "backup_fa_groups", lambda ib: "fake_backup.xml")
    monkeypatch.setattr(order_router, "already_present",
                        lambda *a, **k: order_router.LegState.FRESH)
    monkeypatch.setattr(lx, "_quotes_cache", {})

    ib = _FakeIB()
    routes = [_block("Balanced", "SPY", "tier_balanced", {"DU8922143": 15, "DU8922144": 15})]
    result = lx.execute_fa_block_routes(
        ib, routes, _account_inputs(), _targets(), _e2e_target(),
        permit=True, summaries=_pdt_rows())

    assert ib.replace_fa_calls == 1                   # EXACTLY one group write
    assert len(ib.placed) == 1                        # EXACTLY one block order
    assert result["replace_fa_writes"] == 1
    assert result["n_blocks"] == 1
    # The placed order is a group order (faGroup set, faMethod="", no single account).
    _contract, order = ib.placed[0]
    assert order.faGroup == "tier_balanced"
    assert order.faMethod == ""
    assert getattr(order, "account", None) in (None, "")


def test_e2e_preview_writes_nothing_transmits_nothing(monkeypatch):
    # PREVIEW (permit=False): no replaceFA, no placeOrder, but the DIFF is surfaced.
    monkeypatch.setattr(lx, "margin_preflight_over_split", lambda *a, **k: (True, ""))
    monkeypatch.setattr(lx, "_quotes_cache", {})
    ib = _FakeIB()
    routes = [_block("Balanced", "SPY", "tier_balanced", {"DU8922143": 15, "DU8922144": 15})]
    result = lx.execute_fa_block_routes(
        ib, routes, _account_inputs(), _targets(), _e2e_target(),
        permit=False, summaries=_pdt_rows())
    assert ib.replace_fa_calls == 0                   # NO FA config written
    assert ib.placed == []                            # nothing transmitted
    assert result["n_blocks"] == 1                    # the block was previewed (build-only)


def test_e2e_armed_account_wall_refuses_outsider_no_write(monkeypatch):
    # A split member outside the enrolled roster -> the whole block is refused: no replaceFA,
    # no order, even armed+permitted.
    monkeypatch.setattr(config, "READONLY", False)
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(lx, "margin_preflight_over_split", lambda *a, **k: (True, ""))
    monkeypatch.setattr(rebalance_execute, "backup_fa_groups", lambda ib: "fake_backup.xml")
    monkeypatch.setattr(order_router, "already_present",
                        lambda *a, **k: order_router.LegState.FRESH)
    monkeypatch.setattr(lx, "_quotes_cache", {})
    ib = _FakeIB()
    # DU9999999 is NOT in the target enrollment.
    routes = [_block("Balanced", "SPY", "tier_balanced", {"DU8922143": 15, "DU9999999": 15})]
    result = lx.execute_fa_block_routes(
        ib, routes, _account_inputs(), _targets(), _e2e_target(),
        permit=True, summaries=_pdt_rows())
    assert ib.replace_fa_calls == 0
    assert ib.placed == []
    assert result["replace_fa_writes"] == 0


def test_direct_routes_are_skipped_out_of_scope(monkeypatch):
    # Direct routes (single-account tiers) are OUT OF SCOPE — never written/placed here.
    monkeypatch.setattr(config, "READONLY", False)
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(rebalance_execute, "backup_fa_groups", lambda ib: "fake_backup.xml")
    monkeypatch.setattr(lx, "_quotes_cache", {})
    ib = _FakeIB()
    routes = [_direct("Conservative", "TFLO", "DU8922142", 10)]
    result = lx.execute_fa_block_routes(
        ib, routes, _account_inputs(), _targets(), _e2e_target(),
        permit=True, summaries=_pdt_rows())
    assert result["n_blocks"] == 0
    assert result["n_direct_skipped"] == 1
    assert ib.replace_fa_calls == 0
    assert ib.placed == []


# --- (h) whatIf seam is OFF by design ------------------------------------------
def test_whatif_disabled_by_design():
    assert lx.FA_BLOCK_WHATIF_ENABLED is False
    with pytest.raises(NotImplementedError):
        lx.fa_block_whatif_preflight()


# --- margin pre-flight over the split (unlevered book clears) -------------------
def test_margin_preflight_over_split_unlevered_clears():
    # A real (small) sized plan on an unlevered book clears with zero reasons on an empty
    # summary (the _margin_preflight_ok HARD invariant). Uses the real engine + a real Target.
    import strategy_target
    t = strategy_target.current_target(version="Balanced")
    ai = [
        {"account": "DU8922143", "version": "Balanced", "net_liq": 1_000_000.0,
         "positions": {}, "prices": {s: float(t.prices.get(s, 100.0)) for s in t.weights.index}},
        {"account": "DU8922144", "version": "Balanced", "net_liq": 1_000_000.0,
         "positions": {}, "prices": {s: float(t.prices.get(s, 100.0)) for s in t.weights.index}},
    ]
    sym = list(t.weights.index)[0]
    route = _block("Balanced", sym, "tier_balanced", {"DU8922143": 5, "DU8922144": 5})
    ok, reason = lx.margin_preflight_over_split(route, ai, {"Balanced": t}, summaries={})
    assert ok is True, reason


# --- (i) PER-RUN orderRef stamp on the built block (v0.34.0) -------------------
def test_build_fa_block_stamps_the_per_run_ref():
    # The block ref is keyed on the GROUP and now ends in the run stamp. Every other field
    # stays readable, so a human can still tie the ref to group / as_of / side / symbol.
    bo = order_router.build_fa_block("SPY", "BUY", 30, 100.0, "tier_balanced", "", "as_of",
                                     ib=None, run_id="20260819T090000")
    assert bo.order_ref == "paperbot:tier_balanced:as_of:BUY:SPY:20260819T090000"
    assert bo.order.orderRef == bo.order_ref     # the wire carries the SAME string
    # Omitting run_id leaves the historical base ref byte-identical (morning lane back-compat).
    base = order_router.build_fa_block("SPY", "BUY", 30, 100.0, "tier_balanced", "", "as_of",
                                       ib=None)
    assert base.order_ref == "paperbot:tier_balanced:as_of:BUY:SPY"


def test_two_runs_of_the_same_block_are_not_deduped_into_each_other(monkeypatch):
    # THE 2026-07-28 ROOT CAUSE. Run 1's block is WORKING at the broker. Under the old
    # month-stamped ref, run 2 of the SAME group/symbol/side matched it and sent nothing.
    monkeypatch.setattr(config, "READONLY", False)
    monkeypatch.setattr(config, "DRY_RUN", False)
    run1 = "paperbot:tier_balanced:as_of:BUY:SPY:20260819T090000"
    ib = _FakeIB(working_refs={run1})
    bo = order_router.build_fa_block("SPY", "BUY", 30, 100.0, "tier_balanced", "", "as_of",
                                     ib=None, run_id="20260819T143000")
    assert bo.order_ref != run1
    res = order_router.place(ib, [bo], armed=True)
    assert res["transmitted"] == 1                 # run 2 is NEW WORK — it sends
    assert len(ib.placed) == 1


def test_within_one_run_the_same_block_ref_is_still_deduped(monkeypatch):
    # ... and the within-run guarantee is untouched: the SAME run stamp still gates.
    monkeypatch.setattr(config, "READONLY", False)
    monkeypatch.setattr(config, "DRY_RUN", False)
    ref = "paperbot:tier_balanced:as_of:BUY:SPY:20260819T090000"
    ib = _FakeIB(working_refs={ref})
    bo = order_router.build_fa_block("SPY", "BUY", 30, 100.0, "tier_balanced", "", "as_of",
                                     ib=None, run_id="20260819T090000")
    assert bo.order_ref == ref
    res = order_router.place(ib, [bo], armed=True)
    assert res["transmitted"] == 0
    assert ib.placed == []


# ========================================================================================
# (j) A.12 — the HELD-ASIDE (bond) carve-out is WIRED INTO THE BLOCK RAIL.
#
# The defect: this lane built account_inputs with only account/version/net_liq/positions/
# prices, so rebalance_engine.plan_account read sec_types=None and holding_class.carve_out
# short-circuited to "nothing held aside". A bond-holding account had its model weights sized
# against its FULL NetLiq INCLUDING the bond — silently INSIDE the target allocation, when
# owner decision D6 puts held-aside holdings OUTSIDE it and applies the model to the remaining
# managed sleeve as its own 100%. The per-account batch rail always did this correctly.
# ========================================================================================
BOND_SYM = "US912828ZT0"
BOND_MV = 95_000.0
CARVE_NET_LIQ = 1_000_000.0


class _CarveIB:
    """Broker double for build_account_inputs: each account holds one MANAGED equity plus one
    individual BOND, and the bond has NO strategy close and NO live quote (exactly like the
    real thing) — so only the broker's reported marketValue can price it."""

    def __init__(self, accounts, equity_symbol):
        self._accounts = list(accounts)
        self._equity = equity_symbol

    def accountSummary(self, *_a, **_k):
        rows = []
        for a in self._accounts:
            rows.append(SimpleNamespace(account=a, tag="NetLiquidation",
                                        value=str(CARVE_NET_LIQ)))
            rows.append(SimpleNamespace(account=a, tag="DayTradesRemaining", value="-1"))
        return rows

    def positions(self, account=None):
        return [
            SimpleNamespace(account=account, position=10.0,
                            contract=SimpleNamespace(symbol=self._equity, secType="STK")),
            SimpleNamespace(account=account, position=100_000.0,
                            contract=SimpleNamespace(symbol=BOND_SYM, secType="BOND")),
        ]

    def portfolio(self, account=None):
        return [
            SimpleNamespace(account=account, marketValue=1_000.0,
                            contract=SimpleNamespace(symbol=self._equity, secType="STK")),
            SimpleNamespace(account=account, marketValue=BOND_MV,
                            contract=SimpleNamespace(symbol=BOND_SYM, secType="BOND")),
        ]


def _carve_setup():
    """(ib, clients, targets, equity_symbol) for the carve-out tests, on a REAL Balanced
    model so the engine's sizing is the production sizing."""
    import strategy_target
    t = strategy_target.current_target(version="Balanced")
    equity = str(list(t.weights.index)[0])
    accounts = ["DU8922143", "DU8922144"]
    clients = [SimpleNamespace(number=a, version="Balanced", net_liq=CARVE_NET_LIQ)
               for a in accounts]
    return _CarveIB(accounts, equity), clients, {"Balanced": t}, equity


def test_build_account_inputs_carries_sec_types_and_values():
    # The wiring itself: the lane must hand the engine the broker's OWN contract.secType per
    # held symbol, plus the broker's reported market values. Missing either is the A.12 bug.
    ib, clients, targets, equity = _carve_setup()
    account_inputs, summaries = lx.build_account_inputs(ib, clients, targets, quotes={})

    assert len(account_inputs) == 2
    for ai in account_inputs:
        assert ai["sec_types"] == {equity: "STK", BOND_SYM: "BOND"}
        assert ai["values"][BOND_SYM] == BOND_MV
        # ...and the per-account summary rows the gates read still come back per account.
        assert summaries[ai["account"]]


def test_block_rail_carves_the_bond_out_of_the_investable_base():
    # END TO END on the block rail's own inputs: the bond's value leaves NetLiq, the model is
    # sized against the REMAINING managed sleeve, and NO block/route is ever emitted for it.
    ib, clients, targets, equity = _carve_setup()
    account_inputs, _summaries = lx.build_account_inputs(ib, clients, targets, quotes={})
    out = lx.build_plan(account_inputs, targets, tier_groups={"Balanced": "tier_balanced"})

    for plan in out["plans"]:
        assert plan.net_liq == CARVE_NET_LIQ
        assert plan.held_aside_value == pytest.approx(BOND_MV)
        assert plan.managed_net_liq == pytest.approx(CARVE_NET_LIQ - BOND_MV)
        # The model applies to the managed sleeve as its own 100% -> investable is bounded by
        # the sleeve, NEVER by the full NetLiq.
        assert plan.investable <= plan.managed_net_liq + 1e-6
        assert BOND_SYM in {h.symbol for h in plan.held_aside}
        # The bond never became a reconcile line, so it cannot be a delta of any kind.
        assert BOND_SYM not in {ln.symbol for ln in plan.lines}
        assert int(plan.orders.get(BOND_SYM, 0)) == 0

    # ...and no block and no route mentions the bond.
    assert BOND_SYM not in {b.symbol for b in out["blocks"]}
    assert BOND_SYM not in {r.symbol for r in out["routes"]}
    assert equity in {r.symbol for r in out["routes"]}       # the sleeve DOES still trade


def test_without_the_carve_out_the_bond_is_sized_inside_the_allocation():
    # THE REGRESSION ITSELF. Strip sec_types (what this lane used to send) and the SAME account
    # sizes the model against the full NetLiq — a strictly larger investable. This asserts the
    # fix changes the sized numbers, not just the reported ones.
    ib, clients, targets, _equity = _carve_setup()
    carved, _s = lx.build_account_inputs(ib, clients, targets, quotes={})
    naked = [{k: v for k, v in ai.items() if k not in ("sec_types", "values")}
             for ai in carved]

    p_carved = lx.build_plan(carved, targets)["plans"][0]
    p_naked = lx.build_plan(naked, targets)["plans"][0]
    assert p_naked.held_aside_value == 0.0
    assert p_naked.managed_net_liq == CARVE_NET_LIQ
    assert p_naked.investable > p_carved.investable


def test_plan_for_rederives_the_same_investable_as_the_engine():
    # _plan_for feeds the margin pre-flight. If it drops sec_types/values it re-plans a
    # bond-holding account against the FULL NetLiq while the routed block was sized against
    # the managed sleeve — two different investables for one account inside one run.
    ib, clients, targets, _equity = _carve_setup()
    account_inputs, _s = lx.build_account_inputs(ib, clients, targets, quotes={})
    engine_plan = lx.build_plan(account_inputs, targets)["plans"][0]
    rederived = lx._plan_for(account_inputs[0], targets)
    assert rederived.investable == pytest.approx(engine_plan.investable)
    assert rederived.managed_net_liq == pytest.approx(engine_plan.managed_net_liq)


# ========================================================================================
# (k) A.14 — the PATTERN-DAY-TRADER gate.
#
# An account IBKR has ALREADY flagged PDT with equity under $25k rejects ORDINARY orders
# regardless of order shape — the 2026-07-28 U5721712 rejection was a plain BUY of 1 USFR
# (~$50) with no offsetting sell. So the gate asks the broker's own DayTradesRemaining tag
# (already in the per-account summaries this lane holds; ZERO new broker reads), never
# "does this run create a day trade".
#
# VERIFIED READ-ONLY on the live 4003 FA master 2026-08-25: the tag comes back PER SUB-ACCOUNT
# (U14438624='-1' unrestricted, U5721712='0' blocked) and NOT on the aggregate 'All' scope.
# ========================================================================================
def _rows(value):
    """One account's accountSummary rows carrying a DayTradesRemaining of `value`."""
    return [SimpleNamespace(account="X", tag="NetLiquidation", value="957.10"),
            SimpleNamespace(account="X", tag="DayTradesRemaining", value=str(value))]


def test_day_trades_remaining_parses_the_tag():
    assert lx.day_trades_remaining(_rows(-1)) == -1
    assert lx.day_trades_remaining(_rows(0)) == 0
    assert lx.day_trades_remaining(_rows(3)) == 3
    # dict shape (s0_live.filter_account_summary) is accepted too
    assert lx.day_trades_remaining({"DayTradesRemaining": "-1"}) == -1
    # absent / blank / unparseable -> None, which the caller treats as REFUSE
    assert lx.day_trades_remaining([]) is None
    assert lx.day_trades_remaining(_rows("")) is None
    assert lx.day_trades_remaining(_rows("n/a")) is None
    assert lx.day_trades_remaining({}) is None


def test_pdt_account_ok_semantics():
    assert lx.pdt_account_ok(_rows(-1))[0] is True        # -1 = unlimited / not restricted
    assert lx.pdt_account_ok(_rows(1))[0] is True         # positive = day trades remain
    assert lx.pdt_account_ok(_rows(4))[0] is True

    ok, reason = lx.pdt_account_ok(_rows(0))              # 0 = none left -> REFUSE
    assert ok is False and "DayTradesRemaining=0" in reason

    ok, reason = lx.pdt_account_ok([])                    # missing tag -> FAIL CLOSED
    assert ok is False and "FAILING CLOSED" in reason

    ok, reason = lx.pdt_account_ok(_rows("garbage"))      # unparseable -> FAIL CLOSED
    assert ok is False and "FAILING CLOSED" in reason


def _split_summaries(mapping):
    return {a: _rows(v) if v is not None else [] for a, v in mapping.items()}


def test_pdt_preflight_over_split_clears_when_every_account_clears():
    route = _block("Balanced", "SPY", "tier_balanced", {"DU8922143": 15, "DU8922144": 15})
    s = _split_summaries({"DU8922143": -1, "DU8922144": 4})
    assert lx.pdt_preflight_over_split(route, s) == (True, "")
    assert lx.pdt_blocked_in_split(route, s) == []


def test_pdt_preflight_does_not_veto_the_block_for_one_blocked_account():
    # A $957 restricted account must NEVER veto a multi-account rebalance.
    route = _block("Balanced", "SPY", "tier_balanced", {"DU8922143": 15, "DU8922144": 15})
    s = _split_summaries({"DU8922143": -1, "DU8922144": 0})
    ok, reason = lx.pdt_preflight_over_split(route, s)
    assert ok is True and reason == ""


def test_pdt_preflight_refuses_the_block_only_when_the_split_empties():
    route = _block("Balanced", "SPY", "tier_balanced", {"DU8922143": 15, "DU8922144": 15})
    s = _split_summaries({"DU8922143": 0, "DU8922144": 0})
    ok, reason = lx.pdt_preflight_over_split(route, s)
    assert ok is False
    assert "EVERY account" in reason and "REFUSING" in reason


def test_pdt_preflight_fails_closed_on_a_missing_tag():
    # summaries={} — the tag cannot be read for EITHER account -> the split empties -> refuse.
    route = _block("Balanced", "SPY", "tier_balanced", {"DU8922143": 15, "DU8922144": 15})
    ok, reason = lx.pdt_preflight_over_split(route, {})
    assert ok is False
    blocked = lx.pdt_blocked_in_split(route, {})
    assert {b["account"] for b in blocked} == {"DU8922143", "DU8922144"}
    assert all(b["day_trades_remaining"] is None for b in blocked)


def test_pdt_drop_removes_only_the_blocked_account_and_recomputes_total_qty():
    route = _block("Balanced", "SPY", "tier_balanced",
                   {"DU8922143": 15, "DU8922144": 15, "DU8922145": 10})
    assert route.total_qty == 40
    s = _split_summaries({"DU8922143": -1, "DU8922144": 0, "DU8922145": 2})
    resized, dropped = lx.pdt_drop_blocked_from_split(route, s)

    assert resized.per_account_split == {"DU8922143": 15, "DU8922145": 10}
    assert resized.total_qty == 25                        # RECOMPUTED off the survivors
    assert [d["account"] for d in dropped] == ["DU8922144"]
    assert dropped[0]["shares"] == 15
    assert dropped[0]["day_trades_remaining"] == 0
    # The engine's ORIGINAL route is untouched (dc_replace, same as the cash re-sizer).
    assert route.per_account_split == {"DU8922143": 15, "DU8922144": 15, "DU8922145": 10}
    assert route.total_qty == 40


def test_pdt_drop_is_a_noop_when_nothing_is_blocked():
    route = _block("Balanced", "SPY", "tier_balanced", {"DU8922143": 15, "DU8922144": 15})
    resized, dropped = lx.pdt_drop_blocked_from_split(
        route, _split_summaries({"DU8922143": -1, "DU8922144": -1}))
    assert resized is route and dropped == []


# --- A.14 at the LANE level (armed path) ---------------------------------------
def _armed_lane(monkeypatch):
    monkeypatch.setattr(config, "READONLY", False)
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(lx, "margin_preflight_over_split", lambda *a, **k: (True, ""))
    monkeypatch.setattr(rebalance_execute, "backup_fa_groups", lambda ib: "fake_backup.xml")
    monkeypatch.setattr(order_router, "already_present",
                        lambda *a, **k: order_router.LegState.FRESH)
    monkeypatch.setattr(lx, "_quotes_cache", {})


def test_armed_lane_drops_only_the_pdt_account_and_writes_the_reduced_split(monkeypatch,
                                                                           capsys):
    _armed_lane(monkeypatch)
    ib = _FakeIB()
    routes = [_block("Balanced", "SPY", "tier_balanced", {"DU8922143": 15, "DU8922144": 15})]
    summaries = _pdt_rows()
    summaries["DU8922144"] = _rows(0)                      # ONE account PDT-blocked
    result = lx.execute_fa_block_routes(
        ib, routes, _account_inputs(), _targets(), _e2e_target(),
        permit=True, summaries=summaries)

    # The block still ran for the CLEARED account...
    assert ib.replace_fa_calls == 1
    assert len(ib.placed) == 1
    _c, order = ib.placed[0]
    assert float(order.totalQuantity) == 15.0              # total_qty RECOMPUTED (30 -> 15)
    # ...the group write carried ONLY the surviving account...
    res = result["buy_results"][0]
    assert res["split"] == {"DU8922143": 15}
    assert res["requested"] == 15.0
    # ...and the drop is LOUD in the report AND machine-readable in the run summary.
    assert [d["account"] for d in result["pdt_dropped"]] == ["DU8922144"]
    assert result["pdt_dropped"][0]["day_trades_remaining"] == 0
    assert result["pdt_dropped"][0]["shares"] == 15
    out = capsys.readouterr().out
    assert "PDT DROP (LOUD)" in out
    assert "PATTERN-DAY-TRADER EXCEPTIONS (LOUD" in out
    assert "DU8922144" in out


def test_armed_lane_fully_pdt_blocked_block_writes_NO_replaceFA(monkeypatch, capsys):
    # THE LOAD-BEARING ONE: a block whose whole split is PDT-blocked must be refused BEFORE
    # the group diff and before set_group_contracts_or_shares — no replaceFA, no order.
    _armed_lane(monkeypatch)
    ib = _FakeIB()
    routes = [_block("Balanced", "SPY", "tier_balanced", {"DU8922143": 15, "DU8922144": 15})]
    summaries = {"DU8922143": _rows(0), "DU8922144": _rows(0)}
    result = lx.execute_fa_block_routes(
        ib, routes, _account_inputs(), _targets(), _e2e_target(),
        permit=True, summaries=summaries)

    assert ib.replace_fa_calls == 0                        # NO FA config written
    assert ib.placed == []                                 # nothing transmitted
    assert result["replace_fa_writes"] == 0
    res = result["buy_results"][0]
    assert res["skipped"] is True and res["status"] == "SKIPPED_PDT"
    assert [b["symbol"] for b in result["pdt_refused_blocks"]] == ["SPY"]
    assert "BLOCK REFUSED" in capsys.readouterr().out


def test_armed_lane_missing_pdt_tag_fails_closed_no_replaceFA(monkeypatch):
    # Unreadable tag == refused account (fail closed), NOT a pass-through.
    _armed_lane(monkeypatch)
    ib = _FakeIB()
    routes = [_block("Balanced", "SPY", "tier_balanced", {"DU8922143": 15, "DU8922144": 15})]
    result = lx.execute_fa_block_routes(
        ib, routes, _account_inputs(), _targets(), _e2e_target(),
        permit=True, summaries={})                         # no summary rows at all

    assert ib.replace_fa_calls == 0
    assert ib.placed == []
    assert result["buy_results"][0]["status"] == "SKIPPED_PDT"


def test_clean_run_says_so_and_carries_empty_pdt_lists(monkeypatch, capsys):
    _armed_lane(monkeypatch)
    ib = _FakeIB()
    routes = [_block("Balanced", "SPY", "tier_balanced", {"DU8922143": 15, "DU8922144": 15})]
    result = lx.execute_fa_block_routes(
        ib, routes, _account_inputs(), _targets(), _e2e_target(),
        permit=True, summaries=_pdt_rows())
    assert result["pdt_dropped"] == [] and result["pdt_refused_blocks"] == []
    assert ib.replace_fa_calls == 1 and len(ib.placed) == 1
    assert "Pattern-day-trader check: CLEAN" in capsys.readouterr().out
