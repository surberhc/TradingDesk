"""
test_s8_runner.py — offline unit tests for the S8 scheduled entry point (Stage 5,
final stage of the 5-stage S8 build).

NO broker, NO real gateway, NO network, NO real sleeps. Proves the guardrails that
matter most for an unattended script that could (once PILOT_MODE is one day flipped)
transmit real S8 orders:
  * PILOT_MODE defaults True.
  * the due-check (due_templates) correctly matches/misses within the tolerance window,
    and a template whose ENTRY_GRID_CT is None NEVER fires, at any time of day.
  * s8_config.ACCOUNT == "TBD" -> loud refusal, ZERO gateway contact (bounded_connect
    never even called). This is the LIVE default now (s8_config.ACCOUNT is "TBD" until
    Andrew provides the S8 live-trading TEST account), so the refusal fires for real;
    tests that need to run the cycle PAST this gate monkeypatch ACCOUNT to a fake
    non-TBD string ("DU8922144").
  * a full simulated due-cycle (fake IB, fake chain snapshot, real account string)
    completes PILOT_MODE-only end to end and NEVER calls order_router.place() (nor
    anything resembling it) at any point.

CONNECTION TARGET (see s8_runner.py's own module docstring, "CONNECTION TARGET"
section): the runner's live cycle connects exclusively through `connections.ibkr_live_trade`
(the live-TRADING Gateway, port 4003, read-only by default), never `connections.ibkr_paper`
(paper, port 4002) or the earlier port-4001 live-DATA login, and never wraps its work in
`gateway_lock` (that mutex protects the shared paper Gateway, which this file does not
touch at all). Tests below mock `runner.bounded_connect` directly (the seam that would
call `connections.ibkr_live_trade`) rather than reaching into that module itself.

Run:
  cd livebot
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe -m pytest test_s8_runner.py -q
"""
from __future__ import annotations

from datetime import time as dt_time

import pandas as pd
import pytest

import s8_config
import s8_runner as runner


# --- PILOT_MODE defaults True -------------------------------------------------------
def test_pilot_mode_defaults_true():
    assert runner.PILOT_MODE is True


# --- due_templates(): matches / misses within tolerance -----------------------------
def test_due_templates_matches_within_tolerance_window():
    # Puts-80-$4 has a real grid slot at 08:45 (s8_config.ENTRY_GRID_CT).
    assert "08:45" in s8_config.ENTRY_GRID_CT["Puts-80-$4"]

    exact = runner.due_templates(dt_time(8, 45))
    assert ("Puts-80-$4", "08:45") in exact

    two_min_late = runner.due_templates(dt_time(8, 47))
    assert any(n == "Puts-80-$4" for n, _ in two_min_late)

    two_min_early = runner.due_templates(dt_time(8, 43))
    assert any(n == "Puts-80-$4" for n, _ in two_min_early)


def test_due_templates_misses_outside_tolerance_window():
    # 09:40 CT sits in Puts-80-$4's biggest grid gap (09:20 -> 10:55, both well over
    # 2min away in either direction) -- outside the +/-2min window, so it must NOT be due.
    due = runner.due_templates(dt_time(9, 40))
    assert not any(n == "Puts-80-$4" for n, _ in due)


def test_due_templates_empty_when_nothing_due():
    # 03:00 CT is well outside every template's entry grid (all real slots sit between
    # ~08:35 and ~14:05 CT per s8_config.ENTRY_GRID_CT).
    assert runner.due_templates(dt_time(3, 0)) == []


def test_due_templates_none_grid_templates_never_fire():
    # Sweep every 5 minutes across a full 24h day; a None-grid template must NEVER
    # appear in the due list at ANY time -- see s8_config.py: these templates' real
    # MATCHED-fills sample was too thin to name a grid at all.
    none_grid_templates = {name for name, grid in s8_config.ENTRY_GRID_CT.items()
                           if grid is None}
    assert none_grid_templates, "expected at least one None-grid template in s8_config"

    seen_names = set()
    for h in range(24):
        for m in range(0, 60, 5):
            for name, _slot in runner.due_templates(dt_time(h, m)):
                seen_names.add(name)

    assert not (seen_names & none_grid_templates), (
        f"None-grid template(s) fired: {seen_names & none_grid_templates}")


def test_due_templates_matched_slot_always_belongs_to_that_templates_grid():
    # Sanity: due_templates never fabricates a slot string not actually in the grid.
    for h in range(24):
        for m in range(0, 60, 5):
            for name, slot in runner.due_templates(dt_time(h, m)):
                assert slot in s8_config.ENTRY_GRID_CT[name]


# --- ACCOUNT == "TBD": loud refusal, zero gateway contact ---------------------------
# NOTE: s8_config.ACCOUNT is "TBD" by default now (the S8 live-trading TEST account has
# not been provided yet — see s8_config.py), so this refusal is the LIVE fail-closed
# behavior, not a hypothetical. These tests set it explicitly for clarity/isolation and
# assert the loud refusal fires BEFORE any gateway contact -- see s8_runner.py's module
# docstring, "ACCOUNT == TBD REFUSAL" section.
def test_account_tbd_refuses_without_connecting(monkeypatch):
    monkeypatch.setattr(s8_config, "ACCOUNT", "TBD")
    monkeypatch.setattr(runner.s8_config, "ACCOUNT", "TBD")

    def _boom(*a, **k):
        raise AssertionError("must not attempt a connection while ACCOUNT is 'TBD'")

    monkeypatch.setattr(runner, "bounded_connect", _boom)
    monkeypatch.setattr(runner.ibkr_live_trade, "connect", _boom)

    rc = runner.main()

    assert rc != 0


def test_account_tbd_checked_before_due_check(monkeypatch):
    # Even if something WOULD be due right now, the ACCOUNT=="TBD" gate must still
    # refuse first -- it's a build-time gap, not a scheduling question.
    monkeypatch.setattr(s8_config, "ACCOUNT", "TBD")
    monkeypatch.setattr(runner.s8_config, "ACCOUNT", "TBD")
    monkeypatch.setattr(runner, "due_templates", lambda now: [("Puts-80-$4", "08:45")])

    def _boom(*a, **k):
        raise AssertionError("must not connect while ACCOUNT is 'TBD', due or not")

    monkeypatch.setattr(runner, "bounded_connect", _boom)
    monkeypatch.setattr(runner.ibkr_live_trade, "connect", _boom)

    rc = runner.main()

    assert rc != 0


# --- Full simulated due-cycle: PILOT_MODE never transmits ---------------------------
class _FakeClient:
    def __init__(self):
        self._next = 1000

    def getReqId(self) -> int:
        self._next += 1
        return self._next


class _FakeIB:
    def __init__(self, summary):
        self._summary = summary
        self.disconnected = False
        self.client = _FakeClient()

    def accountSummary(self):
        # accountSummary() takes no account argument. Returns whatever this fake was
        # seeded with -- either a single-account dict (which filter_account_summary passes
        # through unchanged) or a list of per-account rows (which it filters to
        # s8_config.ACCOUNT, mirroring the real two-account live-trade login).
        return self._summary

    def disconnect(self):
        self.disconnected = True


def _synthetic_chain_snapshot() -> pd.DataFrame:
    """A simple, internally-consistent synthetic PUT ladder (same construction as
    test_s8_strategy.py's bare-snapshot test): bid=strike*0.05, ask=bid+0.05. Credit
    (short_bid - long_ask) rises linearly with width, crossing every template's target
    credit somewhere on this 5-point grid -- enough to drive pick_spread_by_credit to a
    real (non-None) pick end to end, offline."""
    rows = []
    for k in range(0, 205, 5):
        bid = k * 0.05
        ask = bid + 0.05
        rows.append({"strike": float(k), "right": "PUT", "bid": bid, "ask": ask})
    df = pd.DataFrame(rows, columns=["strike", "right", "bid", "ask"])
    df.attrs["spot"] = 100.0
    df.attrs["expiration"] = "20260713"
    df.attrs["snapshot_time"] = "2026-07-13T08:45:00.000"
    return df


def _boom_place(*a, **k):
    raise AssertionError("order_router.place must NEVER be called while PILOT_MODE=True")


def test_full_due_cycle_never_calls_order_router_place(monkeypatch):
    # A real-looking (not "TBD") account, so the cycle proceeds past the safety gate.
    monkeypatch.setattr(s8_config, "ACCOUNT", "DU8922144")
    monkeypatch.setattr(runner.s8_config, "ACCOUNT", "DU8922144")

    # Force exactly one due template this cycle (bypasses real-clock timing entirely).
    monkeypatch.setattr(runner, "due_templates", lambda now: [("Puts-80-$4", "08:45")])

    fake_summary = {"AccountType": "MARGIN", "BuyingPower": 10_000_000.0,
                    "ExcessLiquidity": 5_000_000.0}
    fake_ib = _FakeIB(fake_summary)
    # bounded_connect is the one seam that would otherwise call
    # connections.ibkr_live_trade.connect(...) against the real live-trading Gateway.
    monkeypatch.setattr(runner, "bounded_connect", lambda *a, **k: fake_ib)

    monkeypatch.setattr(runner.s8_chain, "snapshot_0dte_chain",
                        lambda ib, *a, **k: _synthetic_chain_snapshot())

    # Phase-1 rich entry capture reaches out to the live gateway for greeks/quotes; stub it
    # to a deterministic no-op so this offline cycle stays hermetic (its live behavior is
    # exercised by the s8_capture live smoke, not here). Keeps all other assertions intact.
    monkeypatch.setattr(runner.s8_capture, "capture_and_persist_entry",
                        lambda *a, **k: "fake_trade_id")

    # The hard guarantee: neither order_router.place nor place_laddered is ever called.
    monkeypatch.setattr(runner.order_router, "place", _boom_place)
    monkeypatch.setattr(runner.order_router, "place_laddered", _boom_place)

    ledger_calls = []
    monkeypatch.setattr(runner.ledger, "record_run",
                        lambda record: ledger_calls.append(record) or "fake.jsonl")

    alerts = []
    monkeypatch.setattr(runner, "_alert_email", lambda subj, lines: alerts.append((subj, lines)))

    rc = runner.main()

    assert rc == 0
    assert fake_ib.disconnected is True
    # Persisted exactly one ledger record for the cycle, marked PILOT and untransmitted.
    assert len(ledger_calls) == 1
    rec = ledger_calls[0]
    assert rec["mode"] == "s8_live_pilot"
    assert rec["n_transmitted"] == 0
    assert rec["due_templates"] == ["Puts-80-$4"]
    # The synthetic ladder is constructed so a real pick should be found and approved.
    assert rec["n_approved"] == 1
    assert rec["results"][0]["template"] == "Puts-80-$4"
    assert rec["results"][0]["pick"] is not None
    assert "would_transmit" in rec["results"][0]
    assert any("WOULD HAVE TRANSMITTED" in "\n".join(lines) for _, lines in alerts)


def test_full_due_cycle_builds_a_stop_parent_and_b2_child(monkeypatch):
    """Deeper check on the same cycle: the order group actually has the parentId link
    (child.parentId == parent.orderId) and the child sits on a DIFFERENT contract than
    the parent, per the settled cross-contract design."""
    monkeypatch.setattr(runner.s8_config, "ACCOUNT", "DU8922144")
    monkeypatch.setattr(runner, "due_templates", lambda now: [("Puts-80-$4", "08:45")])

    fake_summary = {"AccountType": "MARGIN", "BuyingPower": 10_000_000.0,
                    "ExcessLiquidity": 5_000_000.0}
    fake_ib = _FakeIB(fake_summary)
    monkeypatch.setattr(runner, "bounded_connect", lambda *a, **k: fake_ib)
    monkeypatch.setattr(runner.s8_chain, "snapshot_0dte_chain",
                        lambda ib, *a, **k: _synthetic_chain_snapshot())
    monkeypatch.setattr(runner.s8_capture, "capture_and_persist_entry",
                        lambda *a, **k: "fake_trade_id")
    monkeypatch.setattr(runner.order_router, "place", _boom_place)
    monkeypatch.setattr(runner.ledger, "record_run", lambda record: "fake.jsonl")
    monkeypatch.setattr(runner, "_alert_email", lambda *a, **k: None)

    # Reach into the pipeline directly to inspect the built order group.
    import s8_config as cfg_mod
    import s8_strategy
    chain = _synthetic_chain_snapshot()
    cfg = cfg_mod.TEMPLATES["Puts-80-$4"]
    pick = s8_strategy.pick_spread_by_credit(
        chain, "Puts-80-$4", cfg, spot=chain.attrs["spot"], expiration=chain.attrs["expiration"])
    assert pick is not None

    group = runner.build_entry_order_group(fake_ib, chain, pick, cfg, "DU8922144", 1)

    assert group.b2_close_order.parentId == group.stop_order.orderId
    assert group.short_contract.strike != group.long_contract.strike
    assert group.stop_order.transmit is False
    assert group.b2_close_order.transmit is False
    assert group.entry_short_order.transmit is False
    assert group.entry_long_order.transmit is False
    assert group.entry_short_order.action == "SELL"
    assert group.entry_long_order.action == "BUY"
    assert group.stop_order.action == "BUY"      # closes (buys back) the short leg
    assert group.b2_close_order.action == "SELL"  # closes (sells) the long leg


class _Row:
    """A minimal accountSummary row: has .account/.tag/.value like ib_async's AccountValue."""
    def __init__(self, account, tag, value):
        self.account = account
        self.tag = tag
        self.value = value


def test_filter_account_summary_picks_target_from_multi_account_login():
    rows = [
        _Row("All", "BuyingPower", "999"),
        _Row("U14438624", "AccountType", "TRUST"),
        _Row("U14438624", "BuyingPower", "378279"),
        _Row("U14438624", "NetLiquidation", "116852"),
        _Row("U14438624", "ExcessLiquidity", "94569"),
        _Row("U5721712", "AccountType", "INDIVIDUAL"),
        _Row("U5721712", "BuyingPower", "957"),
        _Row("U5721712", "NetLiquidation", "957"),
    ]
    filtered = runner.filter_account_summary(rows, "U14438624")
    assert {r.account for r in filtered} == {"U14438624"}

    import s8_risk
    pf = s8_risk.margin_preflight(filtered, width_points=50.0, realized_credit=2.0, qty=1)
    assert pf.ok, pf.reasons
    assert pf.buying_power == pytest.approx(378279)


def test_filter_account_summary_dict_passes_through():
    d = {"AccountType": "MARGIN", "BuyingPower": 1, "ExcessLiquidity": 1}
    assert runner.filter_account_summary(d, "anything") is d


def test_full_cycle_refuses_when_target_account_absent(monkeypatch):
    monkeypatch.setattr(runner.s8_config, "ACCOUNT", "U14438624")
    monkeypatch.setattr(runner, "due_templates", lambda now: [("Puts-80-$4", "08:45")])
    rows = [
        _Row("U5721712", "AccountType", "INDIVIDUAL"),
        _Row("U5721712", "BuyingPower", "957"),
        _Row("U5721712", "NetLiquidation", "957"),
    ]
    fake_ib = _FakeIB(rows)
    monkeypatch.setattr(runner, "bounded_connect", lambda *a, **k: fake_ib)
    monkeypatch.setattr(runner.s8_chain, "snapshot_0dte_chain",
                        lambda ib, *a, **k: _synthetic_chain_snapshot())
    monkeypatch.setattr(runner.order_router, "place", _boom_place)
    recs = []
    monkeypatch.setattr(runner.ledger, "record_run", lambda r: recs.append(r) or "f.jsonl")
    monkeypatch.setattr(runner, "_alert_email", lambda *a, **k: None)

    rc = runner.main()

    assert rc == 1
    assert recs and recs[0]["halted"] is True
    assert "not found" in recs[0]["error"]
    assert fake_ib.disconnected is True
