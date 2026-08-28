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

import holding_class
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


# --- HELD-ASIDE classification (the no-trade list) -------------------------------
# The CRM holdings view carries the instrument type in `asset_category`
# (v_tradingdesk_holdings_latest.asset_category == IBKR Flex `assetCategory`: 'STK',
# 'BOND', ...). That is the AUTHORITATIVE classification signal and it is handed straight
# to holding_class — this module makes no judgement of its own and never guesses off a
# symbol string. holding_class decides MANAGED vs HELD_ASIDE, owns the bond percent-of-par
# valuation convention, and fails closed on any type it does not recognise.
#
# HISTORY (what changed, 2026-08-19): bonds used to be dropped out of the engine's inputs
# here and surfaced as "manual liquidation required" — i.e. every bond was an implicit
# sell-to-exit, and in practice the five bond-holding accounts were held back from the
# batch entirely. The owner's decision replaced that: bonds are HELD ASIDE — priced,
# counted, reported, never traded — and they sit OUTSIDE the target allocation, so the
# model applies to the remaining sleeve as its own 100% and the account rebalances that
# sleeve normally. The carve-out itself now lives in the pure engine (which is what
# guarantees no leg can ever be emitted for one); this module only supplies the inputs.
def _sec_type(asset_category) -> str:
    """The instrument type for one holdings row, normalized. Blank/absent -> UNKNOWN, which
    holding_class treats as held-aside-and-flag-for-classification (fail closed)."""
    return holding_class.normalize_type(asset_category)


def _holding_value(qty: float, mark: float, sec_type: str, market_value) -> float | None:
    """One holding's true dollar value under its instrument type's price convention
    (a BOND's quantity is FACE and its mark is a percent of par per 100, so its value is
    qty * mark / 100 — never qty * mark, which overstates it ~100x). Delegates to
    holding_class.position_value so valuation and order-exclusion read the SAME table.
    Returns None when the row cannot be valued at all."""
    return holding_class.position_value(qty, mark, sec_type, reported_value=market_value)


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
                    (EVERY holding, held-aside ones included — the engine carves them out)
      * prices    = {symbol -> mark_price} for MANAGED holdings (positive marks only)
      * sec_types = {symbol -> instrument type} from ``asset_category`` — the engine's ONLY
                    classification input
      * values    = {symbol -> dollar market value} for HELD-ASIDE holdings, valued under
                    their own price convention (a bond's qty*mark/100). This is what lets
                    the engine carve them out without knowing anything about the CRM feed.
      * net_liq   = roster ``total_value``; falls back to the holdings market-value sum

    HELD-ASIDE HOLDINGS (owner decision, 2026-08-19). Individual bonds — and anything else
    holding_class classifies HELD_ASIDE — are on a NO-TRADE list, not a reason to bench the
    account. They are handed to the engine WITH their instrument type and their correct
    value; the engine prices them, carves their value out of NetLiq, applies the model to
    the remaining sleeve as its own 100%, and can never emit a leg for one. An account with
    no held-aside holdings produces byte-identical inputs and a byte-identical plan.

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
        sec_types: dict[str, str] = {}
        values: dict[str, float] = {}
        n_managed = 0
        n_held_aside = 0
        holdings_value = 0.0
        for h in holds:
            sym = str(h.get("symbol") or "").strip()
            if not sym:
                continue
            qty = _to_float(h.get("quantity"))
            px = _to_float(h.get("mark_price"))
            sec_type = _sec_type(h.get("asset_category"))
            positions[sym] = positions.get(sym, 0.0) + qty
            sec_types[sym] = sec_type
            if holding_class.is_held_aside(sec_type):
                # HELD ASIDE: priced under its own convention (a bond's qty*mark/100) and
                # handed to the engine as an explicit value so it can be carved out of
                # NetLiq. Never given a `prices` entry — nothing here will ever be sized.
                n_held_aside += 1
                value = _holding_value(qty, px, sec_type, h.get("market_value"))
                if value is not None:
                    values[sym] = values.get(sym, 0.0) + value
                    holdings_value += value
                continue
            n_managed += 1
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
            "sec_types": sec_types,
            "values": values,
            # carried through for display; the engine ignores unknown keys.
            "advisor_name": row.get("advisor_name"),
            "entity": row.get("entity"),
            "master_name": row.get("master_name"),
            # MANAGED holdings only — the held-aside ones are counted separately so a
            # bond can never be mistaken for a position the model is supposed to hold.
            "n_positions": n_managed,
            "n_held_aside": n_held_aside,
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
    is unit-testable with synthetic AccountPlans (no backtest).

    THE HELD-ASIDE HALF (accounting for them like professionals). Each verdict carries the
    account's held-aside block straight off the engine's plan — the authoritative
    classification, not a second opinion assembled here:

        managed_net_liq   what the model's 100% actually applies to
        held_aside_value  the priced total sitting outside the allocation
        held_aside        per-position symbol / quantity / market value / reason
        n_unclassified    held-aside positions a human must still identify

    and ``net_liq == managed_net_liq + held_aside_value``, so a reader can always see the
    whole account AND the part of it the desk trades. A held-aside holding does NOT make an
    account out-of-spec — it is not a defect, it is a documented no-trade holding, and the
    out-of-spec figure now describes the MANAGED sleeve alone. What DOES surface is
    ``blocked``: the engine withheld orders because a held-aside holding could not be priced
    or reconciled, which is a data problem needing a human."""
    meta = {ai["account"]: ai for ai in account_inputs}
    by_acct = {p.account: p for p in plans}
    verdicts: list[dict] = []
    for account, ai in meta.items():
        p = by_acct.get(account)
        if p is None:
            continue
        orders = getattr(p, "orders", {}) or {}
        legs = _legs_from_orders(orders)
        # The engine's own held-aside records: priced, counted, named, and structurally
        # incapable of having produced a leg (they never became reconcile lines at all).
        held = [h.as_dict() if hasattr(h, "as_dict") else dict(h)
                for h in (getattr(p, "held_aside", None) or [])]
        blocked_reasons = list(getattr(p, "blocked_reasons", None) or [])
        # A model symbol IBKR would not quote, that this account holds none of. Orders for
        # the REST of the account still stand (isolate, don't bench), but the account is NOT
        # in spec and cannot be made in spec until a price exists (v0.42.0).
        unpriced_reasons = list(getattr(p, "unpriced_reasons", None) or [])
        net_liq = float(getattr(p, "net_liq", ai.get("net_liq", 0.0)) or 0.0)
        held_value = float(getattr(p, "held_aside_value", 0.0) or 0.0)
        managed_net_liq = float(getattr(p, "managed_net_liq", None) or (net_liq - held_value))
        # AccountPlan.needs_rebalance is the engine's band verdict on the MANAGED sleeve;
        # treat any would-trade leg as out-of-spec too (defensive — orders only fill when
        # the band is breached), and a blocked account as needing attention.
        out_of_spec = (bool(getattr(p, "needs_rebalance", False))
                       or bool(legs) or bool(blocked_reasons) or bool(unpriced_reasons))
        verdicts.append({
            "account": account,
            "version": ai.get("version"),
            "advisor_name": ai.get("advisor_name"),
            "entity": ai.get("entity"),
            "master_name": ai.get("master_name"),
            "net_liq": net_liq,
            "managed_net_liq": managed_net_liq,
            "held_aside_value": held_value,
            "n_positions": ai.get("n_positions", 0),
            "out_of_spec": out_of_spec,
            "n_legs": len(legs),
            "legs": legs,
            "n_alien": len(getattr(p, "alien_lines", []) or []),
            "n_held_aside": len(held),
            "held_aside": held,
            "n_unclassified": sum(1 for h in held if h.get("needs_classification")),
            "blocked": bool(blocked_reasons),
            "blocked_reasons": blocked_reasons,
            "unpriced": bool(unpriced_reasons),
            "unpriced_reasons": unpriced_reasons,
        })
    verdicts.sort(key=lambda v: (not v["out_of_spec"], -v["net_liq"]))
    return verdicts


def scan_out_of_spec(roster_rows: list[Mapping],
                     holdings_by_account: Mapping[str, list[Mapping]],
                     targets: Mapping,
                     band_pct: Optional[float] = None,
                     universe: Optional[set] = None,
                     cash_reserve_pct_by_version: Optional[Mapping] = None) -> dict:
    """Whole-book out-of-spec read: assemble inputs, run the UNCHANGED pure
    ``rebalance_engine.build_plan``, and return per-account verdicts.

    ``targets`` maps version -> ``strategy_target.Target`` (the frozen desk model, e.g.
    ``strategy_target.current_target("Growth")``) — supplied by the caller so this stays pure
    and broker-free. Returns::

        {"verdicts": [...], "skipped": [...], "excluded": [...], "n_accounts": int,
         "n_out_of_spec": int, "n_in_spec": int, "n_excluded": int,
         "n_with_held_aside": int, "held_aside_value": float, "n_blocked": int}

    ``excluded`` carries any account held OUT of the run by the fail-safe NAV/holdings mismatch
    guard (recorded NAV wildly below holdings+cash — a departed/closed account or a data
    desync). Excluded accounts are NEVER sized or armed; they surface for manual review only.

    ``cash_reserve_pct_by_version`` maps version -> that model's standing cash reserve, for
    models that name their own (Andrew-authored custom allocations, 1%). A version not in the
    map gets the global default, which is S0's validated 1.5%. It is threaded straight into
    build_plan, which applies it to BOTH the sizing and the CASH-line drift for that account,
    so this readout claims the same reserve the executor would deploy against.

    Builds and transmits NOTHING (``build_plan`` is the pure engine; no ``ib``, no arming)."""
    account_inputs, skipped, excluded = account_inputs_from_roster(
        roster_rows, holdings_by_account)
    if not account_inputs:
        return {"verdicts": [], "skipped": skipped, "excluded": excluded, "n_accounts": 0,
                "n_out_of_spec": 0, "n_in_spec": 0, "n_excluded": len(excluded),
                "n_with_held_aside": 0, "n_blocked": 0, "held_aside_value": 0.0}

    result = rebalance_engine.build_plan(
        account_inputs, dict(targets), band_pct=band_pct, universe=universe,
        cash_reserve_pct_by_version=(dict(cash_reserve_pct_by_version)
                                     if cash_reserve_pct_by_version else None))
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
        # Book-level held-aside accounting: how many accounts hold something on the
        # no-trade list, what it is all worth, and how many had orders withheld for a
        # data reason. Held-aside money is reported, never silently absorbed into NAV.
        "n_with_held_aside": sum(1 for v in verdicts if v["n_held_aside"]),
        "held_aside_value": sum(v["held_aside_value"] for v in verdicts),
        "n_blocked": sum(1 for v in verdicts if v["blocked"]),
    }
