"""
s4_rebalance_run.py — S4 (SPX vol-control fund) SINGLE-ACCOUNT review-only runner.

Modeled on execution_engine.py's shape (diff -> risk -> build -> place(armed=False)), but
S4-specific and SINGLE-ACCOUNT: it does NOT use the multi-account FA-block / tier-group
machinery (rebalance_run.py / rebalance_engine.py). S4 is one account holding a
{SPY, BIL} vol-control book that may run SPY exposure above 1.0x on real margin.

Pipeline:
  [1] s4_strategy_target.current_target(profile) -> the {SPY, BIL} book (read-only compute,
      with the stale-data guard). ZERO exposure math here.
  [2] gateway_lock (refuse-if-busy) + connect READ-ONLY, confirm the S4 paper account.
  [3] read NAV, positions, and the account SUMMARY (AccountType/BuyingPower/ExcessLiquidity).
  [4] MARGIN PREFLIGHT (s4_risk.margin_preflight): the leveraged (>1.0) path is refused
      unless the account is a confirmed margin account with sufficient buying power; the
      un-levered path is always allowed. Fails CLOSED.
  [5] S4 leverage SIZING (s4_sizing.size_orders): SPY notional = NAV*exposure (may exceed
      NAV), borrow leg carried through.
  [6] S4 RISK GUARD (s4_risk.evaluate_s4): permits up to the profile cap, vetoes beyond.
  [7] order_router.build + place(armed=False): logs the exact orders, transmits NOTHING.

HARD SAFETY (same gate as the rest of the paperbot): transmission is impossible unless
config.READONLY is False AND config.DRY_RUN is False AND a human passes armed=True. There is
NO auto-arm here. The runner connects read-only (physically cannot transmit). The account id
is a PARAMETER — never hardcoded.

Run (gateway auto-starts if down); ACCOUNT is required at deploy:
  C:\\TradingDesk-Local\\venv\\Scripts\\python.exe s4_rebalance_run.py --account DU89221XX --profile conservative
"""
from __future__ import annotations

import argparse
import sys

import config
import order_router
import s4_risk
import s4_sizing
import s4_strategy_target
import version
from connections import clientids, ibkr_paper
from gateway_lock import GatewayBusyRefuse, gateway_lock


# --- PURE preview (no broker, no I/O beyond printing) -------------------------------
def build_preview(nav: float, positions: dict, target, leverage_cap: float,
                  summary=None, prices: dict | None = None) -> dict:
    """Run S4 sizing + risk guard (+ preflight if a summary is given) and print a full,
    reviewable report. PURE: no broker. Returns
    {"intents", "verdict", "preflight", "exposure"}."""
    exposure = s4_sizing.exposure_of(target)
    preflight = None
    if summary is not None:
        preflight = s4_risk.margin_preflight(summary, nav, exposure, leverage_cap)

    intents = s4_sizing.size_orders(nav, positions, target, prices=prices)
    verdict = s4_risk.evaluate_s4(nav, target, intents, leverage_cap)

    print("\n" + "=" * 84)
    print(f"S4 SPX VOL-CONTROL FUND — single-account review   [{version.banner()}]")
    print("=" * 84)
    print(f"  target as_of {target.as_of.date()}  (data through {target.price_date.date()})")
    print(f"  model: {target.version}")
    print(f"  exposure {exposure:.4f}x   leverage_cap {leverage_cap:.2f}x   NAV {nav:,.2f}")
    print("-" * 84)
    for sym, w in target.weights.items():
        tag = "  (BORROW)" if (sym == s4_sizing.CASH_TICKER and w < 0) else ""
        print(f"    {sym:<6} target {w * 100:>8.2f}%{tag}")
    print("-" * 84)

    if preflight is not None:
        print("  MARGIN PREFLIGHT:")
        print(f"    AccountType={preflight.account_type!r}  is_margin={preflight.is_margin}  "
              f"BuyingPower={preflight.buying_power:,.0f}  ExcessLiq={preflight.excess_liquidity:,.0f}")
        print(f"    required SPY notional={preflight.required_notional:,.0f}  "
              f"-> {'OK' if preflight.ok else 'REFUSED'}")
        for r in preflight.reasons:
            print(f"      - {r}")
        print("-" * 84)

    print("  INTENDED ORDERS (S4 leverage sizing; borrow leg carried, not dropped):")
    if not intents:
        print("    none — the account already matches the target book.")
    for o in intents:
        if o.is_borrow_leg:
            print(f"    BORROW {o.symbol:<6} notional {o.target_dollars:>15,.0f}  "
                  f"(financed by the >100% SPY leg; no BIL shares traded)")
        else:
            print(f"    {o.side:<4}  {o.symbol:<6} x{o.quantity:<8d} @ {o.limit_price:>10,.2f}  "
                  f"tgt_w {o.target_weight * 100:>7.2f}%  tgt_$ {o.target_dollars:>15,.0f}")
    print("-" * 84)
    print(f"  RISK GUARD: {'OK' if verdict.ok else 'VETO'}  "
          f"(exposure {verdict.exposure:.4f}x vs cap {verdict.leverage_cap:.2f}x)")
    for r in verdict.reasons:
        print(f"      - {r}")
    for sym, r in verdict.order_vetoes:
        print(f"      - {sym}: {r}")
    print("=" * 84)
    return {"intents": intents, "verdict": verdict, "preflight": preflight,
            "exposure": exposure}


def _net_liq(summary, account: str) -> float | None:
    for row in summary:
        if row.account == account and row.tag == "NetLiquidation":
            return float(row.value)
    return None


def _safety_banner(armed: bool) -> None:
    permit, why = order_router.transmit_guard(armed)
    print("\n" + "#" * 84)
    print(f"# SAFETY STATE   READONLY={config.READONLY}   DRY_RUN={config.DRY_RUN}   armed={armed}")
    print(f"# transmission: {'PERMITTED' if permit else 'BLOCKED'} ({why})")
    print("# S4 runner connects READ-ONLY and is BUILD-ONLY. Transmits nothing.")
    print("#" * 84)


def main(account: str | None = None, *, profile: str | None = None,
         target_vol: float | None = None, leverage_cap: float | None = None,
         armed: bool = False, today=None) -> int:
    """LIVE review-only path. Requires READONLY + DRY_RUN. Builds orders but transmits
    nothing (place(armed=False) + the read-only connection both block transmission).
    `account` is REQUIRED (no hardcoded default) and must be a paper (DU/DF) account.
    `armed` is exposed only so the guard's shape is visible; flipping it has no effect
    while READONLY/DRY_RUN hold."""
    print("=" * 84)
    print(f"S4 SINGLE-ACCOUNT REBALANCE RUNNER — REVIEW ONLY (build-only, transmits nothing)")
    print("=" * 84)
    _safety_banner(armed)

    if not (config.READONLY and config.DRY_RUN):
        print("\nSAFETY STOP: this runner requires READONLY=True and DRY_RUN=True in config.")
        return 2
    if not account:
        print("\nSAFETY STOP: no account given. Pass --account DU89221XX (never hardcoded).")
        return 2

    # [1] Target BEFORE connecting (fail fast on stale data). Resolve the active leverage_cap
    # from the same profile/overrides so the guard + preflight use the CORRECT cap.
    print("\n[1] Computing the S4 target book (shared brain; stale-data guarded)...")
    try:
        target = s4_strategy_target.current_target(
            account=account, profile=profile, target_vol=target_vol,
            leverage_cap=leverage_cap, today=today)
    except Exception as exc:
        print(f"    COULD NOT BUILD TARGET: {exc}")
        return 2
    _tv, active_cap, label = s4_strategy_target._resolve_params(profile, target_vol, leverage_cap)
    print(f"    {target.version}   as_of={target.as_of.date()}  price_date={target.price_date.date()}")

    # GATEWAY LOCK: acquire the single-process mutex for the WHOLE session (refuse if held,
    # naming the holder). Never operate the Gateway blind into a contended session.
    try:
        with gateway_lock(purpose="s4_rebalance_run",
                          client_id=clientids.get("paperbot_s4"), on_busy="refuse"):
            return _run_gateway_session(account, target, active_cap, armed)
    except GatewayBusyRefuse as busy:
        h = busy.holder or {}
        print(f"\n[2] REFUSING to start — gateway held by {h.get('purpose')} pid {h.get('pid')} "
              f"clientId {h.get('client_id')} since {h.get('acquired_at') or h.get('acquired_ts')}. "
              f"No connection opened, NO orders built, nothing transmitted. Re-run once it finishes.")
        return 2


def _run_gateway_session(account: str, target, leverage_cap: float, armed: bool) -> int:
    """connect -> read -> preflight -> size -> guard -> build -> place(armed=False), run only
    while the gateway lock is HELD."""
    print(f"\n[2] Connecting to PAPER gateway {ibkr_paper.HOST}:{ibkr_paper.PAPER_PORT} "
          f"(clientId={clientids.get('paperbot_s4')}, readonly=True)...")
    try:
        ib = ibkr_paper.connect("paperbot_s4", readonly=True, launch=True)
    except Exception as exc:
        print(f"    COULD NOT CONNECT: {exc}")
        return 1
    try:
        accounts = ib.managedAccounts()
        if account not in accounts:
            print(f"    SAFETY STOP: account {account} not among managed accounts {accounts}.")
            return 2
        if not account.startswith(("DU", "DF")):
            print(f"    SAFETY STOP: {account} is not a paper (DU/DF) account. Halting.")
            return 2

        summary = ib.accountSummary(account)
        nav = _net_liq(summary, account)
        if not nav or nav <= 0:
            print(f"    SAFETY STOP: could not read a positive NetLiquidation for {account}.")
            return 2
        positions = {p.contract.symbol: p.position
                     for p in ib.positions(account) if p.position != 0}
        print(f"    account={account}   NetLiq={nav:,.2f}   open_positions={len(positions)}")

        # [3-6] PREVIEW does preflight + sizing + guard and prints the full report.
        out = build_preview(nav, positions, target, leverage_cap, summary=summary)
        preflight, verdict, intents = out["preflight"], out["verdict"], out["intents"]

        # [4b] Fail closed on a refused preflight or a vetoed guard: build NOTHING.
        if preflight is not None and not preflight.ok:
            print("\n[4] MARGIN PREFLIGHT REFUSED — no orders built, nothing transmitted.")
            return 2
        if not verdict.ok:
            print("\n[6] S4 RISK GUARD VETO — no orders built, nothing transmitted.")
            return 2

        # [7] Build the tradeable (non-borrow) intents and place(armed=False) -> logs, no send.
        tradeable = [o for o in intents if not o.is_borrow_leg and o.quantity > 0]
        print("\n[7] Building order objects (build-only) and routing via place(armed=False)...")
        built = order_router.build(tradeable, account, target.as_of, ib=ib)
        order_router.place(ib, built, armed=armed)

        print("\nDone. READ-ONLY + DRY_RUN: orders were BUILT and LOGGED, nothing transmitted.")
        return 0
    finally:
        ib.disconnect()
        print("Read-only session closed. Nothing was transmitted.")


def _parse_args(argv=None):
    ap = argparse.ArgumentParser(description="S4 single-account review-only runner")
    ap.add_argument("--account", required=True, help="paper account (DU/DF...) — never hardcoded")
    ap.add_argument("--profile", default=None, choices=["balanced", "conservative"],
                    help="named deploy cell; omit to use overrides or the conservative default")
    ap.add_argument("--target-vol", type=float, default=None, help="custom target vol (with --leverage-cap)")
    ap.add_argument("--leverage-cap", type=float, default=None, help="custom cap (with --target-vol)")
    return ap.parse_args(argv)


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    args = _parse_args()
    sys.exit(main(account=args.account, profile=args.profile,
                  target_vol=args.target_vol, leverage_cap=args.leverage_cap))
