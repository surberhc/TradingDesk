"""
s0_live_pilot_run.py — S0 (adaptive_all_weather) LIVE-PILOT *PREVIEW* runner
(conductor #3/#41, Slice 2). READ-ONLY, ZERO-TRANSMIT.

WHAT THIS IS
------------
The single-account analog of the paper FA multi-account morning pipeline
(nightly_monitor_run.py -> morning_execute_run.py), pointed at a REAL, funded account.
It reads the individual live-trading TEST account U5721712 on the Live-Trade Gateway
(port 4003 — the SAME gateway S8 pilots on; S8 uses the TRUST account U14438624 under
the same login, NEVER this one), computes S0's target via the shared brain
(strategy_target.current_target -> backtester run_backtest), sizes that target against the
REAL account with the UNCHANGED rebalance_engine.plan_account, and reports
"WOULD HAVE TRANSMITTED" — transmitting NOTHING.

Because this touches a real funded account, the safety posture is identical to the S8
pilot's, with TWO independent zero-transmit walls:
  1. READ-ONLY CONNECTION: it connects via s0_live.connect_s0_live(), which calls
     ibkr_live_trade.connect(readonly=True) and NEVER passes readonly=False. The gateway
     account is transmit-capable at the broker level, so read-only is a real, honored
     session flag — a bare connection here physically cannot write.
  2. BUILD / PREVIEW-ONLY: there is NO arm path and NO transmit code in this file at all.
     It imports no order_router, calls no place()/place_laddered()/ib.placeOrder(), and
     never touches arming. It sizes the plan and prints/emails the would-trade list.

This is a MANUAL, on-demand runner FOR NOW — deliberately NOT scheduled. The first pilot
cycles are meant to be human-reviewed (Andrew reads the emailed "WOULD HAVE TRANSMITTED"
report) before any automation is layered on, exactly as morning_execute_run.py's PILOT
cycles were reviewed before its own gate was ever considered for flipping.

It reuses the desk's existing brains UNCHANGED — strategy_target (shared backtester engine),
rebalance_engine.plan_account (sizing / no-trade band / reserve / reconcile), s0_live (the
read-only, account-pinned connection lane), live_quotes (real-time reference prices), and
the dailyreport mailer/status mechanisms the other runners use. It reimplements none of them.

Run (gateway auto-starts if down; reads U5721712 on port 4003, transmits nothing):
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe ^
    "C:\\TradingDesk\\paperbot\\s0_live_pilot_run.py"
"""
from __future__ import annotations

import os
import sys
from datetime import date

import config
import live_quotes
import rebalance_engine
import s0_live
import strategy_target
import version

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "dailyreport"))
import mailer  # noqa: E402  (sibling package, path inserted above)

try:
    import status as _status  # dailyreport/status.py
except Exception:
    _status = None


# --- status / mail plumbing (mirrors morning_execute_run.py / nightly_monitor_run.py) ----
def _write_status(st: str, metrics: dict | None = None, message: str = "") -> None:
    """Best-effort per-run status artifact for the EOD reporter. Never raises into the
    caller. Replicates morning_execute_run._write_status against the SAME dailyreport
    status module — NOT imported from morning_execute_run so its module-level side effects
    (PILOT_MODE et al.) are never pulled in here."""
    if _status is None:
        return
    try:
        _status.write("s0_live_pilot", st, metrics=metrics or {}, message=message,
                      day=date.today().strftime("%Y%m%d"))
    except Exception:
        pass


def _alert_email(subject: str, lines: list[str]) -> None:
    """Fire-and-forget report/alert email via the SHARED dailyreport mailer (the same
    mailer.send_html morning_execute_run.py uses). Never raises. Tagged S0 LIVE-PILOT
    because — unlike morning_execute's PAPER tag — this reads a real funded account."""
    html = "<html><body><pre>" + "\n".join(lines) + "</pre></body></html>"
    try:
        mailer.send_html(f"[TradingDesk S0 LIVE-PILOT] {subject}", html)
    except Exception as exc:
        print(f"    ! alert email itself failed: {exc}")


def _strategy_universe() -> set[str] | None:
    """S0's tradeable universe via the strategy's own universe() accessor — built the SAME
    way nightly_monitor_run._strategy_universe builds it. Returned so plan_account can tell a
    known symbol the model DROPPED (ROTATE_OUT — sell) apart from an ALIEN corp-action
    holding (review, never auto-traded). None on any failure so the plan falls back to legacy
    UNTRACKED classification rather than crashing — alien detection is informational and must
    never block the (already zero-transmit) preview."""
    try:
        from strategies.all_weather import universe as s0_universe
        return s0_universe()
    except Exception as exc:
        print(f"    ! could not resolve strategy universe ({exc}); "
              f"falling back to legacy UNTRACKED classification.")
        return None


def _net_liq(summary) -> float | None:
    """Parse NetLiquidation out of the (already account-FILTERED) accountSummary rows."""
    for row in summary:
        if getattr(row, "tag", None) == "NetLiquidation":
            try:
                return float(row.value)
            except (TypeError, ValueError):
                return None
    return None


def _safety_banner() -> None:
    print("\n" + "#" * 84)
    print("# SAFETY STATE   connection=READ-ONLY (s0_live.connect_s0_live, readonly=True)")
    print(f"# account={s0_live.S0_LIVE_ACCOUNT}   gateway=Live-Trade port 4003   "
          f"(NEVER the trust account U14438624)")
    print("# This runner is BUILD/PREVIEW-ONLY: no arm path, no order_router, no transmit")
    print("# code exists here. It reports 'WOULD HAVE TRANSMITTED' and transmits NOTHING.")
    print("#" * 84)


def _build_report(net_liq: float, positions: dict, target, prices: dict,
                  plan) -> list[str]:
    """PURE: assemble the human-readable 'WOULD HAVE TRANSMITTED' report body from an
    already-sized AccountPlan. Builds NOTHING and transmits NOTHING — string work only."""
    breached = plan.needs_rebalance   # AccountPlan's field name; plan_account's `breached`
    lines: list[str] = [
        f"S0 LIVE-PILOT PREVIEW — nothing was transmitted. This is what WOULD have been "
        f"sent if this were an armed run.",
        "",
        f"  account   {plan.account}   (individual live-trading TEST account, port 4003)",
        f"  model     {target.version}   as_of={target.as_of.date()}  "
        f"data_through={target.price_date.date()}",
        f"  NetLiq    {net_liq:,.2f}",
        f"  reserve   {plan.reserve:,.2f}   (distribution carve-out; 0 unless in "
        f"cashflows.SCHEDULE)",
        f"  investable {plan.investable:,.2f}",
        "",
        "  TARGET WEIGHTS:",
    ]
    for sym, w in target.weights.items():
        px = prices.get(sym, float("nan"))
        lines.append(f"    {sym:<6} {w * 100:>7.2f}%   px~{px:,.2f}")

    lines += ["", "  CURRENT POSITIONS:"]
    if positions:
        for sym in sorted(positions):
            px = prices.get(sym, float("nan"))
            lines.append(f"    {sym:<6} x{positions[sym]:<12,.4f}  px~{px:,.2f}")
    else:
        lines.append("    (none — account holds no positions / cash only)")

    lines += [""]
    if breached:
        lines.append("  WOULD HAVE TRANSMITTED (band breached — full rebalance to model):")
        for sym in sorted(plan.orders):
            delta = plan.orders[sym]
            verb = "WOULD BUY " if delta > 0 else "WOULD SELL"
            px = prices.get(sym, float("nan"))
            lines.append(f"    {verb} {sym:<6} x{abs(delta):<10} @~{px:,.2f}")
    else:
        lines.append("  No trade — account is within the no-trade band; nothing would be "
                     "transmitted.")

    if plan.alien_lines:
        lines += ["", "  REVIEW (alien / corp-action holdings, never auto-traded):"]
        for ln in plan.alien_lines:
            px = prices.get(ln.symbol, float("nan"))
            lines.append(f"    {ln.symbol:<6} qty={ln.actual_shares:,.4f}  px~{px:,.2f}")

    return lines


def main(today=None) -> int:
    """LIVE-PILOT PREVIEW path. READ-ONLY + BUILD-ONLY: computes the target, reads the real
    account U5721712 on port 4003, sizes the plan with the UNCHANGED engine, and reports
    'WOULD HAVE TRANSMITTED'. Transmits nothing — there is no arm path and no order_router
    here. `today` is accepted for signature parity with the other runners; the shared brain
    always runs to the most recent data date."""
    print("=" * 84)
    print(f"S0 LIVE-PILOT PREVIEW RUNNER — READ-ONLY, transmits nothing   "
          f"[{version.banner()}]")
    print("=" * 84)

    # [1] Compute the target BEFORE connecting (fail fast on stale data; connect nothing on
    # failure). Uses config.STRATEGY_VERSION by default — the shared backtester brain, so
    # paper == backtest == this preview by construction.
    print("\n[1] Computing the S0 target (shared brain; stale-data guarded)...")
    try:
        target = strategy_target.current_target()
    except Exception as exc:
        print(f"    COULD NOT BUILD TARGET: {exc}")
        _write_status("fail", message=f"target computation failed: {exc}")
        return 2
    print(f"    {target.version}   as_of={target.as_of.date()}  "
          f"price_date={target.price_date.date()}  ({len(target.weights)} holdings)")

    # [2] Safety banner — read-only, preview-only, real account on 4003.
    _safety_banner()

    # [3] Connect READ-ONLY to the Live-Trade gateway (port 4003). No gateway_lock here: that
    # lock is scoped to the PAPER gateway (4002); the 4003 launch coordination lives inside
    # ibkr_live_trade.ensure_gateway — this is exactly why s8_runner.py deliberately does NOT
    # use gateway_lock (see its note ~line 185-193). Whole session in try/finally so the
    # connection is ALWAYS disconnected.
    print(f"\n[3] Connecting READ-ONLY to the Live-Trade gateway (port 4003), "
          f"clientId s0_live_pilot...")
    try:
        ib = s0_live.connect_s0_live(launch=True)
    except Exception as exc:
        msg = (f"s0_live_pilot: could not connect READ-ONLY to the Live-Trade gateway "
               f"(port 4003): {type(exc).__name__}: {exc}. Nothing read, nothing sized, "
               f"nothing transmitted.")
        print(f"    {msg}")
        _alert_email("S0 live-pilot: gateway connect FAILED", [msg])
        _write_status("fail", message=msg)
        return 1

    try:
        return _run_session(ib, target)
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass
        print("Read-only session closed. Nothing was transmitted.")


def _run_session(ib, target) -> int:
    account = s0_live.S0_LIVE_ACCOUNT

    # [4] Read + FILTER to U5721712 (the login exposes the trust account + an 'All' aggregate
    # too; every read is pinned to S0's individual account and never the trust account).
    print(f"\n[4] Reading account summary + positions, filtering to {account}...")
    try:
        summary_all = ib.accountSummary()
    except Exception as exc:
        msg = f"s0_live_pilot: could not read accountSummary() from the Live-Trade connection: {exc}"
        print(f"    {msg}")
        _alert_email("S0 live-pilot: accountSummary FAILED", [msg])
        _write_status("fail", message=msg)
        return 1

    summary = s0_live.filter_account_summary(summary_all)
    if not summary:
        seen = sorted(str(a) for a in {getattr(r, "account", None) for r in summary_all}
                      if a is not None)
        msg = (f"s0_live_pilot: target account {account} not found under the Live-Trade "
               f"login (accounts seen: {seen}) — REFUSING this cycle. Nothing sized, "
               f"nothing transmitted. Check S0_LIVE_ACCOUNT against the login's managed "
               f"accounts.")
        print(f"    {msg}")
        _alert_email("S0 live-pilot: target account not found", [msg])
        _write_status("fail", message=msg)
        return 1

    net_liq = _net_liq(summary)
    if not net_liq or net_liq <= 0:
        msg = f"s0_live_pilot: could not read a positive NetLiquidation for {account}."
        print(f"    {msg}")
        _alert_email("S0 live-pilot: NetLiquidation unreadable", [msg])
        _write_status("fail", message=msg)
        return 1

    positions_raw = s0_live.filter_positions(ib.positions())
    positions = {p.contract.symbol: p.position for p in positions_raw if p.position != 0}
    print(f"    account={account}   NetLiq={net_liq:,.2f}   open_positions={len(positions)}")

    # [5] Prices: fetch live real-time quotes on the 4003 connection over the union of the
    # target's symbols and any held symbol; prefer a fresh quote (>0) and fall back to the
    # target's strategy-data close — exactly the merge morning_execute / nightly_monitor use.
    universe = sorted(set(target.weights.index) | set(positions))
    print(f"\n[5] Fetching live quotes for {len(universe)} symbol(s) on port 4003...")
    quotes = live_quotes.fetch(ib, universe)
    prices: dict = {}
    for sym in universe:
        q = quotes.get(sym)
        ref = live_quotes.reference_price(q) if q else None
        prices[sym] = ref if (ref and ref > 0) else float(target.prices.get(sym, float("nan")))

    # [6] The strategy's tradeable universe (same accessor nightly_monitor uses), threaded
    # into plan_account so a held symbol the model dropped is ROTATE_OUT (sell) vs an ALIEN
    # holding is surfaced for review, never auto-traded.
    strat_universe = _strategy_universe()

    # [7] Size the REAL account against the target with the UNCHANGED engine. band_pct=None
    # lets the engine use config.REBALANCE_BAND_PCT. NOTE: U5721712 is not in
    # cashflows.SCHEDULE, so reserve_for returns 0 (fully invested) — the correct default; if
    # this account ever needs a distribution carve-out it gets added to cashflows.SCHEDULE.
    print("\n[7] Sizing the plan with rebalance_engine.plan_account (UNCHANGED engine)...")
    plan = rebalance_engine.plan_account(account, target.version, net_liq, positions,
                                         target, prices=prices, universe=strat_universe)

    # [8] Build + print + email the 'WOULD HAVE TRANSMITTED' report. Nothing is built or sent.
    breached = plan.needs_rebalance
    n_orders = len(plan.orders)
    report = _build_report(net_liq, positions, target, prices, plan)
    print("\n" + "\n".join(report))

    subject = (f"S0 live-pilot PREVIEW ({account}): {n_orders} would-trade / "
               f"band {'breached' if breached else 'in-band'}")
    _alert_email(subject, report)
    _write_status("ok", metrics={"account": account, "net_liq": net_liq,
                                 "n_orders": n_orders, "band_breached": breached,
                                 "pilot_preview": True},
                  message=f"preview: {n_orders} would-trade, band "
                          f"{'breached' if breached else 'in-band'}, nothing transmitted")

    # [9] Clean preview.
    print(f"\nDone. READ-ONLY preview: {n_orders} would-trade line(s), band "
          f"{'breached' if breached else 'in-band'}. Nothing transmitted.")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    sys.exit(main())
