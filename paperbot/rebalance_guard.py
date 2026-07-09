"""
rebalance_guard.py — pre-stage safety gate for the AUTOMATED nightly rebalance path.

Imported by nightly_monitor_run.py (and re-called defensively by morning_execute_run.py
before it ever transmits). This module makes NO broker calls and NEVER connects to the
Gateway itself — it is pure validation over data the caller already has in hand (a
route list, account inputs, and the tier targets), plus one read of price-history files
via strategies.parts.regime.market_health_score (same file reads eod_report.py already
does for the 9PM email — no broker, no network).

FAIL CLOSED, always: any check this module cannot complete (missing data, an exception
computing the regime, an unrecognized symbol) is treated as a FAILURE, never a pass. A
trade list is staged for unattended morning execution ONLY if every check explicitly
passes. There is no silent default-to-pass path anywhere in here.

Three checks, each independently named/documented so the thresholds are easy to find
and tune later (with Andrew's blessing — these are automation SAFETY RAILS, not the
frozen strategy/regime knobs in strategies\\strategies\\config.py, so they are NOT
covered by the curve-fit freeze; they may be revisited as pilot experience accumulates):

  (a) TICKER ALLOW-LIST — every symbol in the trade list must be a ticker the shared S0
      strategy actually knows about (strategies.config.ALL_TICKERS, which is
      EQUITY_CORE + SECTORS + DEFENSIVE_ASSETS + REAL_ASSETS — the exact universe the
      backtester/paperbot draw target weights from). Anything else is unrecognized and
      the run fails closed rather than staging an unknown symbol.

  (b) TURNOVER / NOTIONAL CAP — for a MONTHLY-rebalance all-weather strategy, a single
      day's turnover should be a modest fraction of NAV (moving one asset class' weight
      by 5-15 points is a big month; the regime-band engine intentionally moves in
      graduated steps, not all-in/all-out swings). MAX_SINGLE_ACCOUNT_TURNOVER_PCT_NAV
      = 0.50 (50% of that account's NAV in one day's total buy+sell notional) is set
      well above ordinary monthly drift/rebalance turnover so it never fires on a normal
      month, but well below "the whole book flipped" — a level that should never happen
      organically and is worth a human's eyes before anything transmits unattended.

  (c) REGIME CROSS-CHECK — recompute today's regime the EXACT same way
      dailyreport/eod_report.py's build_s0_regime() does (same market_health_score() +
      apply_hysteresis() call over the same price/vix/hy_oas loaders) and compare it to
      the regime the caller says governed the trade list being staged. A mismatch means
      the nightly monitor's compute path and the 9PM email's compute path have drifted
      apart — exactly the kind of silent divergence that must never be allowed to
      authorize an unattended trade. Any exception while computing it is ALSO a fail
      (never assume "must be fine" on a broken read).
"""
from __future__ import annotations

from dataclasses import dataclass, field

# --- (a) allow-list source ------------------------------------------------------
def _known_universe() -> set[str]:
    """The exact ticker universe the shared S0 strategy draws target weights from.
    Read-only reference into strategies\\strategies\\config.py — never modified here."""
    from strategies import config as s_config
    return set(s_config.ALL_TICKERS)


# --- (b) turnover cap ------------------------------------------------------------
# Named constant so Andrew can tune it later without hunting through the module.
MAX_SINGLE_ACCOUNT_TURNOVER_PCT_NAV = 0.50   # 50% of that account's NAV, one day, one account


# --- (c) regime cross-check ------------------------------------------------------
def compute_regime_now() -> tuple[str | None, str | None, str | None]:
    """Recompute (raw_regime, confirmed_regime, as_of_iso) the SAME way
    dailyreport/eod_report.py's build_s0_regime() does — same market_health_score() +
    apply_hysteresis() call over the same price/vix/hy_oas loaders. No broker contact;
    file/parquet reads only. Returns (None, None, None) on ANY failure — the caller
    must treat that as fail-closed, never as "skip this check"."""
    try:
        from src import data_loader
        from strategies import config as s_config
        from strategies.parts import regime as s_regime

        prices = data_loader.load_prices()
        hyg = data_loader.load_prices([s_config.CREDIT_PROXY[0]])[s_config.CREDIT_PROXY[0]]
        denom_t = s_config.CREDIT_PROXY[1]
        credit_denom = (prices[denom_t] if denom_t in prices.columns
                        else data_loader.load_prices([denom_t])[denom_t])
        vix, _vix_src = data_loader.load_vix()
        hy_oas, _hy_oas_src = data_loader.load_hy_oas()

        score_df = s_regime.market_health_score(
            prices, hyg=hyg, credit_denom=credit_denom, vix=vix, hy_oas=hy_oas)
        confirmed = s_regime.apply_hysteresis(score_df["score"])

        as_of = score_df.index[-1]
        raw_regime = score_df.loc[as_of, "regime"]
        confirmed_regime = confirmed.iloc[-1]
        return str(raw_regime), str(confirmed_regime), as_of.strftime("%Y-%m-%d")
    except Exception:
        return None, None, None


# --- result type -------------------------------------------------------------------
@dataclass
class GuardResult:
    """Pass/fail verdict + plain-English reasons. `passed` is False unless EVERY check
    explicitly passed — there is no default-True path."""
    passed: bool
    reasons: list[str] = field(default_factory=list)
    detail: dict = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.passed


def check(routes: list, account_inputs: list[dict], prices_by_symbol: dict[str, float],
         claimed_regime: str | None = None) -> GuardResult:
    """Run all three guard checks over an already-computed trade list.

    routes             : list of rebalance_engine.RoutePlan (or any object exposing
                          .symbol, .side, .total_qty, .account, .per_account_split)
                          — exactly what rebalance_engine.build_plan()["routes"] returns.
    account_inputs      : the same list of dicts nightly_monitor_run built for
                          rebalance_run.build_preview (each has "account" and "net_liq").
    prices_by_symbol    : symbol -> reference price used to value each route's notional
                          (the same live/close prices the caller sized the trade with).
    claimed_regime      : the confirmed regime the caller computed when building this
                          trade list (e.g. from the same s_regime call site inline in
                          nightly_monitor_run). If None, the cross-check still runs and
                          reports today's regime, but cannot compare — that also FAILS
                          CLOSED (a staged trade must be able to name the regime it was
                          built under).

    Returns a GuardResult. NEVER raises — any internal exception is caught and folded
    into a failing reason, because a broken guard must never accidentally pass a trade."""
    reasons: list[str] = []
    detail: dict = {}

    # --- (a) ticker allow-list ---------------------------------------------------
    try:
        universe = _known_universe()
        bad_symbols = sorted({r.symbol for r in routes if r.symbol not in universe})
        if bad_symbols:
            reasons.append(
                f"ticker allow-list: unrecognized symbol(s) {bad_symbols} not in the S0 "
                f"universe ({len(universe)} known tickers). FAILING CLOSED.")
        detail["universe_size"] = len(universe)
        detail["bad_symbols"] = bad_symbols
    except Exception as exc:
        reasons.append(f"ticker allow-list: could not load the known universe "
                       f"({type(exc).__name__}: {exc}). FAILING CLOSED.")

    # --- (b) turnover / notional cap ---------------------------------------------
    try:
        net_liq_by_acct = {a["account"]: float(a.get("net_liq") or 0.0) for a in account_inputs}
        notional_by_acct: dict[str, float] = {}
        for r in routes:
            px = float(prices_by_symbol.get(r.symbol, float("nan")))
            if not (px == px and px > 0):
                reasons.append(
                    f"turnover check: no usable price for {r.symbol} — cannot value its "
                    f"notional. FAILING CLOSED.")
                continue
            total_qty = getattr(r, "total_qty", None)
            split = getattr(r, "per_account_split", None) or (
                {r.account: total_qty} if getattr(r, "account", None) and total_qty else {})
            if not split:
                reasons.append(
                    f"turnover check: route for {r.symbol} has no per_account_split and "
                    f"no (account, total_qty) to fall back to — cannot attribute its "
                    f"notional to any account. FAILING CLOSED.")
                continue
            for acct, qty in split.items():
                notional_by_acct[acct] = notional_by_acct.get(acct, 0.0) + abs(qty) * px

        breaches = []
        for acct, notional in sorted(notional_by_acct.items()):
            nav = net_liq_by_acct.get(acct)
            if not nav or nav <= 0:
                breaches.append(f"{acct}: no NAV on file for turnover denominator")
                continue
            pct = notional / nav
            if pct > MAX_SINGLE_ACCOUNT_TURNOVER_PCT_NAV:
                breaches.append(f"{acct}: turnover {pct:.1%} of NAV exceeds cap "
                                f"{MAX_SINGLE_ACCOUNT_TURNOVER_PCT_NAV:.0%}")
        if breaches:
            reasons.append("turnover cap breached: " + "; ".join(breaches) + ". FAILING CLOSED.")
        detail["notional_by_account"] = notional_by_acct
        detail["turnover_cap_pct_nav"] = MAX_SINGLE_ACCOUNT_TURNOVER_PCT_NAV
    except Exception as exc:
        reasons.append(f"turnover check: internal error ({type(exc).__name__}: {exc}). "
                       f"FAILING CLOSED.")

    # --- (c) regime cross-check ----------------------------------------------------
    try:
        raw_regime, confirmed_regime, as_of = compute_regime_now()
        detail["regime_now"] = {"raw": raw_regime, "confirmed": confirmed_regime, "as_of": as_of}
        if confirmed_regime is None:
            reasons.append("regime cross-check: could not compute today's regime "
                           "(market_health_score/apply_hysteresis failed). FAILING CLOSED.")
        elif claimed_regime is None:
            reasons.append("regime cross-check: caller did not supply the regime the trade "
                           "list was built under — cannot cross-check. FAILING CLOSED.")
        elif str(claimed_regime) != str(confirmed_regime):
            reasons.append(
                f"regime cross-check: trade list was built under regime "
                f"{claimed_regime!r} but the guard's independent recompute (same "
                f"method eod_report.py uses) says {confirmed_regime!r} as of {as_of}. "
                f"The nightly compute path and the EOD-email compute path have DRIFTED "
                f"APART. FAILING CLOSED.")
    except Exception as exc:
        reasons.append(f"regime cross-check: internal error ({type(exc).__name__}: {exc}). "
                       f"FAILING CLOSED.")

    return GuardResult(passed=(len(reasons) == 0), reasons=reasons, detail=detail)
