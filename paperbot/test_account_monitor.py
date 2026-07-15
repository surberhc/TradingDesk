"""
test_account_monitor.py — Slice 5: the per-account MONITOR BRAIN (pure, propose-only).

Proves `decide()` returns the right Verdict for each trigger, and ENFORCES the propose-
only boundary: the module (and its module-level imports) cannot reach any order-transmit
or arming path. SYNTHETIC fixtures only — no broker, no gateway, nothing transmitted; the
withdrawal path is exercised with a fake in-test distribution schedule (never real client
data — cashflows.SCHEDULE stays empty in committed code).

Run:
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe -m pytest test_account_monitor.py -q
"""
from __future__ import annotations

import inspect
from datetime import date

import pandas as pd
import pytest

import account_monitor as mon
import accounts
import cashflows
import config


# --- synthetic helpers --------------------------------------------------------
def make_target(weights: dict, prices: dict, version: str = "Balanced"):
    """A strategy_target.Target from plain dicts — no backtester, no data load.
    reconcile reads only .weights (index + get) and .prices (get)."""
    import strategy_target
    return strategy_target.Target(
        weights=pd.Series(weights, dtype="float64"),
        prices=pd.Series(prices, dtype="float64"),
        as_of=pd.Timestamp("2026-06-30"),
        price_date=pd.Timestamp("2026-06-30"),
        version=version,
    )


def make_state(positions: dict, *, net_liq: float = 1_000_000.0, cash: float = 50_000.0,
               schedule: list | None = None, weights=None, prices=None,
               version: str = "Balanced") -> mon.AccountState:
    target = make_target(weights or {"SPY": 1.0}, prices or {"SPY": 100.0}, version)
    return mon.AccountState(
        account="DU0001", version=version, net_liq=net_liq, cash=cash,
        positions=positions, schedule=schedule or [], target=target)


# target shares for SPY 100% @ $100 on NetLiq 1,000,000:
#   reserve 0 -> investable 1,000,000*(1-0.015)=985,000 -> floor(985,000/100)=9850 sh.
ON_TARGET = 9850


# --- 1. HOLD: in-band, no flows, no stray positions ---------------------------
def test_hold_in_band_no_flows():
    state = make_state({"SPY": ON_TARGET}, schedule=[])
    v = mon.decide(state)
    assert v.action == "HOLD"
    assert v.reason == mon.REASON_IN_BAND
    assert v.account == "DU0001"


# --- 2. REBALANCE: a real band breach -----------------------------------------
def test_rebalance_on_band_breach():
    # Hold only 5000 of a 9850 target -> trade ~48% of NetLiq, far past the 3% band.
    state = make_state({"SPY": 5000}, schedule=[])
    v = mon.decide(state)
    assert v.action == "REBALANCE"
    assert v.reason == mon.REASON_DRIFT_BAND_BREACH
    assert v.detail["drifted_lines"] >= 1


def test_small_drift_stays_hold():
    # Hold 9800 of 9850 -> trade 0.5% of NetLiq, inside the band -> HOLD, not REBALANCE.
    state = make_state({"SPY": 9800}, schedule=[])
    assert mon.decide(state).action == "HOLD"


# --- 3. ALERT: an upcoming distribution that available cash does not cover -----
def test_alert_withdrawal_due_unreserved():
    # SYNTHETIC schedule: $20,000/mo distribution; RESERVE_MONTHS(=1) -> reserve $20,000.
    # Account cash is only $5,000 -> cannot cover the upcoming distribution -> ALERT.
    sched = [cashflows.Flow("distribution", amount=20_000.0, pct_nav=0.0, day=1,
                            note="SYNTHETIC test flow")]
    state = make_state({"SPY": ON_TARGET}, cash=5_000.0, schedule=sched)
    v = mon.decide(state)
    assert v.action == "ALERT"
    assert v.reason == mon.REASON_WITHDRAWAL_DUE_UNRESERVED
    assert v.detail["shortfall"] == pytest.approx(15_000.0)


def test_withdrawal_covered_is_not_alerted():
    # Same $20k reserve, but cash $25,000 covers it -> no withdrawal ALERT (HOLD here).
    sched = [cashflows.Flow("distribution", amount=20_000.0, pct_nav=0.0, day=1,
                            note="SYNTHETIC test flow")]
    state = make_state({"SPY": ON_TARGET}, cash=25_000.0, schedule=sched)
    assert mon.decide(state).action == "HOLD"


def test_contribution_only_schedule_needs_no_reserve():
    # A contribution (cash IN) requires no reserve, so low cash does NOT alert.
    sched = [cashflows.Flow("contribution", amount=20_000.0, pct_nav=0.0, day=15,
                            note="SYNTHETIC test flow")]
    state = make_state({"SPY": ON_TARGET}, cash=1.0, schedule=sched)
    assert mon.decide(state).action == "HOLD"


# --- 4. ALERT: an untracked / unknown held position ---------------------------
def test_alert_untracked_position():
    # Hold the SPY target plus a stray GOOG not in the model -> ALERT (untracked), and the
    # untracked symbol is reported. (No distribution schedule, so it isn't the cash alert.)
    state = make_state(
        {"SPY": ON_TARGET, "GOOG": 10},
        weights={"SPY": 1.0}, prices={"SPY": 100.0, "GOOG": 150.0}, schedule=[])
    v = mon.decide(state)
    assert v.action == "ALERT"
    assert v.reason == mon.REASON_UNTRACKED_POSITION
    assert "GOOG" in v.detail["symbols"]


def test_withdrawal_alert_takes_precedence_over_rebalance():
    # Both a band breach AND an uncovered withdrawal -> the liquidity ALERT wins (cash
    # earmarked for a client must surface before any rebalance proposal).
    sched = [cashflows.Flow("distribution", amount=20_000.0, pct_nav=0.0, day=1,
                            note="SYNTHETIC test flow")]
    state = make_state({"SPY": 5000}, cash=1_000.0, schedule=sched)
    v = mon.decide(state)
    assert v.action == "ALERT"
    assert v.reason == mon.REASON_WITHDRAWAL_DUE_UNRESERVED


# --- 5. Verdict is immutable (propose-only data, can't be mutated post-decision) ---
def test_verdict_is_frozen():
    v = mon.decide(make_state({"SPY": ON_TARGET}))
    with pytest.raises(Exception):
        v.action = "REBALANCE"   # frozen dataclass -> FrozenInstanceError


# --- 5a. SettledCashByDate parser (REAL feed format) --------------------------
def test_parse_settled_cash_by_date_roundtrip():
    # The exact shape a live read-only probe returned 2026-06-30: 'YYYYMMDD:amount'.
    result = accounts.parse_settled_cash_by_date("20260630:51755.46")
    assert result == (date(2026, 6, 30), 51755.46)


def test_parse_settled_cash_by_date_handles_whitespace_and_int():
    assert accounts.parse_settled_cash_by_date("  20260101:1000  ") == (date(2026, 1, 1), 1000.0)


@pytest.mark.parametrize("bad", ["", None, "51755.46", "notadate:1000",
                                 "20260630:", ":1000", "20260630"])
def test_parse_settled_cash_by_date_rejects_malformed(bad):
    # Missing/empty/garbled -> None (NEVER a spurious 0.0 that could look like a withdrawal).
    assert accounts.parse_settled_cash_by_date(bad) is None


def test_parse_never_floats_the_raw_string():
    # The raw tag is NOT float()-able; the parser must split it, not blow up.
    with pytest.raises(ValueError):
        float("20260630:51755.46")
    assert accounts.parse_settled_cash_by_date("20260630:51755.46")[1] == 51755.46


# --- 5b. Deposit-detection core (PURE) — synthetic fixtures, REAL field formats ----
def make_deposit_state(*, settled_cash, baseline, net_liq=1_000_000.0,
                       fills=None, already_flagged=False,
                       positions=None, cash=None) -> mon.AccountState:
    """An on-target, no-schedule AccountState carrying deposit-detection inputs. On-target
    holdings + no flows mean ONLY the deposit path can change the verdict away from HOLD."""
    target = make_target({"SPY": 1.0}, {"SPY": 100.0}, "Balanced")
    # default holdings = the on-target share count for NAV (so drift never trips on its own)
    if positions is None:
        invest = mon.reconcile._investable.compute_investable(net_liq, 0.0)
        positions = {"SPY": int(invest // 100.0)}
    return mon.AccountState(
        account="DU0001", version="Balanced", net_liq=net_liq,
        cash=cash if cash is not None else settled_cash,
        positions=positions, schedule=[], target=target,
        settled_cash=settled_cash, baseline_settled_cash=baseline,
        baseline_date=date(2026, 6, 29), as_of_date=date(2026, 6, 30),
        fills=fills or [], deposit_already_flagged_today=already_flagged)


def test_clean_external_deposit_fires():
    # +$60,000 on a $1M account = 6% of NAV, > the 3% NAV floor AND > the $1,000 abs floor,
    # with NO sell fill -> EXTERNAL DEPOSIT -> REBALANCE / DEPOSIT_ARRIVED.
    state = make_deposit_state(settled_cash=110_000.0, baseline=50_000.0)
    v = mon.decide(state)
    assert v.action == "REBALANCE"
    assert v.reason == mon.REASON_DEPOSIT_ARRIVED
    assert v.detail["delta"] == pytest.approx(60_000.0)


def test_classify_clean_deposit():
    state = make_deposit_state(settled_cash=110_000.0, baseline=50_000.0)
    assert mon.classify_cash_increase(state)["classification"] == "EXTERNAL_DEPOSIT"


def test_sale_raised_cash_does_not_fire():
    # Cash up $60,000 BUT a same-day SLD fill (600 sh @ $100 = $60,000 proceeds) explains
    # it -> SALE_RAISED, NOT a deposit. (Sell down so the post-sale book isn't wildly off.)
    sld = mon.Execution(symbol="SPY", side="SLD", shares=600, price=100.0)
    state = make_deposit_state(settled_cash=110_000.0, baseline=50_000.0,
                               fills=[sld], positions={"SPY": 9250})
    res = mon.classify_cash_increase(state)
    assert res["classification"] == "SALE_RAISED"
    assert mon.decide(state).reason != mon.REASON_DEPOSIT_ARRIVED


def test_small_dividend_below_floor_does_not_fire():
    # +$200 dividend/interest, NO fill -> below BOTH the $1,000 abs floor and the 3% NAV
    # floor -> BELOW_GUARDS, never a deposit.
    state = make_deposit_state(settled_cash=50_200.0, baseline=50_000.0)
    assert mon.classify_cash_increase(state)["classification"] == "BELOW_GUARDS"
    assert mon.decide(state).reason != mon.REASON_DEPOSIT_ARRIVED


def test_increase_clears_abs_floor_but_not_nav_floor_does_not_fire():
    # +$2,000 clears the $1,000 abs floor but is only 0.2% of a $1M NAV -> under the 3%
    # NAV floor -> BELOW_GUARDS. BOTH guards must clear.
    state = make_deposit_state(settled_cash=52_000.0, baseline=50_000.0)
    assert mon.classify_cash_increase(state)["classification"] == "BELOW_GUARDS"


def test_debounce_second_same_day_does_not_refire():
    # Same qualifying deposit, but the live shell already flagged it today -> DEBOUNCED,
    # no second DEPOSIT_ARRIVED.
    state = make_deposit_state(settled_cash=110_000.0, baseline=50_000.0,
                               already_flagged=True)
    assert mon.classify_cash_increase(state)["classification"] == "DEBOUNCED"
    assert mon.decide(state).reason != mon.REASON_DEPOSIT_ARRIVED


def test_cold_start_no_baseline_does_not_fire():
    # No prior baseline (first observation) -> can't claim a deposit -> INSUFFICIENT_DATA.
    state = make_deposit_state(settled_cash=110_000.0, baseline=None)
    assert mon.classify_cash_increase(state)["classification"] == "INSUFFICIENT_DATA"


def test_missing_settled_cash_does_not_fire():
    # A garbled SettledCashByDate decodes to None upstream -> no current cash -> no deposit.
    state = make_deposit_state(settled_cash=None, baseline=50_000.0)
    assert mon.classify_cash_increase(state)["classification"] == "INSUFFICIENT_DATA"


def test_cash_decrease_is_not_a_deposit():
    # Settled cash DOWN since baseline -> NONE (a withdrawal, handled elsewhere, never a
    # deposit).
    state = make_deposit_state(settled_cash=40_000.0, baseline=50_000.0)
    assert mon.classify_cash_increase(state)["classification"] == "NONE"


def test_deposit_with_unreserved_withdrawal_still_alerts_withdrawal():
    # A qualifying deposit AND an uncovered upcoming distribution -> the liquidity ALERT
    # still WINS (cash earmarked for a client must surface before any redeploy proposal).
    sched = [cashflows.Flow("distribution", amount=20_000.0, pct_nav=0.0, day=1,
                            note="SYNTHETIC test flow")]
    target = make_target({"SPY": 1.0}, {"SPY": 100.0}, "Balanced")
    invest = mon.reconcile._investable.compute_investable(1_000_000.0, 20_000.0)
    state = mon.AccountState(
        account="DU0001", version="Balanced", net_liq=1_000_000.0,
        cash=5_000.0, positions={"SPY": int(invest // 100.0)}, schedule=sched,
        target=target, settled_cash=110_000.0, baseline_settled_cash=50_000.0,
        baseline_date=date(2026, 6, 29), as_of_date=date(2026, 6, 30), fills=[])
    v = mon.decide(state)
    assert v.action == "ALERT"
    assert v.reason == mon.REASON_WITHDRAWAL_DUE_UNRESERVED


def test_no_deposit_inputs_behaves_like_slice5():
    # An AccountState built WITHOUT any deposit fields (the Slice-5 call shape) must still
    # produce a HOLD on an on-target account — defaults make deposit detection a no-op.
    assert mon.decide(make_state({"SPY": ON_TARGET})).action == "HOLD"


# --- 5c. Withdrawal EARMARK fence + sale-raised NUDGE (Slice 6b) — PURE, synthetic ----
# Scenario substrate: NAV $1M, model 100% SPY @ $100. The operator sells ~600 SPY to raise
# ~$60,000 for an ad-hoc client withdrawal. Post-sale book ~9250 SPY + the raised cash.
#
# target shares with NO earmark: investable 1M*(1-0.015)=985,000 -> 9850 sh.
# target shares WITH a $60k earmark: (1M-60k)*0.985=925,900 -> 9259 sh, so a 9250-share
# post-sale book reads IN-BAND (the fence shrank the target to match the sold-down holdings).
SOLD_DOWN = 9250          # post-sale SPY share count
RAISED = 60_000.0         # cash raised by the sale / amount the operator earmarks


def make_earmark_state(*, earmarks, cash, positions, net_liq=1_000_000.0,
                       fills=None) -> mon.AccountState:
    target = make_target({"SPY": 1.0}, {"SPY": 100.0}, "Balanced")
    return mon.AccountState(
        account="DU0001", version="Balanced", net_liq=net_liq, cash=cash,
        positions=positions, schedule=[], target=target,
        # carry a prior baseline so the 6a classifier can see today's cash jump
        settled_cash=cash, baseline_settled_cash=cash - RAISED,
        baseline_date=date(2026, 6, 29), as_of_date=date(2026, 6, 30),
        fills=fills or [], earmarks=earmarks)


def test_earmark_fences_cash_no_rebalance():
    # An earmark covering the raised cash -> the sold-down book reads in-band (fence) and
    # the verdict is the WITHDRAWAL_EARMARK_RESERVED status, NOT a rebalance.
    em = mon.Earmark(account="DU0001", amount=RAISED, note="client X ad-hoc withdrawal")
    state = make_earmark_state(earmarks=[em], cash=RAISED, positions={"SPY": SOLD_DOWN})
    v = mon.decide(state)
    assert v.action == "ALERT"
    assert v.reason == mon.REASON_WITHDRAWAL_EARMARK_RESERVED
    assert v.detail["earmarked"] == pytest.approx(RAISED)


def test_earmark_sold_down_holdings_do_not_buy_back():
    # The fenced (sold-down) holdings must NOT produce a buy-back rebalance. Even setting
    # the deposit baseline aside, the drift path must propose nothing for the fenced cash.
    em = mon.Earmark(account="DU0001", amount=RAISED, note="fence")
    state = make_earmark_state(earmarks=[em], cash=RAISED, positions={"SPY": SOLD_DOWN})
    v = mon.decide(state)
    assert v.reason != mon.REASON_DRIFT_BAND_BREACH
    assert v.action != "REBALANCE"


def test_unearmarked_sale_raised_cash_nudges_not_rebalances():
    # Same sold-down book + raised cash, but NO earmark and a same-day SLD fill explaining
    # the cash -> SALE_RAISED_UNEARMARKED nudge, NOT a drift rebalance.
    sld = mon.Execution(symbol="SPY", side="SLD", shares=600, price=100.0)
    state = make_earmark_state(earmarks=[], cash=RAISED, positions={"SPY": SOLD_DOWN},
                               fills=[sld])
    v = mon.decide(state)
    assert v.action == "ALERT"
    assert v.reason == mon.REASON_SALE_RAISED_UNEARMARKED
    assert v.detail["amount"] == pytest.approx(RAISED)


def test_external_deposit_still_fires_without_sale_or_earmark():
    # An external deposit (no sale fill, no earmark) must STILL read as DEPOSIT_ARRIVED, not
    # be swallowed by the nudge path. +$60k, no SLD fill -> EXTERNAL_DEPOSIT.
    state = make_earmark_state(earmarks=[], cash=110_000.0,
                               positions={"SPY": ON_TARGET}, net_liq=1_000_000.0)
    # baseline = cash - RAISED = 50_000 -> +60k delta, no fill -> external deposit
    v = mon.decide(state)
    assert v.action == "REBALANCE"
    assert v.reason == mon.REASON_DEPOSIT_ARRIVED


def test_earmark_plus_drift_earmark_status_wins():
    # An earmark IS set (cash covers it) but the book is also genuinely drifted (way under
    # even the earmark-shrunk target). The earmark status outranks the drift rebalance so the
    # fenced cash is never proposed for redeployment.
    em = mon.Earmark(account="DU0001", amount=RAISED, note="fence")
    state = make_earmark_state(earmarks=[em], cash=RAISED, positions={"SPY": 5000})
    v = mon.decide(state)
    assert v.action == "ALERT"
    assert v.reason == mon.REASON_WITHDRAWAL_EARMARK_RESERVED


def test_earmark_set_but_cash_not_yet_raised_alerts_withdrawal():
    # The operator set the earmark BEFORE selling (cash does not yet cover it). Liquidity
    # safety: WITHDRAWAL_DUE_UNRESERVED still wins (the fenced cash isn't actually there).
    em = mon.Earmark(account="DU0001", amount=RAISED, note="fence pre-sale")
    state = make_earmark_state(earmarks=[em], cash=1_000.0, positions={"SPY": ON_TARGET})
    v = mon.decide(state)
    assert v.action == "ALERT"
    assert v.reason == mon.REASON_WITHDRAWAL_DUE_UNRESERVED


def test_no_earmarks_field_behaves_like_before():
    # An AccountState with the default (empty) earmarks list behaves exactly as Slice 6a:
    # on-target -> HOLD; no earmark status, no nudge.
    assert mon.decide(make_state({"SPY": ON_TARGET})).action == "HOLD"


# --- 6. PROPOSE-ONLY BOUNDARY: the module cannot reach a transmit/arm path -----
# Symbols that, if reachable from account_monitor's namespace, would mean it could touch a
# broker / build / transmit / arm an order. The monitor must compose only PURE pieces.
_FORBIDDEN_MODULES = {
    "order_router",          # build / build_fa_block / transmit_guard live here
    "execution_engine",      # the arming + transmit driver
    "live_quotes",           # live broker quotes -> a broker session
    "ibkr_paper",            # connections.ibkr_paper — the gateway/broker connection
    "ib_async", "ib_insync",
    # Slice 6a: the monitor must NOT import accounts (it reaches connections.ibkr_paper / a live
    # reqExecutions read). The deposit core takes ALREADY-DECODED settled-cash floats +
    # Execution objects as inputs; the live shell (6b) owns the broker read and the
    # accounts.parse_settled_cash_by_date decode.
    "accounts",
}
_FORBIDDEN_CALLABLE_NAMES = {
    "transmit", "build", "build_fa_block", "transmit_guard", "arm", "place_order",
    "placeOrder", "connect",
}


def test_module_does_not_import_any_transmit_path():
    """account_monitor's module namespace must not contain a transmit-capable module or
    a transmit/arm/connect callable — directly OR via a re-export."""
    ns = vars(mon)
    for name, obj in ns.items():
        if name.startswith("__"):
            continue
        # No forbidden MODULE bound in the namespace.
        modname = getattr(obj, "__name__", None)
        if inspect.ismodule(obj):
            assert modname not in _FORBIDDEN_MODULES, (
                f"account_monitor imports forbidden module {modname!r}")
        # No transmit/arm/connect CALLABLE bound by name.
        if callable(obj):
            assert name not in _FORBIDDEN_CALLABLE_NAMES, (
                f"account_monitor exposes forbidden callable {name!r}")


def test_source_does_not_reference_transmit_symbols():
    """Belt-and-suspenders: the SOURCE text imports none of the transmit modules. Catches a
    forbidden import even if it were aliased so the namespace check missed it."""
    src = inspect.getsource(mon)
    for bad in ("import order_router", "import execution_engine", "import live_quotes",
                "from connections import", "import ib_async", "import ib_insync",
                "import accounts"):
        assert bad not in src, f"account_monitor source references a transmit path: {bad!r}"
    # No LIVE executions call (the prose may NAME reqExecutions to document the seam, so we
    # forbid the actual call form `.reqExecutions(`, not the bare word).
    assert ".reqExecutions(" not in src, "account_monitor calls reqExecutions live"


def test_deposit_core_reaches_no_transmit_path():
    """Slice 6a: the deposit-detection additions stay inside the pure boundary. The new
    classify function and Execution type must reach nothing transmit-capable, and
    classify_cash_increase must build/transmit nothing (it returns a plain dict)."""
    assert "accounts" not in {getattr(v, "__name__", None)
                              for v in vars(mon).values() if inspect.ismodule(v)}
    res = mon.classify_cash_increase(
        mon.AccountState(account="DU0001", version="Balanced", net_liq=1_000_000.0,
                         cash=0.0, positions={}, schedule=[],
                         target=make_target({"SPY": 1.0}, {"SPY": 100.0}),
                         settled_cash=110_000.0, baseline_settled_cash=50_000.0))
    assert isinstance(res, dict) and res["classification"] == "EXTERNAL_DEPOSIT"


def test_earmark_core_reaches_no_transmit_path():
    """Slice 6b: the earmark fence + nudge additions stay inside the pure boundary. The new
    Earmark type and the earmark/nudge path in decide() must build/transmit nothing — decide
    returns a plain frozen Verdict, and the module imports no transmit-capable module."""
    # No forbidden module bound by the new code.
    assert {getattr(v, "__name__", None) for v in vars(mon).values()
            if inspect.ismodule(v)} & _FORBIDDEN_MODULES == set()
    # An earmark verdict is a plain frozen Verdict carrying no order / transmit handle.
    em = mon.Earmark(account="DU0001", amount=60_000.0, note="fence")
    target = make_target({"SPY": 1.0}, {"SPY": 100.0})
    invest = mon.reconcile._investable.compute_investable(1_000_000.0 - 60_000.0, 0.0)
    state = mon.AccountState(
        account="DU0001", version="Balanced", net_liq=1_000_000.0, cash=60_000.0,
        positions={"SPY": int(invest // 100.0)}, schedule=[], target=target,
        earmarks=[em])
    v = mon.decide(state)
    assert isinstance(v, mon.Verdict)
    assert v.reason == mon.REASON_WITHDRAWAL_EARMARK_RESERVED


def test_transitive_imports_stay_pure():
    """The modules account_monitor DOES import (cashflows, config, rebalance_engine,
    reconcile, strategy_target) are the same pure pieces the read-only recon path uses.
    Assert none of account_monitor's own imported modules is a forbidden transmit module."""
    imported = {getattr(v, "__name__", None)
                for v in vars(mon).values() if inspect.ismodule(v)}
    assert imported & _FORBIDDEN_MODULES == set(), (
        f"account_monitor reached a transmit module transitively: "
        f"{imported & _FORBIDDEN_MODULES}")
