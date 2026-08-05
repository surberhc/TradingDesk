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


# Asset category (v_tradingdesk_holdings_latest.asset_category) that marks an individual
# BOND. IBKR represents a bond position's QUANTITY as its FACE / PAR amount (e.g. 10000)
# and its price as a PERCENTAGE OF PAR quoted per 100 (a mark of 100.146 == 100.146% of
# face). So a bond's true value is  qty * mark / 100 , NOT qty * mark — valuing it as
# qty * mark overstates it ~100x. Confirmed against live IBKR data 2026-08-05: for every
# BOND row the broker's own reported market_value equals qty * mark / 100 exactly. See
# `_is_bond` for the single detection point.
_BOND_CATEGORY = "BOND"


def _is_bond(asset_category) -> bool:
    """True iff a holdings row is an individual bond (asset_category == 'BOND'). Single
    source of truth for bond detection so valuation and order-exclusion never disagree."""
    return str(asset_category or "").strip().upper() == _BOND_CATEGORY


def _bond_value(qty: float, mark: float, market_value) -> float:
    """A bond's true dollar value. IBKR quotes bond price as a percent of par per 100, and
    the position quantity is the face amount, so value = qty * mark / 100. Falls back to the
    broker's reported market_value when the mark is missing/non-positive (never qty*mark)."""
    if mark == mark and mark > 0:          # mark is a real, positive per-100 price
        return qty * mark / 100.0
    return _to_float(market_value)


# --- NAV / holdings-value mismatch fail-safe (pre-arm exclusion) -----------------
# An account whose recorded NAV (roster ``total_value``) is wildly BELOW the value its own
# latest holdings + cash imply is EXCLUDED from the engine run, so it can never be sized or
# armed off a NAV that disagrees with reality — WHATEVER the cause (a departed/closed account
# whose positions transferred out while its last holdings snapshot is now stale; an upstream
# Flex-feed dropout; any other data desync). This is a FAIL-SAFE EXCLUSION, not a "heal" or
# "re-sync" instruction: it asserts nothing about which side is correct, only that the two
# disagree too much to size an order safely — a human reviews it.
#
# It is deliberately ONE-SIDED. Only ``total_value`` << holdings+cash is caught. The opposite
# (``total_value`` > holdings) is the NORMAL, CORRECT cash cushion — ``holdings_snapshots``
# records positions only and excludes cash, so a cash-heavy / under-deployed account rightly
# has total_value above its holdings sum and must NOT be flagged (sizing to total_value there
# is exactly what deploys the idle cash). Confirmed on live data 2026-08-05: the two departed
# APS Ventures accounts sit at total_value/(holdings+cash) ~= 0.001, while cash-heavy accounts
# sit at ~1.00 — a 0.5 cut cleanly separates them.
#
# CASH NOTE: the roster view does not currently expose cash_balance, so ``cash`` degrades to
# 0.0 (reference == holdings alone) unless a caller supplies it. That fallback is CONSERVATIVE:
# holdings <= holdings+cash, so a 0-cash reference can only ever flag a STRICT SUBSET — it never
# false-flags a healthy cash-heavy/normal account regardless of size. If cash_balance is later
# added to the roster row, the guard consumes it automatically and becomes fully cash-aware.
_NAV_MISMATCH_RATIO = 0.5              # flag when total_value < 50% of (holdings + cash)
_NAV_MISMATCH_MIN_REFERENCE = 1000.0  # ignore sub-$1k references (snapshot noise, immaterial)
_NAV_MISMATCH_REASON = ("NAV/holdings mismatch — manual review "
                        "(possible closed/departed account or data issue)")


def _is_nav_mismatch(total_value: float, reference: float) -> bool:
    """True iff recorded ``total_value`` is materially below the ``reference`` (holdings+cash)
    it should reflect — the fail-safe pre-arm exclusion test. One-sided (only the dangerous
    under-statement direction) and floored at a small reference so trivially tiny accounts are
    not flagged. Pure."""
    return (reference > _NAV_MISMATCH_MIN_REFERENCE
            and total_value < _NAV_MISMATCH_RATIO * reference)


def account_inputs_from_roster(roster_rows: list[Mapping],
                               holdings_by_account: Mapping[str, list[Mapping]],
                               ) -> tuple[list[dict], list[dict], list[dict]]:
    """Turn CRM roster rows + their latest holdings into ``rebalance_engine`` account_inputs.

    For each roster row:
      * account   = the IBKR number (crm_roster.account_identifier — CRM->desk identity map)
      * version   = the roster ``model`` (today uniformly "Growth")
      * positions = {symbol -> quantity}  from the account's latest holdings snapshot
                    (individual BONDS excluded — see below)
      * prices    = {symbol -> mark_price} from the same snapshot (positive marks only)
      * net_liq   = roster ``total_value``; falls back to the holdings market-value sum
      * bonds     = [{symbol, quantity, mark_price, value}] — individual bond holdings,
                    valued correctly (qty * mark / 100) and held OUT of the engine

    BONDS (order-affecting correctness, 2026-08-05). IBKR carries an individual bond with a
    FACE-VALUE quantity and a per-100 (percent-of-par) price, so valuing it as qty*mark — as
    the pure engine does for equities — overstates it ~100x and can't produce a placeable
    equity order for a CUSIP. So bonds are (a) valued correctly (qty*mark/100) into the
    account's holdings value / NAV, and (b) kept OUT of ``positions``/``prices`` — the engine
    never sees them, so it never inflates the account's valuation and never emits a broken
    bond leg. They are returned on ``bonds`` for the verdict to surface as MANUAL liquidation
    (S0 Growth holds no individual bonds — they are all sells-to-exit, done by a human). The
    account's non-bond legs still size against the corrected full NAV (which includes the
    bond value). An account with NO bonds is byte-identical to before.

    Returns ``(account_inputs, skipped, excluded)``.
      * ``skipped``  — accounts with no usable net_liq (unfunded / no snapshot): the read-only
        stand-in for 'not funded reality', kept OUT of the engine run and surfaced separately.
      * ``excluded`` — accounts whose recorded NAV (``total_value``) is wildly BELOW the
        holdings+cash it should reflect (see ``_is_nav_mismatch``). A FAIL-SAFE pre-arm
        exclusion — held OUT of the engine so they can NEVER be sized or armed off a NAV that
        disagrees with reality (a departed/closed account with a now-stale holdings snapshot, a
        feed dropout, or any other data desync), and surfaced for MANUAL review. Checked BEFORE
        the net_liq fallback so an account cannot be silently sized off a stale holdings sum.
    PURE."""
    account_inputs: list[dict] = []
    skipped: list[dict] = []
    excluded: list[dict] = []

    for row in roster_rows:
        aid = str(row.get("account_id"))
        account = crm_roster.account_identifier(row)
        version = row.get("model") or ""
        holds = holdings_by_account.get(aid, []) or []

        positions: dict[str, float] = {}
        prices: dict[str, float] = {}
        bonds: list[dict] = []
        holdings_value = 0.0
        for h in holds:
            sym = str(h.get("symbol") or "").strip()
            if not sym:
                continue
            qty = _to_float(h.get("quantity"))
            px = _to_float(h.get("mark_price"))
            if _is_bond(h.get("asset_category")):
                # Value the bond correctly (qty*mark/100) into NAV, but keep it OUT of the
                # engine's positions/prices: the equity engine can neither value nor route a
                # face-value/per-100 CUSIP. Surfaced separately for manual liquidation.
                value = _bond_value(qty, px, h.get("market_value"))
                bonds.append({"symbol": sym, "quantity": qty, "mark_price": px,
                              "value": value})
                holdings_value += value
                continue
            positions[sym] = positions.get(sym, 0.0) + qty
            if px == px and px > 0:
                prices[sym] = px
            holdings_value += _to_float(h.get("market_value"))

        base = {
            "account": account,
            "advisor_name": row.get("advisor_name"),
            "entity": row.get("entity"),
            "master_name": row.get("master_name"),
        }

        # FAIL-SAFE NAV/holdings mismatch guard (BEFORE the net_liq fallback): if the recorded
        # NAV is PRESENT but wildly below the holdings+cash it should reflect, EXCLUDE the
        # account from the engine so it can never be sized/armed off a NAV that disagrees with
        # reality. Gated on total_value being PRESENT: a genuinely-absent total_value (None) is
        # the legitimate "no roster NAV, fall back to holdings" case (a funded account whose NAV
        # column wasn't populated) — NOT a mismatch. A present total_value that is 0/tiny against
        # a large holdings snapshot IS the dangerous case (a departed/closed account, a feed
        # dropout) and is caught here, ahead of the fallback that would otherwise size it off a
        # possibly-stale holdings sum. (This book is unlevered — total = invested + cash with
        # cash >= 0 — so a NAV below half of holdings is never a legit margin account here; once
        # cash_balance is exposed on the roster row the reference becomes exact for that case too.)
        raw_present = row.get("total_value") is not None
        raw_total = _to_float(row.get("total_value"))
        cash = _to_float(row.get("cash_balance"))     # absent in the roster view today -> 0.0
        reference = holdings_value + cash
        if raw_present and _is_nav_mismatch(raw_total, reference):
            excluded.append({**base, "version": version,
                             "total_value": raw_total,
                             "holdings_value": holdings_value,
                             "cash_balance": cash,
                             "reference": reference,
                             "reason": _NAV_MISMATCH_REASON})
            continue

        net_liq = raw_total
        if not (net_liq > 0):
            net_liq = holdings_value

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
            "bonds": bonds,
        })

    return account_inputs, skipped, excluded


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
        # Individual bonds are excluded from the auto-generated equity legs (the engine
        # can't route them) and surfaced explicitly as MANUAL liquidation so a human acts on
        # them — never silently dropped. Holding one makes the account out-of-spec.
        bond_legs = [{"symbol": b["symbol"], "side": "SELL",
                      "quantity": b.get("quantity"), "value": b.get("value"),
                      "action": "manual liquidation required (bond)"}
                     for b in (ai.get("bonds") or [])]
        # AccountPlan.needs_rebalance is the engine's band verdict; treat any would-trade
        # leg (or a held bond needing manual liquidation) as out-of-spec too (defensive —
        # orders only fill when the band is breached).
        out_of_spec = (bool(getattr(p, "needs_rebalance", False))
                       or bool(legs) or bool(bond_legs))
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
            "n_bonds": len(bond_legs),
            "bonds": bond_legs,
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

        {"verdicts": [...], "skipped": [...], "excluded": [...], "n_accounts": int,
         "n_out_of_spec": int, "n_in_spec": int, "n_excluded": int}

    ``excluded`` carries any account held OUT of the run by the fail-safe NAV/holdings mismatch
    guard (recorded NAV wildly below holdings+cash — a departed/closed account or a data
    desync). Excluded accounts are NEVER sized or armed; they surface for manual review only.

    Builds and transmits NOTHING (``build_plan`` is the pure engine; no ``ib``, no arming)."""
    account_inputs, skipped, excluded = account_inputs_from_roster(
        roster_rows, holdings_by_account)
    if not account_inputs:
        return {"verdicts": [], "skipped": skipped, "excluded": excluded, "n_accounts": 0,
                "n_out_of_spec": 0, "n_in_spec": 0, "n_excluded": len(excluded)}

    result = rebalance_engine.build_plan(
        account_inputs, dict(targets), band_pct=band_pct, universe=universe)
    verdicts = verdicts_from_plans(result["plans"], account_inputs)
    n_oos = sum(1 for v in verdicts if v["out_of_spec"])
    return {
        "verdicts": verdicts,
        "skipped": skipped,
        "excluded": excluded,
        "n_accounts": len(verdicts),
        "n_out_of_spec": n_oos,
        "n_in_spec": len(verdicts) - n_oos,
        "n_excluded": len(excluded),
    }
