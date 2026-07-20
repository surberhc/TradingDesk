"""rehearse_dedup_gate_stage.py — REHEARSAL-ONLY staging for the S0 dedup gate.

Dev / verification harness for conductor #41 (armed-live S0), step 2: rehearse the
order_router dedup gate on a REAL cycle without waiting for a genuine month-end
rebalance (S0's cadence is monthly, so real staged cycles are sparse).

Zero-transmit. Connects READ-ONLY to the paper gateway (4002), computes the REAL
current tier models vs REAL live account holdings, and forces a full-rebalance plan
(band_pct=0.0 — a rehearsal parameter only; no frozen strategy/regime/band config is
touched) to produce a genuine, non-empty staged trade list for TODAY so that
morning_execute_run.py (PILOT_MODE=True) can then rehearse the dedup gate against real
broker reads. Routes are tagged reason="REHEARSAL_GATE" so they are distinguishable from
a real rebalance. It NEVER places, modifies, or transmits any order — it only writes the
staged JSON via nightly_monitor_run.stage_trade_list in the exact production format;
transmission stays walled by morning_execute's PILOT_MODE.

Run (paper gateway must be up on 4002):
    cd C:\\TradingDesk\\paperbot
    C:\\TradingDesk-Local\\venv\\Scripts\\python.exe rehearse_dedup_gate_stage.py
then run morning_execute_run.py (PILOT) to exercise the gate on what this staged.
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import accounts
import live_quotes
import rebalance_guard
import rebalance_run
import rebalance_engine
import account_monitor_run as amr
import nightly_monitor_run as nm
from connections import ibkr_paper


def main() -> int:
    today = date.today()
    print(f"REHEARSAL staging for {today.isoformat()} (read-only, zero-transmit)")
    ib = ibkr_paper.connect("paperbot_nightly_monitor", readonly=True, launch=False, timeout=90)
    try:
        targets = amr._targets_by_version()
        for v, t in targets.items():
            print(f"  model {v:13s} as_of={t.as_of.date()} ({len(t.weights)} holdings)")
        infos = accounts.discover(ib)
        clients = [i for i in infos if i.enrolled and i.funded and not i.is_master]
        if not clients:
            print("no enrolled/funded client accounts -- cannot rehearse")
            return 2
        universe = sorted({s for t in targets.values() for s in t.weights.index})
        quotes = live_quotes.fetch(ib, universe)
        account_inputs = []
        for info in sorted(clients, key=lambda x: x.number):
            positions = {p.contract.symbol: p.position
                         for p in ib.positions(info.number) if p.position != 0}
            tier_prices = targets[info.version].prices
            prices = {}
            for sym in set(tier_prices.index) | set(positions):
                q = quotes.get(sym)
                ref = live_quotes.reference_price(q) if q else None
                prices[sym] = ref if (ref and ref > 0) else float(tier_prices.get(sym, float("nan")))
            account_inputs.append({"account": info.number, "version": info.version,
                                   "net_liq": info.net_liq, "positions": positions, "prices": prices})
        enrolled_versions = {i.version for i in clients}
        tier_groups = rebalance_run.resolve_tier_groups(ib, enrolled_versions)
        out = rebalance_engine.build_plan(account_inputs, targets, band_pct=0.0,
                                          tier_groups=tier_groups)
        routes = out["routes"]
        print(f"  forced full-rebalance (band_pct=0.0) -> {len(routes)} route(s)")
        if not routes:
            print("no routes produced (accounts exactly at model) -- nothing to rehearse")
            return 3
        for r in routes:
            try:
                r.reason = "REHEARSAL_GATE"
            except Exception:
                pass
        raw_regime, confirmed_regime, regime_as_of = rebalance_guard.compute_regime_now()
        regime = {"raw": raw_regime, "confirmed": confirmed_regime, "as_of": regime_as_of}
        prices_by_symbol = {sym: px for ai in account_inputs for sym, px in ai["prices"].items()}
        guard = rebalance_guard.check(routes, account_inputs, prices_by_symbol,
                                      claimed_regime=confirmed_regime)
        print(f"  guard.passed={guard.passed}")
        if not guard.passed:
            for reason in guard.reasons:
                print(f"    GUARD REASON: {reason}")
            print("guard did not pass -- NOT staging (morning re-validation would reject it)")
            return 4
        needing = sorted({r.account for r in routes if r.account}
                         | {a for r in routes for a in (r.per_account_split or {})})
        path = nm.stage_trade_list(today, routes, regime, guard, targets, needing, prices_by_symbol)
        print(f"STAGED (rehearsal) -> {path}")
        for r in routes:
            tgt = r.account or f"group={r.fa_group}"
            print(f"    {r.route} {r.side} {r.symbol} x{r.total_qty}  {tgt}  reason={r.reason}")
        return 0
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass
        print("disconnected.")


if __name__ == "__main__":
    sys.exit(main())
