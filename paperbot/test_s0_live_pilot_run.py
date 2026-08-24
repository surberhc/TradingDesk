"""
test_s0_live_pilot_run.py — offline unit tests for the S0 LIVE-PILOT PREVIEW runner.

NO broker, NO real gateway, NO network. Proves the guardrails that matter most for a
runner that reads a REAL funded account (even though it is zero-transmit):
  * the target is computed BEFORE connecting — a target-computation failure connects nothing.
  * the connection is the READ-ONLY s0_live lane (connect_s0_live), and NO transmit path is
    ever exercised (the module imports no order_router; ib.placeOrder is never called).
  * every read is FILTERED to S0's own execution account (U14438624) — other accounts
    under the same login and the 'All' aggregate are never used for NetLiq or positions.
  * an absent target account -> alert + non-zero return, nothing sized.
  * plan_account is wired with account/target/prices/universe, and a band breach renders
    'WOULD BUY / WOULD SELL' lines while transmitting nothing.
  * an in-band plan reports 'no trade'; an empty (cash-only) account still sizes cleanly.

Run:
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe -m pytest test_s0_live_pilot_run.py -q
"""
from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

import s0_live_pilot_run as sp

ACCT = sp.s0_live.S0_LIVE_ACCOUNT   # S0's live execution account (U14438624 since 2026-07-28)
OTHER = "U5721712"                  # the RETIRED account, still visible under the same 4003 login

# Guard: these fixtures only test anything while they are DIFFERENT accounts. ACCT tracks
# S0_LIVE_ACCOUNT, so a future retarget that collides with OTHER would silently turn the
# read-filtering tests below into no-ops. That happened once already (S0 moved to U14438624
# on 2026-07-28, which was this file's hardcoded "other" account), so fail loudly instead.
assert OTHER != ACCT, (
    "test fixture collision: the 'other account' must not be S0's own execution account"
)


# --- fixtures -----------------------------------------------------------------------
def _fake_target():
    return sp.strategy_target.Target(
        weights=pd.Series({"SPY": 0.6, "TLT": 0.4}),
        prices=pd.Series({"SPY": 500.0, "TLT": 90.0}),
        as_of=pd.Timestamp("2026-07-27"),
        price_date=pd.Timestamp("2026-07-25"),
        version="Balanced",
    )


def _summary_row(account, tag, value):
    return SimpleNamespace(account=account, tag=tag, value=value)


def _pos_row(account, symbol, position):
    return SimpleNamespace(account=account, position=position,
                           contract=SimpleNamespace(symbol=symbol))


class _FakeIB:
    def __init__(self, summary_rows, position_rows):
        self._summary = summary_rows
        self._positions = position_rows
        self.disconnected = False

    def accountSummary(self):
        return self._summary

    def positions(self):
        return self._positions

    def disconnect(self):
        self.disconnected = True

    # If any transmit path were ever reached, fail LOUDLY (mirrors test_morning_execute_run).
    def placeOrder(self, *a, **k):
        raise AssertionError("ib.placeOrder must never be called by the preview runner")


def _fake_plan(*, breached: bool, orders: dict, alien_lines=None):
    return SimpleNamespace(
        account=ACCT, version="Balanced", net_liq=100_000.0, reserve=0.0,
        investable=98_500.0, lines=[], needs_rebalance=breached, orders=orders,
        alien_lines=alien_lines or [])


def _patch_common(monkeypatch, *, target=None):
    """Neutralize I/O boundaries; leave the real filter_account_summary/filter_positions
    (they are the behavior under test in the filtering case)."""
    monkeypatch.setattr(sp.strategy_target, "current_target",
                        lambda *a, **k: (target or _fake_target()))
    monkeypatch.setattr(sp, "_strategy_universe", lambda: {"SPY", "TLT"})
    monkeypatch.setattr(sp.live_quotes, "fetch", lambda ib, universe: {})
    monkeypatch.setattr(sp, "_write_status", lambda *a, **k: None)


# --- 1. target computed BEFORE connect ----------------------------------------------
def test_target_failure_connects_nothing(monkeypatch):
    monkeypatch.setattr(sp.strategy_target, "current_target",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stale data")))

    def _no_connect(*a, **k):
        raise AssertionError("must not connect when target computation fails")

    monkeypatch.setattr(sp.s0_live, "connect_s0_live", _no_connect)
    monkeypatch.setattr(sp, "_write_status", lambda *a, **k: None)

    rc = sp.main()

    assert rc != 0


# --- 2. connects READ-ONLY, NEVER transmits ------------------------------------------
def test_connects_readonly_and_never_transmits(monkeypatch):
    _patch_common(monkeypatch)
    spy = {"called": False, "kwargs": None}

    fake = _FakeIB([_summary_row(ACCT, "NetLiquidation", "100000")],
                   [_pos_row(ACCT, "SPY", 8)])

    def _connect(*a, **k):
        spy["called"] = True
        spy["kwargs"] = k
        return fake

    monkeypatch.setattr(sp.s0_live, "connect_s0_live", _connect)
    monkeypatch.setattr(sp.rebalance_engine, "plan_account",
                        lambda *a, **k: _fake_plan(breached=False, orders={}))
    monkeypatch.setattr(sp, "_alert_email", lambda *a, **k: None)

    rc = sp.main()

    assert rc == 0
    assert spy["called"] is True                       # the read-only s0_live lane was used
    assert fake.disconnected is True
    # Hard structural proof there is no transmit path: the module imports no order_router
    # and no arming, and never touches an order-placing method.
    assert not hasattr(sp, "order_router")
    assert not hasattr(sp, "arming")


# --- 3. every read is FILTERED to S0's own account ----------------------------------
def test_reads_are_filtered_to_individual_account(monkeypatch):
    _patch_common(monkeypatch)
    captured = {}

    fake = _FakeIB(
        summary_rows=[
            _summary_row(ACCT, "NetLiquidation", "100000"),
            _summary_row(OTHER, "NetLiquidation", "999999"),   # other account — must be ignored
            _summary_row("All", "NetLiquidation", "888"),      # aggregate — must be ignored
        ],
        position_rows=[
            _pos_row(ACCT, "SPY", 10),
            _pos_row(OTHER, "TLT", 5),                          # other account — must be ignored
        ],
    )
    monkeypatch.setattr(sp.s0_live, "connect_s0_live", lambda *a, **k: fake)

    def _plan_spy(account, ver, net_liq, positions, target, **k):
        captured["net_liq"] = net_liq
        captured["positions"] = positions
        return _fake_plan(breached=False, orders={})

    monkeypatch.setattr(sp.rebalance_engine, "plan_account", _plan_spy)
    monkeypatch.setattr(sp, "_alert_email", lambda *a, **k: None)

    rc = sp.main()

    assert rc == 0
    assert captured["net_liq"] == 100000.0             # S0's own NetLiq, not the other account's
    assert captured["positions"] == {"SPY": 10}        # only S0's own positions


# --- 4. target account NOT found under the login ------------------------------------
def test_account_not_found_alerts_and_sizes_nothing(monkeypatch):
    _patch_common(monkeypatch)

    fake = _FakeIB([_summary_row(OTHER, "NetLiquidation", "999999"),
                    _summary_row("All", "NetLiquidation", "888")], [])
    monkeypatch.setattr(sp.s0_live, "connect_s0_live", lambda *a, **k: fake)

    def _no_plan(*a, **k):
        raise AssertionError("plan_account must not run when the account is absent")

    monkeypatch.setattr(sp.rebalance_engine, "plan_account", _no_plan)

    alerts = []
    monkeypatch.setattr(sp, "_alert_email", lambda subj, lines: alerts.append((subj, lines)))

    rc = sp.main()

    assert rc == 1
    assert fake.disconnected is True
    assert any("not found" in subj.lower() or "not found" in "\n".join(lines).lower()
               for subj, lines in alerts)


# --- 5. plan_account wired; a band breach renders WOULD BUY/SELL, transmits nothing --
def test_plan_account_wired_and_breach_reports_would_trades(monkeypatch):
    target = _fake_target()
    _patch_common(monkeypatch, target=target)
    seen = {}

    fake = _FakeIB([_summary_row(ACCT, "NetLiquidation", "100000")],
                   [_pos_row(ACCT, "SPY", 3)])
    monkeypatch.setattr(sp.s0_live, "connect_s0_live", lambda *a, **k: fake)

    def _plan_spy(account, ver, net_liq, positions, tgt, prices=None, band_pct=None,
                  universe=None):
        seen["account"] = account
        seen["target_is"] = tgt is target
        seen["prices"] = prices
        seen["universe"] = universe
        return _fake_plan(breached=True, orders={"SPY": 10, "TLT": -5})

    monkeypatch.setattr(sp.rebalance_engine, "plan_account", _plan_spy)

    alerts = []
    monkeypatch.setattr(sp, "_alert_email", lambda subj, lines: alerts.append((subj, lines)))

    rc = sp.main()

    assert rc == 0
    assert seen["account"] == ACCT
    assert seen["target_is"] is True
    assert seen["prices"] and seen["prices"].get("SPY") == 500.0   # merged reference prices
    assert seen["universe"] == {"SPY", "TLT"}                      # strat universe threaded
    body = "\n".join(alerts[0][1])
    assert "WOULD BUY  SPY" in body or "WOULD BUY SPY" in body
    assert "WOULD SELL TLT" in body
    # Zero-transmit proof: fake.placeOrder would have raised; a clean rc==0 means it never ran.
    assert fake.disconnected is True


# --- 6. in-band plan reports 'no trade' ---------------------------------------------
def test_in_band_reports_no_trade(monkeypatch):
    _patch_common(monkeypatch)

    fake = _FakeIB([_summary_row(ACCT, "NetLiquidation", "100000")],
                   [_pos_row(ACCT, "SPY", 118)])
    monkeypatch.setattr(sp.s0_live, "connect_s0_live", lambda *a, **k: fake)
    monkeypatch.setattr(sp.rebalance_engine, "plan_account",
                        lambda *a, **k: _fake_plan(breached=False, orders={}))

    alerts = []
    monkeypatch.setattr(sp, "_alert_email", lambda subj, lines: alerts.append((subj, lines)))

    rc = sp.main()

    assert rc == 0
    body = "\n".join(alerts[0][1])
    assert "no-trade band" in body.lower()
    assert "in-band" in alerts[0][0]


# --- 7. empty (cash-only) account still sizes cleanly -------------------------------
def test_empty_positions_still_sizes(monkeypatch):
    _patch_common(monkeypatch)
    seen = {}

    fake = _FakeIB([_summary_row(ACCT, "NetLiquidation", "100000")], [])   # cash only
    monkeypatch.setattr(sp.s0_live, "connect_s0_live", lambda *a, **k: fake)

    def _plan_spy(account, ver, net_liq, positions, tgt, **k):
        seen["positions"] = positions
        return _fake_plan(breached=True, orders={"SPY": 120, "TLT": 44})

    monkeypatch.setattr(sp.rebalance_engine, "plan_account", _plan_spy)
    monkeypatch.setattr(sp, "_alert_email", lambda *a, **k: None)

    rc = sp.main()

    assert rc == 0
    assert seen["positions"] == {}                     # freshly funded, no holdings
    assert fake.disconnected is True
