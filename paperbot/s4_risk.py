"""
s4_risk.py — S4-specific risk guard (PERMITS leverage up to the profile cap) + a
fail-closed MARGIN PREFLIGHT.

WHY A SIBLING (not an edit to risk_manager)
-------------------------------------------
The frozen risk_manager.evaluate hard-vetoes any book whose liquid reserve < cash_reserve_pct
— i.e. it vetoes ANY exposure above 1.0. That is correct for S0 and is FROZEN. S4 legitimately
runs exposure > 1.0 in calm markets (real margin borrow, Andrew's decision). So S4 gets its
OWN guard with the OPPOSITE default for the borrow: it PERMITS exposure up to the active
profile's leverage_cap and VETOES anything beyond it. Everything else stays a fat-finger
backstop (positive quantities, single-leg, sane notional).

Two independent gates, both must pass before S4 could ever transmit:
  1. evaluate_s4(...) — the per-run guard: exposure within [0, leverage_cap], borrow leg
     accounted, per-order sanity.
  2. margin_preflight(...) — a DEPLOY-TIME check against the LIVE account: refuses the
     leveraged (>1.0 exposure) path unless the account is CONFIRMED a margin account with
     sufficient buying power. The un-levered conservative path (exposure <= 1.0) is allowed
     on any account type. This function is PURE over an account-summary mapping so it is
     testable offline; the driver reads accountSummary and feeds it in AT DEPLOY, never now.

Neither function connects, transmits, or edits any frozen module.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# A tiny epsilon so a cap of exactly 1.50 is not tripped by float noise at 1.5000000001.
_EPS = 1e-9

# What we consider a margin-capable IBKR account type. IBKR reports AccountType as strings
# like "MARGIN", "REG T MARGIN", "PORTFOLIO MARGIN"; a cash account reports "CASH" / "REG T
# CASH". We require the word "MARGIN" and the ABSENCE of a pure "CASH" type.
_MARGIN_TOKENS = ("MARGIN",)
_CASH_TOKENS = ("CASH",)


@dataclass
class S4Verdict:
    ok: bool
    exposure: float
    leverage_cap: float
    reasons: list = field(default_factory=list)
    order_vetoes: list = field(default_factory=list)   # (symbol, reason) per bad order


def evaluate_s4(nav: float, target, intents, leverage_cap: float) -> S4Verdict:
    """S4 pre-trade guard. PERMITS exposure up to `leverage_cap`; VETOES beyond it.

    nav        : account NetLiquidation (>0).
    target     : the S4 Target (its SPY weight IS the exposure).
    intents    : list of s4_sizing.S4Intent (duck-typed: symbol/side/quantity/limit_price/
                 target_weight/is_borrow_leg/legs).
    leverage_cap : the active profile's cap (e.g. 1.5 balanced, 1.5 conservative-but-unused).

    A fragile-win guard, not a tuning knob: the cap is the PROFILE's cap, passed in — this
    guard never invents a limit."""
    from s4_sizing import RISK_TICKER

    reasons: list[str] = []
    exposure = float(target.weights.get(RISK_TICKER, 0.0))

    if not nav or nav <= 0:
        reasons.append("non-positive NAV")

    # THE leverage gate: exposure must be within [0, leverage_cap]. Above cap -> veto
    # (the shared brain already clips at cap, so an over-cap exposure means something
    # upstream is wrong — fail closed). Below 0 is nonsensical for a long-only risk leg.
    if exposure < -_EPS:
        reasons.append(f"negative SPY exposure {exposure:.4f} (nonsensical)")
    if exposure > leverage_cap + _EPS:
        reasons.append(
            f"exposure {exposure:.4f}x exceeds leverage_cap {leverage_cap:.2f}x "
            f"(book would lever past the profile limit) — VETO")

    # Per-order sanity (fat-finger backstop). The BORROW leg (quantity 0, side BORROW) is
    # exempt — it is financing, not a share order.
    order_vetoes: list[tuple[str, str]] = []
    for o in intents:
        if getattr(o, "is_borrow_leg", False):
            continue
        if o.quantity <= 0:
            order_vetoes.append((o.symbol, "non-positive quantity"))
        if getattr(o, "legs", 1) > 1:
            order_vetoes.append((o.symbol, f"{o.legs} legs > 1 (S4 trades single ETFs)"))
        px = float(o.limit_price or 0.0)
        if px <= 0:
            order_vetoes.append((o.symbol, "non-positive limit price"))

    ok = not reasons and not order_vetoes
    return S4Verdict(ok=ok, exposure=exposure, leverage_cap=leverage_cap,
                     reasons=reasons, order_vetoes=order_vetoes)


# --- MARGIN PREFLIGHT (deploy-time; PURE over the summary) --------------------------
@dataclass
class PreflightResult:
    ok: bool                       # True => this exposure path may proceed on this account
    exposure: float
    account_type: str
    is_margin: bool
    buying_power: float
    excess_liquidity: float
    required_notional: float       # NAV * exposure the SPY leg needs
    reasons: list = field(default_factory=list)


def _summary_map(summary) -> dict:
    """Accept either a {tag: value} dict or an ib_async accountSummary() list of rows
    (each with .tag and .value) and return {tag: value_str}."""
    if isinstance(summary, dict):
        return {str(k): str(v) for k, v in summary.items()}
    out: dict[str, str] = {}
    for row in summary:
        tag = getattr(row, "tag", None)
        val = getattr(row, "value", None)
        if tag is not None:
            out[str(tag)] = str(val)
    return out


def _num(m: dict, tag: str, default: float = 0.0) -> float:
    try:
        return float(m.get(tag, default))
    except (TypeError, ValueError):
        return default


def account_is_margin(account_type: str) -> bool:
    """True if the reported AccountType string denotes a margin-capable account.

    Requires a MARGIN token; a type that is purely CASH (no MARGIN token) is NOT margin.
    (IBKR uses e.g. "MARGIN", "REG T MARGIN", "PORTFOLIO MARGIN" vs "CASH"/"REG T CASH".)"""
    t = (account_type or "").upper()
    if any(tok in t for tok in _MARGIN_TOKENS):
        return True
    if any(tok in t for tok in _CASH_TOKENS):
        return False
    return False   # unknown/blank -> treat as NON-margin (fail closed)


def margin_preflight(summary, nav: float, exposure: float, leverage_cap: float) -> PreflightResult:
    """DEPLOY-TIME gate. Given the account's live accountSummary (AccountType, BuyingPower,
    ExcessLiquidity) and the intended exposure, decide whether the run may proceed.

    Policy (fail closed):
      * exposure <= 1.0  -> ALLOWED on ANY account type (no borrow needed). Still checks NAV>0.
      * exposure  > 1.0  -> ALLOWED ONLY IF the account is a confirmed MARGIN account AND has
        sufficient buying power / excess liquidity to carry the levered SPY notional. Any
        failure (cash account, unknown type, thin BP, exposure over cap) -> REFUSE.

    `summary` may be a dict {tag: value} (tests) or an ib_async accountSummary list (deploy).
    NEVER connects; the caller reads the summary and passes it in."""
    m = _summary_map(summary)
    account_type = m.get("AccountType", "")
    is_margin = account_is_margin(account_type)
    buying_power = _num(m, "BuyingPower")
    excess_liq = _num(m, "ExcessLiquidity")
    required_notional = float(nav) * float(exposure)

    reasons: list[str] = []
    if not nav or nav <= 0:
        reasons.append("non-positive NAV")
    if exposure > leverage_cap + _EPS:
        reasons.append(f"exposure {exposure:.4f}x exceeds leverage_cap {leverage_cap:.2f}x")

    levered = exposure > 1.0 + _EPS
    if levered:
        if not is_margin:
            reasons.append(
                f"leveraged path (exposure {exposure:.4f}x > 1.0) requires a MARGIN account, "
                f"but AccountType={account_type!r} is not margin — REFUSING")
        # Buying power must cover the SPY notional we intend to hold. IBKR BuyingPower on a
        # Reg-T margin account is ~4x overnight-eligible equity intraday / ~2x overnight; we
        # require it simply to cover the notional we ask for (a conservative floor). Excess
        # liquidity must be positive (a non-negative maintenance-margin cushion).
        if buying_power < required_notional - _EPS:
            reasons.append(
                f"insufficient BuyingPower {buying_power:,.0f} < required SPY notional "
                f"{required_notional:,.0f} — REFUSING")
        if excess_liq <= 0:
            reasons.append(
                f"non-positive ExcessLiquidity {excess_liq:,.0f} (no margin cushion) — REFUSING")

    ok = not reasons
    return PreflightResult(
        ok=ok, exposure=exposure, account_type=account_type, is_margin=is_margin,
        buying_power=buying_power, excess_liquidity=excess_liq,
        required_notional=required_notional, reasons=reasons)
