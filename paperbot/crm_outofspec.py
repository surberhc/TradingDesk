"""crm_outofspec.py — PURE whole-book OUT-OF-SPEC assembly over the frozen rebalance engine.

The P1 payoff: for every blessed CRM roster account, decide IN-SPEC vs OUT-OF-SPEC against
the desk's frozen model target and surface the legs that WOULD trade to conform — reading
only, transmitting nothing.

It assembles ``rebalance_engine.build_plan`` inputs from CRM data and runs the UNCHANGED pure
engine (the same PREVIEW posture as ``crm_execute.preview_crm``: no ``ib``, ``armed=False`` —
``build_plan`` builds and transmits nothing by construction). It then reads each returned
``AccountPlan``'s ``.breached`` verdict + ``.orders`` (the would-trade share deltas).

HARD BOUNDARY: pure computation. Contacts no broker, builds no order object, transmits
nothing. The frozen model + engine are reused verbatim — nothing here re-implements sizing.
"""
from __future__ import annotations

from typing import Mapping, Optional

import rebalance_engine
import crm_roster


def _to_float(x) -> float:
    """Coerce a psycopg2 Decimal / str / number to float; None/blank/unparseable -> 0.0."""
    if x is None:
        return 0.0
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def account_inputs_from_roster(roster_rows: list[Mapping],
                               holdings_by_account: Mapping[str, list[Mapping]],
                               ) -> tuple[list[dict], list[dict]]:
    """Turn CRM roster rows + their latest holdings into ``rebalance_engine`` account_inputs.

    For each roster row:
      * account   = the IBKR number (crm_roster.account_identifier — CRM->desk identity map)
      * version   = the roster ``model`` (today uniformly "Growth")
      * positions = {symbol -> quantity}  from the account's latest holdings snapshot
      * prices    = {symbol -> mark_price} from the same snapshot (positive marks only)
      * net_liq   = roster ``total_value``; falls back to the holdings market-value sum

    Returns ``(account_inputs, skipped)``. ``skipped`` collects accounts with no usable
    net_liq (unfunded / no snapshot) — the read-only stand-in for 'not funded reality', kept
    OUT of the engine run and surfaced separately. PURE."""
    account_inputs: list[dict] = []
    skipped: list[dict] = []

    for row in roster_rows:
        aid = str(row.get("account_id"))
        account = crm_roster.account_identifier(row)
        version = row.get("model") or ""
        holds = holdings_by_account.get(aid, []) or []

        positions: dict[str, float] = {}
        prices: dict[str, float] = {}
        holdings_value = 0.0
        for h in holds:
            sym = str(h.get("symbol") or "").strip()
            if not sym:
                continue
            qty = _to_float(h.get("quantity"))
            px = _to_float(h.get("mark_price"))
            positions[sym] = positions.get(sym, 0.0) + qty
            if px == px and px > 0:
                prices[sym] = px
            holdings_value += _to_float(h.get("market_value"))

        net_liq = _to_float(row.get("total_value"))
        if not (net_liq > 0):
            net_liq = holdings_value

        base = {
            "account": account,
            "advisor_name": row.get("advisor_name"),
            "entity": row.get("entity"),
            "master_name": row.get("master_name"),
        }
        if not (net_liq > 0):
            skipped.append({**base, "version": version,
                            "reason": "no net_liq / no holdings snapshot (unfunded)"})
            continue

        account_inputs.append({
            "account": account,
            "version": version,
            "net_liq": net_liq,
            "positions": positions,
            "prices": prices,
            # carried through for display; the engine ignores unknown keys.
            "advisor_name": row.get("advisor_name"),
            "entity": row.get("entity"),
            "master_name": row.get("master_name"),
            "n_positions": len(positions),
        })

    return account_inputs, skipped


def _legs_from_orders(orders: Mapping[str, int]) -> list[dict]:
    """Turn an AccountPlan's ``orders`` (symbol -> signed share delta) into a display leg
    list: SELLs first then BUYs, each ``{symbol, side, shares}``. Pure."""
    legs = []
    for sym, delta in orders.items():
        d = int(delta)
        if d == 0:
            continue
        legs.append({"symbol": sym, "side": "SELL" if d < 0 else "BUY", "shares": abs(d)})
    legs.sort(key=lambda l: (0 if l["side"] == "SELL" else 1, l["symbol"]))
    return legs


def verdicts_from_plans(plans: list, account_inputs: list[Mapping]) -> list[dict]:
    """Read each engine AccountPlan into a per-account verdict row. PURE — no engine call.

    out_of_spec == the engine's account-level no-trade-band verdict (AccountPlan
    ``needs_rebalance``), with a would-trade-legs fallback. Kept as a separate function so it
    is unit-testable with synthetic AccountPlans (no backtest)."""
    meta = {ai["account"]: ai for ai in account_inputs}
    by_acct = {p.account: p for p in plans}
    verdicts: list[dict] = []
    for account, ai in meta.items():
        p = by_acct.get(account)
        if p is None:
            continue
        orders = getattr(p, "orders", {}) or {}
        legs = _legs_from_orders(orders)
        # AccountPlan.needs_rebalance is the engine's band verdict; treat any would-trade
        # leg as out-of-spec too (defensive — orders only fill when the band is breached).
        out_of_spec = bool(getattr(p, "needs_rebalance", False)) or bool(legs)
        verdicts.append({
            "account": account,
            "version": ai.get("version"),
            "advisor_name": ai.get("advisor_name"),
            "entity": ai.get("entity"),
            "master_name": ai.get("master_name"),
            "net_liq": float(getattr(p, "net_liq", ai.get("net_liq", 0.0)) or 0.0),
            "n_positions": ai.get("n_positions", 0),
            "out_of_spec": out_of_spec,
            "n_legs": len(legs),
            "legs": legs,
            "n_alien": len(getattr(p, "alien_lines", []) or []),
        })
    verdicts.sort(key=lambda v: (not v["out_of_spec"], -v["net_liq"]))
    return verdicts


def scan_out_of_spec(roster_rows: list[Mapping],
                     holdings_by_account: Mapping[str, list[Mapping]],
                     targets: Mapping,
                     band_pct: Optional[float] = None,
                     universe: Optional[set] = None) -> dict:
    """Whole-book out-of-spec read: assemble inputs, run the UNCHANGED pure
    ``rebalance_engine.build_plan``, and return per-account verdicts.

    ``targets`` maps version -> ``strategy_target.Target`` (the frozen desk model, e.g.
    ``strategy_target.current_target("Growth")``) — supplied by the caller so this stays pure
    and broker-free. Returns::

        {"verdicts": [...], "skipped": [...], "n_accounts": int,
         "n_out_of_spec": int, "n_in_spec": int}

    Builds and transmits NOTHING (``build_plan`` is the pure engine; no ``ib``, no arming)."""
    account_inputs, skipped = account_inputs_from_roster(roster_rows, holdings_by_account)
    if not account_inputs:
        return {"verdicts": [], "skipped": skipped, "n_accounts": 0,
                "n_out_of_spec": 0, "n_in_spec": 0}

    result = rebalance_engine.build_plan(
        account_inputs, dict(targets), band_pct=band_pct, universe=universe)
    verdicts = verdicts_from_plans(result["plans"], account_inputs)
    n_oos = sum(1 for v in verdicts if v["out_of_spec"])
    return {
        "verdicts": verdicts,
        "skipped": skipped,
        "n_accounts": len(verdicts),
        "n_out_of_spec": n_oos,
        "n_in_spec": len(verdicts) - n_oos,
    }
