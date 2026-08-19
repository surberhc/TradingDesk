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
        permit=True, summaries={})

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
        permit=False, summaries={})
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
        permit=True, summaries={})
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
        permit=True, summaries={})
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
