r"""page_models.py — "Strategy Models — what each model holds and why". READ-ONLY.

ONE page where the desk menu used to carry THREE overlapping ones (merged 2026-09-08).
The three were "Strategy 0 — Model & Parameters" (S0's rulebook plus today's live regime
read), "Strategy Models — all models" (every model's resolved holdings) and "Custom
allocation" (the models Andrew writes himself, the accounts on them, their drift and the
trades that would correct it). They all answered the same question — what does a model
hold, and why — so they are now one page, and NOTHING any of them showed was dropped:

  * Strategy 0's rulebook is here in full: the regime ladder, the re-entry ladder, the
    whipsaw controls, the client-version allowances and the traded ticker universe, plus
    today's live regime read (the score, the three components behind it, the raw versus
    confirmed regime, the live version's equity band, the next scheduled monthly rebalance
    and which data source each stress input came from).
  * Every model's resolved holdings are here, for both families of model.
  * The two validations only the desk can perform are here per hand-written model (ticker
    priceability and whole-share viability), together with which accounts are assigned to
    each model, how far they have drifted, and the trades that WOULD bring them back.

PROGRESSIVE DISCLOSURE. The page opens with a plain list of every model the desk knows
about — one row each, whichever family it belongs to — so the whole picture fits on one
screen. All the deep detail sits behind a collapsed section per model, plus one collapsed
section for the shared engine and one for how today's score was worked out.

TWO FAMILIES OF MODEL, NEVER MIXED. The book that is actually traded runs mostly on the
models Andrew writes HIMSELF in the client system, so the page is honest about the word
"every":
  * The computed family keeps the "S0 " prefix on its DISPLAY label ("S0 Growth", "S0
    Balanced", "S0 Conservative", "S0 Growth (Small)") so a computed Growth can never be
    read as the hand-written Growth. The prefix is display text ONLY — the `version` value
    the engine resolves against stays the frozen identifier ("Growth", "Balanced",
    "Conservative"), untouched.
  * The hand-written family is read LIVE from the client system through ONE loader
    (`_load_custom_state`) — two SELECTs against read-only views under the
    `tradingdesk_readonly` role. If the client system cannot be reached, that section says
    so in one plain sentence and everything else on the page still renders in full.

Two non-negotiables shape this page (CLAUDE.md §"two non-negotiables"):
  * RULE #1 (never curve-fit): every weight, band, threshold, gate, allowance and ticker is
    IMPORTED LIVE — resolved holdings come from `strategy_target.current_target(version)` →
    `run_backtest` (the exact validated engine the paperbot executes), the ladders /
    allowances / universe come from the frozen `strategies.config`, the regime read comes
    from the same `strategies.parts.regime` functions the nightly report and the paperbot
    call, and the hand-written books come from the client system's own view. NOTHING is
    hardcoded, so this view cannot silently disagree with the executed book.
  * READ-ONLY, AND STRUCTURALLY INCAPABLE OF TRANSMITTING. Not "guarded", not "gated" —
    incapable, and the suite enforces it (test_models_page_safety.py):
      - EXACTLY ONE control, and it only clears a cache. The page carries a single button,
        "Re-read the models Andrew writes himself from the client system", which discards
        this page's own 10-minute cached read so the next render fetches the published
        allocations again. That is the whole of its effect. There is NO free-text box a
        confirm phrase could be typed into, no number or selection input, and no second
        button of any kind — so there is nothing here that could be relabelled into an arm,
        send, execute or confirm affordance later. The suite enforces both halves: the one
        control must exist, and NOTHING else may appear beside it.
      - It spawns NO process. There is no shell-out of any kind, so it cannot invoke an
        executor with or without an arm flag. The whole drift preview is built IN PROCESS
        from the pure `rebalance_engine.build_plan` path (via
        `crm_outofspec.scan_out_of_spec`, the same posture `crm_execute.preview_crm` uses:
        no `ib`, `armed=False`).
      - It opens no broker socket and holds no arm token. Every client-system read is a
        SELECT under the read-only role, and the seam is one way: this page never writes to
        the client system and never publishes or edits an allocation.
    Model changes are a LATER stage: a computed model flows through the gated review →
    validate → deploy pipeline, and a hand-written model changes only when Andrew publishes
    a new version in the client system. Neither is ever an in-app edit.

WHAT THIS PAGE OWNS THAT NOBODY ELSE CAN (the deferred validations). The client system can
enforce that weights sum to 100 and that a published version is immutable. It CANNOT know
whether the desk can actually price and buy the tickers, because only the desk has price
history and only the desk sizes whole shares. So two checks live here and nowhere else:
  1. TICKER PRICEABILITY. `custom_target.build_target` raises `CustomAllocationError`
     naming a ticker with no usable price history (its trap 3). A silently unpriced leg
     becomes target_shares=0 — never bought, never band-breaching, invisible forever. This
     page surfaces that error VERBATIM and loudly, per model. It never swallows it.
  2. WHOLE-SHARE VIABILITY. The "(Small, ...)" models exist for accounts under $25,000,
     where a 3% sleeve of a $180 fund rounds to zero shares. This page computes, per
     ticker, how many WHOLE shares the smallest assigned account would actually buy, and
     flags every position that rounds to nothing. With no account assigned yet it runs the
     same check against a clearly-labelled EXAMPLE account size.

This module holds a small DISPLAY-ONLY model registry (which frozen version/tier to
resolve and show, and which hand-written labels the client system defines). It holds no
weights and steers no decision — adding a row surfaces a model, it does not define one.
"S0 Growth (Small)" is APPROVED but NOT YET DEPLOYED in `strategies/`, so it is shown as a
clearly labelled PROPOSED card — never fabricated as live.

IMPORT DISCIPLINE: module-top imports are CHEAP only (stdlib, pandas, streamlit, theme).
Every heavy import — the frozen config, the regime engine, the resolver/backtester,
custom_target, crm_roster, crm_outofspec — is LAZY and cached, so importing this module
opens no socket and runs no engine, and opening the page runs no backtest needlessly.
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import io
import math
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

import theme

# --- Make the existing packages importable (reuse, don't rebuild) --------------
# Same sys.path bootstrap desk_app.py uses. This module lives at
# dashboard/desk/page_models.py, so the repo root is parents[2].
REPO = Path(__file__).resolve().parents[2]
for _sub in ("paperbot", "backtester", "connections", "strategies", "dailyreport",
             "livebot"):
    _p = REPO / _sub
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
_conn = REPO / "connections"
if str(_conn) not in sys.path:
    sys.path.insert(0, str(_conn))


# =========================================================================== #
# Display-only vocabulary and registries. No weights, no decisions.           #
# =========================================================================== #
# The three client versions, most-defensive -> most-aggressive. S0 LIVE runs "Growth".
VERSION_ORDER = ("Conservative", "Balanced", "Growth")
LIVE_VERSION = "Growth"

# Plain-English name for each regime bucket (spelled out, no shorthand).
REGIME_PLAIN = {
    "RiskOn": "Risk-On",
    "RiskOnNarrowing": "Risk-On narrowing",
    "Caution": "Caution",
    "Defensive": "Defensive",
    "CapitalPreservation": "Capital preservation",
}

# Display-only colour grade for the regime ladder (green -> red). These tint the
# rows; they are NOT strategy knobs — they steer no decision, only the eye.
REGIME_COLOR = {
    "RiskOn": "#3fb950",
    "RiskOnNarrowing": "#a3c93a",
    "Caution": "#d29922",
    "Defensive": "#db6d28",
    "CapitalPreservation": "#f85149",
}

# Plain-English gloss for each component of the Market Health Score.
COMPONENT_GLOSS = {
    "trend": ("Broad equity trend (S&P 500 above its 200-day and 10-month averages, "
              "positive 6-month return, positive 200-day slope)"),
    "breadth": ("Market breadth (how many of the 11 sectors are above their 200-day "
                "average, plus whether the equal-weight S&P is leading the "
                "cap-weight S&P)"),
    "stress": ("Market stress (credit spreads calm and volatility below its own "
               "trend — full marks when calm)"),
}

# Plain-English description of each re-entry stage's ENTRY GATE. The NUMBERS these
# reference (equity percent, the stage-4 score, the stage-3 sector count) are pulled
# LIVE from config in the renderer; this text only explains what each stage waits for.
REENTRY_GATE_TEXT = {
    1: ("S&P 500 back above its 50-day average, the leading sectors improving, and "
        "volatility no longer rising."),
    2: ("S&P 500 back above its 200-day / 10-month average, OR the health score back "
        "above 40."),
    3: ("At least the required number of the 11 sectors back above their 200-day "
        "average, OR market breadth materially improving."),
    4: ("Full re-entry — the health score back above the version's gate (below) AND "
        "both credit and volatility normalised."),
}

# Which frozen version/tier to resolve and render. Descriptive metadata only — no
# weights, no decision. Growth-Small is PROPOSED (undefined in strategies/); its
# intended holdings live here purely as display text, flagged not-yet-deployed.
#
# THE "S0 " PREFIX IS DISPLAY TEXT ONLY. `label` is what the screen shows; `version` is the
# frozen identifier the engine resolves against and MUST stay exactly "Growth" / "Balanced"
# / "Conservative". The prefix exists because Andrew also hand-writes a model called
# "Growth (Custom)" — without it the page would show two different books both called
# "Growth". Never fold the prefix into `version`.
MODELS = [
    {
        "key": "growth", "label": "S0 Growth", "version": "Growth", "status": "live",
        "note": "Strategy 0 (Adaptive All-Weather Core) runs this version live on the "
                "paper account. Highest equity allowance, no minimum cash floor.",
    },
    {
        "key": "balanced", "label": "S0 Balanced", "version": "Balanced",
        "status": "resolves",
        "note": "Same engine as S0 Growth with a 5% minimum cash (T-bill) floor — resolves "
                "to a distinct book (more USFR, less equity per sleeve).",
    },
    {
        "key": "conservative", "label": "S0 Conservative", "version": "Conservative",
        "status": "resolves",
        "note": "Same engine with an 80% equity allowance and a 10% cash floor — the "
                "most defensive version; resolves to a distinct, lower-equity book.",
    },
    {
        "key": "growth_small", "label": "S0 Growth (Small)", "version": "Growth",
        "status": "proposed",
        "note": "A whole-share-feasible PROXY of S0 Growth for small accounts — S0 Growth's "
                "engine, version, regime band and re-entry ladder, with the equity sleeve "
                "collapsed to one cheap total-market ETF (SCHB) and the defensive sleeve "
                "to the same USFR cash instrument Growth already uses.",
        # PROPOSED display-only holdings (intended shape). NOT resolved from code —
        # Growth-Small is not yet defined in strategies/. The 85/15 is the headline
        # split; in production it would track Growth's dynamic equity/defensive split.
        "proposed_rows": [
            ("SCHB", 0.85, "Equity core (total US market — proxy for SPY/VTI/RSP)"),
            ("USFR", 0.15, "Defensive (floating-rate Treasury cash — same as Growth)"),
        ],
        "proposed_universe": ("SCHB", "USFR"),
    },
]

# The six custom models defined in the client system. This list exists so a model with
# NOTHING PUBLISHED YET can still be shown and said so plainly — a label that has no rows
# in the client system's view is otherwise invisible. It holds NO weights and steers NO
# decision; dispatch is never name-based (custom_target trap 1). Any label the client
# system publishes that is not in this list is picked up and shown anyway (see
# _load_custom_state).
CUSTOM_MODEL_LABELS: tuple[str, ...] = (
    "Growth (Custom)",
    "Balanced (Custom)",
    "Conservative (Custom)",
    "Growth (Small, Custom)",
    "Balanced (Small, Custom)",
    "Conservative (Small, Custom)",
)

# Which of them are the small-account tier. DISPLAY ONLY — it decides which EXAMPLE account
# size the whole-share check uses when no account is assigned yet, and nothing else. No
# routing, sizing, or dispatch decision anywhere reads this.
SMALL_MODEL_LABELS: frozenset[str] = frozenset({
    "Growth (Small, Custom)",
    "Balanced (Small, Custom)",
    "Conservative (Small, Custom)",
})

# Stated EXAMPLE account sizes for the whole-share check when a model has no assigned
# account to check against. $25,000 is the small-account tier boundary the desk already
# uses; $5,000 is a realistic small account well inside it.
EXAMPLE_NAV_SMALL = 5_000.0
EXAMPLE_NAV_FULL = 25_000.0

# How long the whole client-system read (view + targets + engine run) is held before it is
# read again. The read renews itself on its own once this many seconds have passed, the age
# of the read is STATED on screen, AND the page's one control re-reads it on demand — so a
# model Andrew has just published in the client system can be seen immediately instead of
# after a wait of up to this long.
_CACHE_TTL_SECS = 600


# =========================================================================== #
# PURE helpers — no Streamlit, no client system, no engine. Unit-tested directly. #
# =========================================================================== #
def _is_small_model(label: str) -> bool:
    """True for the small-account tier of the custom models. Display-only (see
    SMALL_MODEL_LABELS): it picks an example account size for the whole-share check."""
    return str(label) in SMALL_MODEL_LABELS


def _pct_rows(rows) -> tuple[list[tuple[str, float]], float]:
    """Allocation rows -> ([(ticker, weight_as_fraction), ...], total_percent).

    Reads the client system view's own columns (``ticker``, ``weight_pct``) and nothing
    else, so the displayed book is the published book. Percentages are converted to the
    FRACTION unit ``_render_holdings_table`` renders, and the total is returned in PERCENT
    so a book that does not sum to 100 is visible on screen rather than silently
    normalised."""
    out: list[tuple[str, float]] = []
    total_pct = 0.0
    for r in rows:
        ticker = str(r["ticker"]).strip().upper()
        pct = float(r["weight_pct"])
        total_pct += pct
        out.append((ticker, pct / 100.0))
    out.sort(key=lambda t: t[1], reverse=True)
    return out, total_pct


def whole_share_rows(weights, prices, nav: float) -> list[dict]:
    """THE whole-share viability check (validation C-2). PURE.

    For an account worth ``nav`` on a book of ``weights`` (ticker -> fraction of NAV) at
    ``prices`` (ticker -> last close), how many WHOLE shares of each position does the
    account actually buy? A position whose dollar target is smaller than one share rounds
    to ZERO — the account simply never holds it, silently, and the miss does not breach any
    rebalance band. That is precisely the failure the small-account tier exists to avoid, so
    it is computed here and flagged.

    Returns one dict per ticker: target weight, target dollars, price, whole shares, the
    dollars actually invested, the leftover, and ``buyable`` (False iff it rounds to zero or
    has no usable price)."""
    out: list[dict] = []
    for tkr in sorted(weights):
        weight = float(weights[tkr])
        dollars = float(nav) * weight
        raw = prices.get(tkr) if hasattr(prices, "get") else None
        try:
            price = float(raw)
        except (TypeError, ValueError):
            price = float("nan")
        if not (price == price) or price <= 0:      # NaN or non-positive
            out.append({"ticker": str(tkr), "target_weight": weight,
                        "target_dollars": dollars, "price": None, "whole_shares": None,
                        "invested": 0.0, "leftover": dollars, "buyable": False,
                        "note": "no usable price — the desk cannot size this position"})
            continue
        shares = int(math.floor(dollars / price))
        out.append({"ticker": str(tkr), "target_weight": weight,
                    "target_dollars": dollars, "price": price, "whole_shares": shares,
                    "invested": shares * price, "leftover": dollars - shares * price,
                    "buyable": shares > 0,
                    "note": "" if shares > 0 else
                            "rounds to ZERO shares — this account would never hold it"})
    return out


def min_nav_for_whole_book(weights, prices) -> float | None:
    """The smallest account value at which EVERY position in the book buys at least one
    whole share = max(price / weight) over the book. PURE. None if any leg is unpriced (the
    question has no answer then). This is the plain-English "how small can an account be
    and still hold this model properly?" number."""
    worst = 0.0
    for tkr in weights:
        weight = float(weights[tkr])
        if weight <= 0:
            continue
        raw = prices.get(tkr) if hasattr(prices, "get") else None
        try:
            price = float(raw)
        except (TypeError, ValueError):
            return None
        if not (price == price) or price <= 0:
            return None
        worst = max(worst, price / weight)
    return worst or None


def scan_accounts_for_model(target, roster_rows, holdings_by_account,
                            universe=None, cash_reserve_pct=None) -> dict:
    """Drift + would-trade preview for the accounts assigned to ONE custom model. PURE-ish:
    it runs the UNCHANGED pure engine and contacts nothing.

    Delegates verbatim to ``crm_outofspec.scan_out_of_spec`` — the same read-only whole-book
    path the Control Plane's out-of-spec panel uses (``rebalance_engine.build_plan`` with no
    ``ib`` and ``armed=False``: it builds and transmits nothing by construction). The only
    difference is the target handed in: this one is Andrew's hand-authored book instead of a
    computed S0 one, keyed by ``target.version`` (which custom_target guarantees IS the model
    label, verbatim — its trap 2).

    ``universe`` is the tradeable set (custom_target trap 4). Passing it matters: a symbol
    outside the universe is classified ALIEN and can never produce a would-trade leg, so a
    preview built with the wrong universe would show "nothing to do" for an account that
    actually needs rotating.

    ``cash_reserve_pct`` is the model's standing cash reserve. Every target this page hands
    in was built from rows in the client system's custom-allocation view — that IS the
    source-based test — so it defaults to the CUSTOM reserve (1%), which is what a
    hand-authored book actually deploys against. Leaving it at the desk-wide 1.5% here would
    show every correctly-invested custom account as permanently 0.5% adrift on cash and
    over-state every would-trade BUY."""
    import crm_outofspec
    import investable

    if cash_reserve_pct is None:
        cash_reserve_pct = investable.buffer_pct_for(is_custom=True)
    return crm_outofspec.scan_out_of_spec(
        list(roster_rows), dict(holdings_by_account), {str(target.version): target},
        universe=universe,
        cash_reserve_pct_by_version={str(target.version): float(cash_reserve_pct)})


def _fmt_date(value) -> str:
    """A date/timestamp from the client system rendered plainly; '—' when absent."""
    if value is None:
        return "—"
    try:
        return pd.Timestamp(value).strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001 — display only, never take the page down
        return str(value)


def _fmt_datetime(value) -> str:
    if value is None:
        return "—"
    try:
        return pd.Timestamp(value).strftime("%Y-%m-%d %H:%M")
    except Exception:  # noqa: BLE001
        return str(value)


def _pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def _sleeve_of(ticker: str) -> str:
    """Plain-English sleeve label for a ticker, from the frozen config groupings."""
    from strategies import config as scfg

    if ticker in scfg.EQUITY_CORE:
        return "Equity core (broad US market)"
    if ticker in scfg.SECTORS:
        return "Sector fund"
    if ticker in scfg.REAL_ASSETS:
        return "Real asset (inflation hedge)"
    if ticker in scfg.DEFENSIVE_ASSETS:
        return "Defensive (cash / Treasuries)"
    return "—"


def _next_rebalance_dates(today: _dt.date | None = None) -> tuple[str, str] | None:
    """The next scheduled MONTHLY rebalance: the signal date (last trading day of the
    month, SPEC §3) and its T+1 execution date. Derived from the shared market
    calendar — no hardcoded dates. Returns (signal_iso, exec_iso) or None on failure."""
    try:
        from connections import market_calendar as mc

        today = today or _dt.date.today()

        def _month_end(d: _dt.date) -> _dt.date:
            nxt = (_dt.date(d.year + 1, 1, 1) if d.month == 12
                   else _dt.date(d.year, d.month + 1, 1))
            return nxt - _dt.timedelta(days=1)

        signal = mc.last_trading_day(_month_end(today))
        if signal <= today:  # this month's signal already passed -> next month
            nxt = _dt.date(today.year + (today.month == 12),
                           (today.month % 12) + 1, 1)
            signal = mc.last_trading_day(_month_end(nxt))
        exec_day = mc.next_trading_day(signal)
        return (signal.isoformat(), exec_day.isoformat())
    except Exception:  # noqa: BLE001
        return None


# =========================================================================== #
# The three cached reads. All read-only; none of them ever raises to the page. #
# =========================================================================== #
@st.cache_data(ttl=1800,
               show_spinner="Reading today's regime (validated engine, cached 30 min)...")
def _compute_live_state() -> dict:
    """Compute TODAY's Market Health Score, its three components, the raw regime the
    score maps to, and the confirmed regime after hysteresis — through the EXACT
    functions the live path uses (regime.market_health_score / classify_regime /
    apply_hysteresis) over the same end-of-day price/macro data the strategy loads.

    This mirrors dailyreport.eod_report.build_s0_regime's compute path (the nightly
    publisher) so the two can never disagree. It is NOT a second backtest — just the
    sub-second causal score computation. Read-only: writes nothing, touches no broker.
    Returns a plain dict (never raises to the page; failure -> {"error": ...})."""
    try:
        from src import data_loader
        from strategies import config as scfg
        from strategies.parts import regime as sregime

        prices = data_loader.load_prices()
        hyg = data_loader.load_prices([scfg.CREDIT_PROXY[0]])[scfg.CREDIT_PROXY[0]]
        denom_t = scfg.CREDIT_PROXY[1]
        credit_denom = (prices[denom_t] if denom_t in prices.columns
                        else data_loader.load_prices([denom_t])[denom_t])
        vix, vix_src = data_loader.load_vix()
        hy_oas, hy_oas_src = data_loader.load_hy_oas()

        with contextlib.redirect_stdout(io.StringIO()):
            score_df = sregime.market_health_score(
                prices, hyg=hyg, credit_denom=credit_denom, vix=vix, hy_oas=hy_oas)
            confirmed = sregime.apply_hysteresis(score_df["score"])

        # "Data as-of" = the OLDEST last-real-value date across the required inputs
        # (same rule as the nightly publisher, so a NaN row can't read "fresh").
        req = [c for c in (["SPY", "RSP"] + list(scfg.SECTORS)) if c in prices.columns]
        real_dates = [prices[c].last_valid_index() for c in req]
        real_dates += [hyg.last_valid_index(), credit_denom.last_valid_index()]
        real_dates = [d for d in real_dates if d is not None]
        as_of = min(real_dates) if real_dates else score_df.index[-1]
        last = score_df.loc[as_of]

        raw_regime = str(last["regime"])
        confirmed_regime = str(confirmed.iloc[-1])
        growth_allow = scfg.CLIENT_VERSIONS[LIVE_VERSION]["equity_allowance"]
        band_lo, band_hi = sregime.equity_band(confirmed_regime)
        # Growth allowance scales the regime band (SPEC §10).
        g_lo, g_hi = band_lo * growth_allow, band_hi * growth_allow

        return {
            "as_of": as_of.strftime("%Y-%m-%d"),
            "score": float(last["score"]),
            "trend": float(last["trend"]),
            "breadth": float(last["breadth"]),
            "stress": float(last["stress"]),
            "raw_regime": raw_regime,
            "confirmed_regime": confirmed_regime,
            "band_lo": band_lo, "band_hi": band_hi,
            "growth_lo": g_lo, "growth_hi": g_hi,
            "growth_allow": growth_allow,
            "vix_src": vix_src, "hy_oas_src": hy_oas_src,
        }
    except Exception as exc:  # noqa: BLE001 — never take the page down
        return {"error": f"{type(exc).__name__}: {exc}"}


@st.cache_data(ttl=1800,
               show_spinner="Resolving live model holdings (validated engine, cached 30 min)...")
def _resolve_holdings(version: str) -> dict:
    """Run the shared brain through today for `version` and return its latest target
    book — the SAME `strategy_target.current_target(version)` → `run_backtest` path the
    paperbot uses, so the page's holdings ARE the executed holdings. Read-only: loads
    prices and computes, touches no broker. Never raises to the page (-> {"error": ...})."""
    try:
        import strategy_target  # paperbot module, on sys.path via the bootstrap above

        t = strategy_target.current_target(version)
        rows = sorted(((str(tk), float(w)) for tk, w in t.weights.items()),
                      key=lambda r: r[1], reverse=True)
        return {
            "as_of": t.as_of.strftime("%Y-%m-%d"),
            "price_date": t.price_date.strftime("%Y-%m-%d"),
            "version": str(t.version),
            "rows": rows,
        }
    except Exception as exc:  # noqa: BLE001 — never take the page down
        return {"error": f"{type(exc).__name__}: {exc}"}


@st.cache_resource(
    ttl=_CACHE_TTL_SECS,
    show_spinner="Reading Andrew's custom allocations from the client system "
                 "(read-only, cached 10 min)…")
def _load_custom_state() -> dict:
    """Everything the hand-written family shows, in ONE read. Returns a plain dict; on any
    problem it returns ``{"error": ...}`` rather than raising, so the page degrades to a
    plain-English notice instead of a traceback.

    Shape::

        {"built_at_str": str, "labels": [str, ...], "assigned_total": int,
         "models": {label: {"published": bool, "rows": [...], "target": Target|None,
                            "meta": AllocationMeta|None, "error": str|None,
                            "accounts": [roster rows], "scan": {...}|None,
                            "scan_error": str|None}}}

    READ-ONLY: two SELECTs against the client system's views under the read-only role,
    local price history, and the pure rebalance engine. No broker, no process spawn, no
    write. (The Targets carry pandas objects and are not cache_data-serialisable — the same
    reason the sibling Control Plane loader is a cache_resource.)"""
    import crm_roster
    import custom_target

    if not crm_roster.is_configured():
        return {"error": "not_configured"}

    try:
        published = crm_roster.custom_allocation_labels()
        # Every label we know about: the six the client system defines (so an unpublished
        # one can be SAID to be unpublished) plus anything else it is publishing today.
        labels = list(CUSTOM_MODEL_LABELS) + sorted(published - set(CUSTOM_MODEL_LABELS))
        rows = crm_roster.fetch_custom_allocations(labels) if labels else []
        roster_rows = crm_roster.fetch_roster(advisor_name=None)   # whole book, read-only
    except crm_roster.CrmRosterUnavailable as exc:
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001 — the page must never show a traceback
        return {"error": f"{type(exc).__name__}: {exc}"}

    by_label: dict[str, list[dict]] = {}
    for r in rows:
        by_label.setdefault(str(r["strategy_name"]), []).append(dict(r))

    # Which roster accounts are assigned to each custom model.
    assigned: dict[str, list[dict]] = {}
    for r in roster_rows:
        model = (r.get("model") or "")
        if model in by_label or model in labels:
            assigned.setdefault(model, []).append(dict(r))

    # Latest holdings for the assigned accounts only (nothing else is needed here). A
    # failure here is NON-FATAL and deliberately so: the published allocations still
    # display in full, and only the drift read says plainly why it is blank.
    holdings: dict[str, list[dict]] = {}
    holdings_error: str | None = None
    assigned_ids = [str(a.get("account_id")) for group in assigned.values() for a in group]
    if assigned_ids:
        try:
            holdings = crm_roster.fetch_holdings_latest(assigned_ids)
        except crm_roster.CrmRosterUnavailable as exc:
            holdings_error = str(exc)
        except Exception as exc:  # noqa: BLE001
            holdings_error = f"{type(exc).__name__}: {exc}"

    # S0's ticker universe, unioned into each custom model's universe so an account
    # migrating OFF an S0 sleeve can be shown rotating out of it (custom_target trap 4 —
    # a held symbol outside the universe is ALIEN and can never produce a leg).
    try:
        from strategies import config as scfg
        base_universe = set(scfg.ALL_TICKERS)
    except Exception:  # noqa: BLE001
        base_universe = set()

    models: dict[str, dict] = {}
    for label in labels:
        model_rows = by_label.get(label, [])
        entry: dict = {
            "published": bool(model_rows),
            "rows": model_rows,
            "target": None,
            "meta": None,
            "error": None,
            "accounts": assigned.get(label, []),
            "scan": None,
            "scan_error": holdings_error,
            "small": _is_small_model(label),
        }
        if model_rows:
            # THE PRICEABILITY CHECK (validation C-1). build_target loads real price history
            # for exactly these tickers and raises CustomAllocationError naming any it cannot
            # price. Captured, never swallowed — the page shows the message verbatim.
            try:
                target, meta = custom_target.build_target(model_rows, label)
                entry["target"] = target
                entry["meta"] = meta
            except custom_target.CustomAllocationError as exc:
                entry["error"] = str(exc)
            except Exception as exc:  # noqa: BLE001
                entry["error"] = f"{type(exc).__name__}: {exc}"

        # DRIFT + WOULD-TRADE PREVIEW for the assigned accounts (pure engine, no broker).
        if entry["target"] is not None and entry["accounts"] and holdings_error is None:
            try:
                universe = custom_target.universe_for(entry["target"], base=base_universe)
                entry["scan"] = scan_accounts_for_model(
                    entry["target"], entry["accounts"], holdings, universe=universe)
            except Exception as exc:  # noqa: BLE001
                entry["scan_error"] = f"{type(exc).__name__}: {exc}"
        models[label] = entry

    return {
        "built_at_str": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "labels": labels,
        "models": models,
        "assigned_total": sum(len(v["accounts"]) for v in models.values()),
        "n_roster": len(roster_rows),
    }


def _custom_state() -> dict:
    """``_load_custom_state`` behind a guard, so that NOTHING the client-system read can do
    takes this page down.

    The loader already turns every problem it anticipates into ``{"error": ...}``, but it
    reaches a database, a price store and an engine, and an unanticipated failure in any of
    them would otherwise abort the whole render — taking the models the strategy code works
    out down with it, even though they have nothing to do with the client system. That is
    exactly the coupling this page must not have, so the call is wrapped here and the
    failure comes back as one plain-English sentence instead."""
    try:
        return _load_custom_state()
    except Exception as exc:  # noqa: BLE001 — never take the page down
        return {"error": f"{type(exc).__name__}: {exc}"}


# =========================================================================== #
# Shared renderers.                                                            #
# =========================================================================== #
def _section_with_badge(label: str, badge_html: str) -> str:
    """A section heading reading "<model name> — <coloured status badge>".

    ``theme.section()`` escapes its ENTIRE argument, so handing it a ``<span>`` puts the raw
    tag on screen as literal text. The heading is assembled here instead, using the same
    ``dk-section`` class so it looks identical to every other heading: the model name is
    escaped (it is data), the badge is kept as markup (it is this module's own fixed string,
    never user input)."""
    return f'<div class="dk-section">{theme._esc(label)} {badge_html}</div>'


def _render_readonly_banner() -> None:
    """The page's standing statement of what it is and what it structurally cannot do —
    covering BOTH families: the frozen anti-curve-fit strategy code behind the computed
    models, and the one-way read-only seam to the client system behind the hand-written
    ones."""
    st.markdown(
        theme.status_card(
            "This page only displays these models — it cannot change any of them "
            "and it cannot send anything",
            "warn",
            "Read-only — no editing, no arming, nothing transmitted",
            "Nothing below is written down in this page. The models the strategy code "
            "works out are pulled LIVE from the frozen, anti-curve-fit strategy code — "
            "their holdings come from the exact validated engine the paperbot executes — "
            "and the models Andrew writes himself are read LIVE from the client system, "
            "read-only and one way. So this view can never drift from the real book, "
            "either way. Changing a model the strategy code works out is NOT an in-app "
            "edit: it goes through the gated review → validate → deploy flow "
            "(out-of-sample and per-regime checks, maker-checker approval) — a later "
            "stage. Changing a hand-written model means Andrew publishing a new version in "
            "the client system, which is also not done here. This page starts no program, "
            "opens no broker connection, holds no arming control and has no confirmation "
            "box — there is no path from this screen to a live order, and nothing here "
            "writes back to the client system.",
        ),
        unsafe_allow_html=True,
    )


def _render_holdings_table(rows: list[tuple], *, proposed: bool = False,
                           show_sleeve: bool = True) -> None:
    """Render a ticker -> sleeve -> weight table from `rows` = [(ticker, weight, sleeve?)].
    For live models sleeve is looked up from config; for proposed models it is passed in.

    `show_sleeve=False` drops the sleeve column entirely and expects 2-tuples. It exists for
    books that HAVE no sleeve concept — a hand-authored ("custom") allocation, where Andrew
    picks the tickers directly. `_sleeve_of` maps a ticker to one of Strategy 0's four
    sleeves and returns a dash for everything else, so such a book would otherwise render an
    always-blank column; omitting the column is honest, a column of dashes is not."""
    sleeve_th = (
        f'<th style="text-align:left;padding:.4rem .6rem;color:{theme.MUTED};'
        f'font-weight:600;border-bottom:1px solid {theme.BORDER}">Sleeve</th>'
    ) if show_sleeve else ""
    header = (
        '<tr>'
        f'<th style="text-align:left;padding:.4rem .6rem;color:{theme.MUTED};'
        f'font-weight:600;border-bottom:1px solid {theme.BORDER}">Ticker</th>'
        f'{sleeve_th}'
        f'<th style="text-align:right;padding:.4rem .6rem;color:{theme.MUTED};'
        f'font-weight:600;border-bottom:1px solid {theme.BORDER}">Target weight</th></tr>'
    )
    body = []
    total = 0.0
    for entry in rows:
        sleeve = ""
        if not show_sleeve:
            tkr, wt = entry
        elif proposed:
            tkr, wt, sleeve = entry
        else:
            tkr, wt = entry
            sleeve = _sleeve_of(tkr)
        total += wt
        sleeve_td = (
            f'<td style="padding:.45rem .6rem;border-bottom:1px solid {theme.BORDER};'
            f'color:{theme.MUTED};font-size:12.5px">{theme._esc(sleeve)}</td>'
        ) if show_sleeve else ""
        body.append(
            '<tr>'
            f'<td style="padding:.45rem .6rem;border-bottom:1px solid {theme.BORDER};'
            f'color:{theme.TEXT};font-family:monospace;font-weight:650">{theme._esc(tkr)}</td>'
            f'{sleeve_td}'
            f'<td style="padding:.45rem .6rem;border-bottom:1px solid {theme.BORDER};'
            f'color:{theme.TEXT};text-align:right;font-weight:650">{wt * 100:.3f}%</td></tr>'
        )
    body.append(
        '<tr>'
        f'<td style="padding:.45rem .6rem;color:{theme.MUTED}">Total</td>'
        + (f'<td style="padding:.45rem .6rem"></td>' if show_sleeve else "")
        + f'<td style="padding:.45rem .6rem;color:{theme.MUTED};text-align:right">'
        f'{total * 100:.1f}%</td></tr>'
    )
    st.markdown(
        f'<table style="border-collapse:collapse;width:100%;font-size:13.5px">'
        + header + "".join(body) + '</table>',
        unsafe_allow_html=True,
    )


# =========================================================================== #
# THE LIST: every model the desk knows about, on one screen.                   #
# =========================================================================== #
def _computed_overview_rows() -> list[dict]:
    """One plain-English summary row per model the strategy code works out."""
    out = []
    for model in MODELS:
        label = model["label"]
        status = model["status"]
        if status == "proposed":
            out.append({
                "model": label,
                "family": "The strategy code, automatically",
                "status": "Approved but not yet defined in the strategy code",
                "holdings": str(len(model["proposed_rows"])) + " (intended)",
                "accounts": "—",
                "attention": "Shown as a proposal only — no engine has been run for it",
                "tier": "warn",
            })
            continue
        resolved = _resolve_holdings(model["version"])
        if "error" in resolved:
            out.append({
                "model": label,
                "family": "The strategy code, automatically",
                "status": ("Running live on the paper account" if status == "live"
                           else "Resolves to a distinct book"),
                "holdings": "—",
                "accounts": "—",
                "attention": "Its holdings could not be worked out right now",
                "tier": "bad",
            })
            continue
        held = [r for r in resolved["rows"] if r[1] > 1e-9]
        out.append({
            "model": label,
            "family": "The strategy code, automatically",
            "status": ("Running live on the paper account" if status == "live"
                       else "Resolves to a distinct book"),
            "holdings": str(len(held)),
            "accounts": "—",
            "attention": "Nothing outstanding",
            "tier": "good",
        })
    return out


def _custom_overview_rows(custom: dict) -> list[dict]:
    """One plain-English summary row per model Andrew writes himself."""
    out = []
    models = custom.get("models", {})
    for label in custom.get("labels", []):
        entry = models.get(label, {"published": False})
        accounts = entry.get("accounts") or []
        if not entry.get("published"):
            out.append({
                "model": label,
                "family": "Andrew, by hand",
                "status": "No holdings published yet",
                "holdings": "—",
                "accounts": str(len(accounts)),
                "attention": (f"{len(accounts)} account(s) are assigned to it with no book "
                              f"to be brought in line with"
                              if accounts else "Nothing outstanding"),
                "tier": "bad" if accounts else "unknown",
            })
            continue

        rows, total_pct = _pct_rows(entry["rows"])
        meta = entry.get("meta")
        vnum = getattr(meta, "version_number", None)
        if vnum is None and entry["rows"]:
            vnum = entry["rows"][0].get("version_number")
        status = "Published" if vnum is None else f"Published version {vnum}"

        notes: list[str] = []
        tier = "good"
        if entry.get("error"):
            notes.append("A ticker in it has no price history the desk can size from")
            tier = "bad"
        if abs(total_pct - 100.0) >= 1e-6:
            notes.append(f"The percentages add up to {total_pct:.2f}%, not 100%")
            tier = "bad"
        scan = entry.get("scan") or {}
        n_oos = scan.get("n_out_of_spec", 0)
        if entry.get("scan_error"):
            notes.append("The drift of its accounts could not be worked out right now")
            tier = "warn" if tier == "good" else tier
        elif n_oos:
            notes.append(f"{n_oos} account(s) out of line with the allocation")
            tier = "warn" if tier == "good" else tier
        out.append({
            "model": label,
            "family": "Andrew, by hand",
            "status": status,
            "holdings": str(len(rows)),
            "accounts": str(len(accounts)),
            "attention": "; ".join(notes) if notes else "Nothing outstanding",
            "tier": tier,
        })
    return out


def _render_overview(custom: dict) -> None:
    """THE LIST — every model the desk knows about, both families, one row each, before any
    of the detail. This is what makes one page out of three readable: the whole picture
    first, then everything else folded away behind a section per model."""
    st.markdown(theme.section("Every model the desk knows about"), unsafe_allow_html=True)

    rows = _computed_overview_rows()
    if custom.get("error"):
        st.caption("The models Andrew writes himself could not be read from the client "
                   "system for this list, so only the models the strategy code works out "
                   "are listed here. The reason is given in that section further down.")
    else:
        rows += _custom_overview_rows(custom)

    heads = ("Model", "Who chose the holdings", "Status", "Number of holdings",
             "Accounts assigned", "Anything to look at")
    header = "<tr>" + "".join(
        f'<th style="text-align:left;padding:.4rem .6rem;color:{theme.MUTED};'
        f'font-weight:600;border-bottom:1px solid {theme.BORDER}">{theme._esc(h)}</th>'
        for h in heads) + "</tr>"

    body = []
    for r in rows:
        colour = theme.TIER.get(r["tier"], {}).get("c", theme.MUTED)
        cells = [
            f'<td style="padding:.45rem .6rem;border-bottom:1px solid {theme.BORDER};'
            f'color:{theme.TEXT};font-weight:650">{theme._esc(r["model"])}</td>',
            f'<td style="padding:.45rem .6rem;border-bottom:1px solid {theme.BORDER};'
            f'color:{theme.MUTED};font-size:12.5px">{theme._esc(r["family"])}</td>',
            f'<td style="padding:.45rem .6rem;border-bottom:1px solid {theme.BORDER};'
            f'color:{theme.TEXT}">{theme._esc(r["status"])}</td>',
            f'<td style="padding:.45rem .6rem;border-bottom:1px solid {theme.BORDER};'
            f'color:{theme.TEXT}">{theme._esc(r["holdings"])}</td>',
            f'<td style="padding:.45rem .6rem;border-bottom:1px solid {theme.BORDER};'
            f'color:{theme.TEXT}">{theme._esc(r["accounts"])}</td>',
            f'<td style="padding:.45rem .6rem;border-bottom:1px solid {theme.BORDER};'
            f'color:{colour};font-size:12.5px">{theme._esc(r["attention"])}</td>',
        ]
        body.append("<tr>" + "".join(cells) + "</tr>")

    st.markdown(
        '<table style="border-collapse:collapse;width:100%;font-size:13.5px">'
        + header + "".join(body) + "</table>",
        unsafe_allow_html=True,
    )
    st.caption("Every model on the desk, in one list. \"Who chose the holdings\" is the "
               "only real difference between the two families: the strategy code works out "
               "the first group from how the market is behaving, and Andrew picks the "
               "second group by hand in the client system. Open a model further down the "
               "page to see exactly what it holds and why.")


# =========================================================================== #
# The live market regime read (shared by every model the strategy code works   #
# out — it does not apply to the hand-written ones).                           #
# =========================================================================== #
def _render_live_state(state: dict) -> None:
    """Today's regime read in full: the headline score and confirmed regime up front, and
    everything behind it — the three components, raw versus confirmed, the live version's
    band, the next scheduled rebalance and which data source each stress input came from —
    folded into one collapsed section."""
    st.markdown(theme.section("Where the market is right now (live regime read)"),
                unsafe_allow_html=True)
    if "error" in state:
        st.warning("Today's regime could not be computed right now "
                   f"({state['error']}). The frozen model definitions below are "
                   "unaffected, and so are the models Andrew writes himself.")
        return

    score = state["score"]
    raw = state["raw_regime"]
    confirmed = state["confirmed_regime"]
    raw_plain = REGIME_PLAIN.get(raw, raw)
    conf_plain = REGIME_PLAIN.get(confirmed, confirmed)
    tier = "good" if confirmed in ("RiskOn", "RiskOnNarrowing") else (
        "warn" if confirmed == "Caution" else "bad")

    # --- Headline: score + the two regimes (raw vs confirmed) ---
    if raw == confirmed:
        head_sub = (f"Today's score maps straight to {conf_plain}, and the confirmed "
                    f"(traded) regime is the same — hysteresis is not holding back any "
                    f"pending move.")
    else:
        head_sub = (f"Today's score reads {raw_plain}, but the confirmed (traded) "
                    f"regime is still {conf_plain} — the whipsaw buffer has not yet "
                    f"confirmed the move.")
    st.markdown(
        theme.status_card(
            f"Market Health Score (as of {state['as_of']})",
            tier,
            f"{score:.1f} out of 100 — {conf_plain}",
            head_sub
            + " This confirmed regime governs the equity band for every model the strategy "
              "code works out; each of those models scales this band by its own version "
              "allowance. It does not apply to the models Andrew writes himself — those "
              "hold exactly what he published, whatever the market is doing.",
        ),
        unsafe_allow_html=True,
    )

    with st.expander("Show how today's score was worked out, and what it allows"):
        # --- The three components that build the score ---
        comp_rows = []
        for key in ("trend", "breadth", "stress"):
            val = state[key]
            pts = val * (100.0 / 3.0)  # each component is up to a third of the 0-100 score
            comp_rows.append(
                theme.row(
                    COMPONENT_GLOSS[key],
                    f'<span style="color:{theme.TEXT};font-weight:650">{val:.2f} of 1.00'
                    f'</span>&nbsp;<span style="color:{theme.MUTED}">'
                    f'(contributes {pts:.1f} of {100.0/3.0:.1f} points)</span>',
                    key.capitalize(),
                )
            )
        st.markdown(
            theme.card("The three equal-weight components behind the score",
                       "".join(comp_rows)),
            unsafe_allow_html=True,
        )

        # --- Raw vs confirmed + live-version band + next rebalance ---
        reb = _next_rebalance_dates()
        detail_rows = [
            theme.row("Raw regime (today's score, before smoothing)",
                      f'<span style="color:{theme.TEXT};font-weight:650">'
                      f'{theme._esc(raw_plain)}</span>'),
            theme.row("Confirmed regime (governs the book, after hysteresis)",
                      f'<span style="color:{theme.TEXT};font-weight:650">'
                      f'{theme._esc(conf_plain)}</span>'),
            theme.row(f"Equity-allowance band for the live version ({LIVE_VERSION})",
                      f'<span style="color:{theme.TEXT};font-weight:650">'
                      f'{_pct(state["growth_lo"])}–{_pct(state["growth_hi"])} of the '
                      f'portfolio</span>',
                      f"regime band {_pct(state['band_lo'])}–{_pct(state['band_hi'])} "
                      f"× Growth allowance {state['growth_allow']:.0%}"),
            theme.row("Next scheduled monthly rebalance",
                      f'<span style="color:{theme.TEXT}">'
                      f'{theme._esc(reb[1] if reb else "unavailable")}</span>',
                      f"signal {reb[0]} (the last trading day of the month), executed the "
                      f"next trading day" if reb else ""),
        ]
        st.markdown(theme.card("Raw versus confirmed regime, and what it allows",
                               "".join(detail_rows)),
                    unsafe_allow_html=True)
        st.caption("Data sources for the stress component today: volatility from "
                   f"{state['vix_src']}, credit spreads from {state['hy_oas_src']}. "
                   "Computed from end-of-day data through the same engine the live "
                   "strategy uses — read-only, nothing is transmitted.")


# =========================================================================== #
# FAMILY 1 — the models the strategy code works out by itself.                 #
# =========================================================================== #
def _render_model_card(model: dict, state: dict) -> None:
    """One computed model's detail: resolved (or proposed) holdings and a facts card
    (version allowance, today's version-scaled equity band, universe, version line)."""
    from strategies import config as scfg

    label = model["label"]
    version = model["version"]
    status = model["status"]
    proposed = status == "proposed"

    st.caption(model["note"])

    # --- Resolved (or proposed) holdings — the headline ---
    if proposed:
        st.markdown(
            theme.status_card(
                "Intended holdings (PROPOSED — not resolved from code)",
                "warn",
                "SCHB 85% + USFR 15% · whole-share proxy of S0 Growth",
                "S0 Growth (Small) is APPROVED but NOT YET DEFINED in strategies/, so these "
                "weights are the intended design shape — NOT a live engine resolution. "
                "In production it would track S0 Growth's dynamic equity/defensive split "
                "(the 85/15 moves with the regime band, 0–100%). It is whole-share "
                "feasible down to $500. Auto-tier: an account assigned S0 Growth with NAV "
                "below $25,000 resolves to S0 Growth (Small); it auto-promotes to full "
                "S0 Growth past $25k (hysteresis: promote ≥ $27,500, demote < $22,500).",
            ),
            unsafe_allow_html=True,
        )
        _render_holdings_table(model["proposed_rows"], proposed=True)
        st.caption("Display-only intended weights — no engine was run for this card. "
                   "S0 Growth (Small) is not present in strategies/; defining it in code "
                   "is a later stage.")
    else:
        h = _resolve_holdings(version)
        if "error" in h:
            st.warning(f"Could not resolve {label}'s live holdings right now "
                       f"({h['error']}). Its model definition is unaffected.")
        else:
            _render_holdings_table(h["rows"])
            st.caption(f"Resolved LIVE by running the validated engine through today: "
                       f"rebalance as-of {h['as_of']}, prices as-of {h['price_date']} "
                       f"(version \"{h['version']}\"). This is the exact book the paperbot "
                       f"would place — same code path, so it cannot drift from execution.")

    # --- Facts card: allowance, today's version-scaled band, universe, version line ---
    allow = scfg.CLIENT_VERSIONS[version]["equity_allowance"]
    floor = scfg.CLIENT_VERSIONS[version]["tbill_floor"]

    # Today's equity band for THIS version = raw regime band × this version's allowance.
    if "error" in state:
        band_txt = "unavailable right now (live regime read failed)"
    else:
        b_lo, b_hi = state["band_lo"], state["band_hi"]
        s_lo, s_hi = b_lo * allow, b_hi * allow
        conf = REGIME_PLAIN.get(state["confirmed_regime"], state["confirmed_regime"])
        band_txt = (f"{s_lo * 100:.0f}%–{s_hi * 100:.0f}% of the portfolio "
                    f"(confirmed regime {conf}: band {b_lo * 100:.0f}%–{b_hi * 100:.0f}% "
                    f"× {label} allowance {allow:.0%})")

    if proposed:
        uni = model["proposed_universe"]
        uni_txt = (f"{len(uni)} funds — "
                   + ", ".join(uni)
                   + " (equity core collapsed to SCHB; USFR defensive)")
    else:
        n = len(scfg.ALL_TICKERS)
        uni_txt = (f"{n} funds across 4 sleeves — equity core, {len(scfg.SECTORS)} "
                   f"sectors, defensive (cash/Treasuries) and real assets. Full list in "
                   f"the shared engine section below.")

    rows_html = [
        theme.row("Version equity allowance",
                  f'<span style="color:{theme.TEXT};font-weight:650">'
                  f'{allow * 100:.0f}% of the regime band</span>'),
        theme.row("Minimum cash (T-bill) floor",
                  f'<span style="color:{theme.TEXT};font-weight:650">'
                  f'{floor * 100:.0f}% of the portfolio</span>'),
        theme.row("Today's equity band for this model",
                  f'<span style="color:{theme.TEXT};font-weight:650">'
                  f'{theme._esc(band_txt)}</span>'),
        theme.row("Tradeable universe",
                  f'<span style="color:{theme.TEXT}">{theme._esc(uni_txt)}</span>'),
        theme.row("Model version / last changed",
                  f'<span style="color:{theme.MUTED}">'
                  f'git-tracked in strategies/ (authoritative history)</span>',
                  "changelog + version bump land via the gated review→validate→deploy "
                  "flow — a later stage"),
    ]
    st.markdown(theme.card(f"{label} — allowances, today's band, universe & version",
                           "".join(rows_html)),
                unsafe_allow_html=True)


def _computed_badge(status: str) -> str:
    if status == "live":
        return (f'<span style="color:{theme.TIER["good"]["c"]};font-weight:700">'
                f'— LIVE (Strategy 0)</span>')
    if status == "proposed":
        return (f'<span style="color:{theme.TIER["warn"]["c"]};font-weight:700">'
                f'— PROPOSED · not yet deployed</span>')
    return (f'<span style="color:{theme.MUTED};font-weight:600">'
            f'— resolves to a distinct book</span>')


def _render_computed_family(state: dict) -> None:
    """Every model the strategy code works out: one collapsed section per model, then the
    engine detail they all share."""
    st.markdown(theme.section("Models the strategy code works out automatically"),
                unsafe_allow_html=True)
    st.caption("These four are computed by the desk's own validated strategy code — nobody "
               "picks their holdings by hand; the code decides what to hold from how the "
               "market is behaving. Their names all start with \"S0\", short for Strategy 0, "
               "the strategy that runs them, so that the automatic Growth is never confused "
               "with the Growth that Andrew writes himself further down this page. Open a "
               "model to see its exact holdings, its allowance and today's band for it.")

    for model in MODELS:
        st.markdown(_section_with_badge(model["label"], _computed_badge(model["status"])),
                    unsafe_allow_html=True)
        with st.expander(f"Show what {model['label']} holds, and the rules behind it"):
            _render_model_card(model, state)

    _render_shared_engine()


def _render_shared_engine() -> None:
    """The engine detail shared by every computed model (identical frozen config): the
    regime ladder, the re-entry ladder + whipsaw controls, the full version-allowances
    table, and the full ticker universe. This is Strategy 0's rulebook, in one place."""
    st.markdown(theme.section("The shared engine behind the four automatic models"),
                unsafe_allow_html=True)
    st.caption("S0 Growth, S0 Balanced and S0 Conservative are one validated brain — they "
               "share the SAME regime ladder, re-entry ladder, whipsaw controls and ticker "
               "universe; they differ only in the version allowance / cash floor shown on "
               "each model above (which is what makes their resolved books distinct). "
               "S0 Growth (Small) shares this engine too, over a two-ticker universe. None "
               "of this applies to the models Andrew writes himself, further down the page "
               "— those have no engine behind them at all, because Andrew picks their "
               "holdings directly.")
    with st.expander("Show Strategy 0's full rulebook — the regime ladder, the re-entry "
                     "ladder, the whipsaw controls, the version allowances and the traded "
                     "ticker universe"):
        _render_regime_ladder()
        _render_reentry_and_whipsaw()
        _render_version_allowances()
        _render_universe()


def _render_regime_ladder() -> None:
    from strategies import config as scfg

    st.markdown(theme.section("The regime ladder — score range to equity allowance"),
                unsafe_allow_html=True)
    st.caption("The Market Health Score (0-100) maps to one of five regimes; each "
               "regime sets how much of the portfolio may be in stocks (before the "
               "client-version allowance below scales it). Most aggressive at the top.")

    bands = scfg.REGIME_BANDS
    order = sorted(bands, key=lambda r: bands[r]["score"][0], reverse=True)
    rows_html = [
        '<tr>'
        f'<th style="text-align:left;padding:.4rem .6rem;color:{theme.MUTED};'
        f'font-weight:600;border-bottom:1px solid {theme.BORDER}">Regime</th>'
        f'<th style="text-align:left;padding:.4rem .6rem;color:{theme.MUTED};'
        f'font-weight:600;border-bottom:1px solid {theme.BORDER}">Health-score range</th>'
        f'<th style="text-align:left;padding:.4rem .6rem;color:{theme.MUTED};'
        f'font-weight:600;border-bottom:1px solid {theme.BORDER}">Stock (equity) '
        f'allowance band</th></tr>'
    ]
    for name in order:
        s_lo, s_hi = bands[name]["score"]
        e_lo, e_hi = bands[name]["equity"]
        color = REGIME_COLOR.get(name, theme.MUTED)
        plain = REGIME_PLAIN.get(name, name)
        rows_html.append(
            '<tr>'
            f'<td style="padding:.45rem .6rem;border-bottom:1px solid {theme.BORDER}">'
            f'<span style="display:inline-block;width:10px;height:10px;border-radius:50%;'
            f'background:{color};margin-right:.5rem"></span>'
            f'<span style="color:{theme.TEXT};font-weight:600">{theme._esc(plain)}</span></td>'
            f'<td style="padding:.45rem .6rem;border-bottom:1px solid {theme.BORDER};'
            f'color:{theme.TEXT}">{s_lo:.0f}–{s_hi:.0f}</td>'
            f'<td style="padding:.45rem .6rem;border-bottom:1px solid {theme.BORDER};'
            f'color:{theme.TEXT}">{e_lo * 100:.0f}%–{e_hi * 100:.0f}% of the '
            f'portfolio</td></tr>'
        )
    st.markdown(
        f'<table style="border-collapse:collapse;width:100%;font-size:13.5px">'
        + "".join(rows_html) + '</table>',
        unsafe_allow_html=True,
    )


def _render_reentry_and_whipsaw() -> None:
    from strategies import config as scfg

    st.markdown(theme.section("The re-entry ladder — how stocks are rebuilt after a "
                              "de-risk"),
                unsafe_allow_html=True)
    st.caption("After the model has de-risked, it rebuilds stock exposure in stages "
               "rather than all at once, so a single good day cannot whipsaw the book "
               "back to fully invested.")

    stages = scfg.REENTRY_STAGES
    stage4_by_ver = scfg.REENTRY_STAGE4_SCORE
    sector_ct = scfg.REENTRY_STAGE3_SECTOR_COUNT
    total_sectors = len(scfg.SECTORS)

    stage_rows = []
    for stage in sorted(stages):
        eq = stages[stage]["equity_pct"]
        gate = REENTRY_GATE_TEXT.get(stage, "")
        if stage == 3:
            gate = gate.replace("the required number", f"{sector_ct} of {total_sectors}")
        if stage == 4:
            gates_by_ver = ", ".join(
                f"{v} {stage4_by_ver[v]:.0f}" for v in VERSION_ORDER if v in stage4_by_ver)
            gate = gate + f" The stage-4 score gate is by version: {gates_by_ver}."
        stage_rows.append(
            theme.row(
                f"Stage {stage} — rebuild to {eq * 100:.0f}% of the allowed stock band",
                f'<span style="color:{theme.MUTED};font-size:12px">{theme._esc(gate)}</span>',
            )
        )
    st.markdown(theme.card("Re-entry stages (share of the allowed stock band)",
                           "".join(stage_rows)),
                unsafe_allow_html=True)

    # --- Whipsaw controls (hysteresis) ---
    st.markdown(theme.section("Whipsaw controls (hysteresis) — resisting false moves"),
                unsafe_allow_html=True)
    conf_days = scfg.REGIME_CONFIRMATION_DAYS
    cross_pts = scfg.REGIME_MIN_THRESHOLD_CROSS
    drop_pts = scfg.REGIME_IMMEDIATE_DROP_POINTS
    ctrl_rows = [
        theme.row("Confirmation days before a regime change is accepted",
                  f'<span style="color:{theme.TEXT};font-weight:650">{conf_days} '
                  f'observations</span>',
                  "a new regime must persist this many readings first"),
        theme.row("Minimum threshold crossing (dead-zone)",
                  f'<span style="color:{theme.TEXT};font-weight:650">{cross_pts:.0f} '
                  f'points</span>',
                  "score moves smaller than this across a boundary are ignored"),
        theme.row("Immediate de-risk drop",
                  f'<span style="color:{theme.TEXT};font-weight:650">more than '
                  f'{drop_pts:.0f} points</span>',
                  "a fall bigger than this de-risks at once, without waiting"),
    ]
    st.markdown(theme.card("The three whipsaw knobs", "".join(ctrl_rows)),
                unsafe_allow_html=True)
    st.caption("Re-risking (moving to a healthier regime) always serves the "
               "confirmation buffer; only a large drop is allowed to act immediately.")


def _render_version_allowances() -> None:
    from strategies import config as scfg

    st.markdown(theme.section("Client-version allowances — Growth is the live version"),
                unsafe_allow_html=True)
    st.caption("The regime band above is scaled by the client version's equity "
               "allowance, and each version keeps a minimum cash (T-bill) floor. "
               "Strategy 0 runs the Growth version live.")

    versions = scfg.CLIENT_VERSIONS
    header = (
        '<tr>'
        f'<th style="text-align:left;padding:.4rem .6rem;color:{theme.MUTED};'
        f'font-weight:600;border-bottom:1px solid {theme.BORDER}">Version</th>'
        f'<th style="text-align:left;padding:.4rem .6rem;color:{theme.MUTED};'
        f'font-weight:600;border-bottom:1px solid {theme.BORDER}">Stock (equity) '
        f'allowance</th>'
        f'<th style="text-align:left;padding:.4rem .6rem;color:{theme.MUTED};'
        f'font-weight:600;border-bottom:1px solid {theme.BORDER}">Minimum cash '
        f'(T-bill) floor</th></tr>'
    )
    body = []
    for v in VERSION_ORDER:
        if v not in versions:
            continue
        allow = versions[v]["equity_allowance"]
        floor = versions[v]["tbill_floor"]
        is_live = (v == LIVE_VERSION)
        name = (f'{theme._esc(v)} <span style="color:{theme.TIER["good"]["c"]};'
                f'font-weight:700">— LIVE</span>' if is_live else theme._esc(v))
        weight = "700" if is_live else "550"
        body.append(
            '<tr>'
            f'<td style="padding:.45rem .6rem;border-bottom:1px solid {theme.BORDER};'
            f'color:{theme.TEXT};font-weight:{weight}">{name}</td>'
            f'<td style="padding:.45rem .6rem;border-bottom:1px solid {theme.BORDER};'
            f'color:{theme.TEXT}">{allow * 100:.0f}% of the regime band</td>'
            f'<td style="padding:.45rem .6rem;border-bottom:1px solid {theme.BORDER};'
            f'color:{theme.TEXT}">{floor * 100:.0f}% of the portfolio</td></tr>'
        )
    st.markdown(
        f'<table style="border-collapse:collapse;width:100%;font-size:13.5px">'
        + header + "".join(body) + '</table>',
        unsafe_allow_html=True,
    )


def _render_universe() -> None:
    from strategies import config as scfg

    st.markdown(theme.section("The traded ticker universe, by sleeve"),
                unsafe_allow_html=True)
    st.caption("The exchange-traded funds Strategy 0 chooses among, grouped by role. "
               "These are the only instruments the strategy can hold.")

    sleeves = [
        ("Core stocks (equity core)", scfg.EQUITY_CORE,
         "Broad US stock-market funds — the base equity exposure."),
        ("Sector funds", scfg.SECTORS,
         "The 11 S&P 500 sector funds, used for breadth and any sector tilt."),
        ("Defensive assets (cash and bonds)", scfg.DEFENSIVE_ASSETS,
         "T-bills, short/intermediate/long Treasuries, and floating-rate funds — "
         "the safety sleeve."),
        ("Real assets (inflation hedges)", scfg.REAL_ASSETS,
         "Gold, Treasury inflation-protected securities, and broad commodities."),
    ]
    for title, tickers, gloss in sleeves:
        chips = "".join(
            f'<span style="display:inline-block;background:{theme.SURFACE_2};'
            f'border:1px solid {theme.BORDER};border-radius:6px;padding:.15rem .5rem;'
            f'margin:.15rem .3rem .15rem 0;font-size:12.5px;color:{theme.TEXT};'
            f'font-family:monospace">{theme._esc(t)}</span>'
            for t in tickers
        )
        value = (f'<div style="margin:.15rem 0 .35rem 0">{chips}</div>'
                 f'<div style="font-size:12px;color:{theme.MUTED}">{theme._esc(gloss)}'
                 f' ({len(tickers)} funds)</div>')
        st.markdown(theme.card(title, value), unsafe_allow_html=True)


# =========================================================================== #
# FAMILY 2 — the models Andrew writes himself, read live from the client       #
# system: the published book, the two desk-only validations, the accounts on   #
# it, their drift, and the trades that would correct it.                       #
# =========================================================================== #
def _assigned_value(accounts) -> float:
    """Total recorded value of the accounts assigned to a model. Display only."""
    total = 0.0
    for a in accounts or []:
        try:
            total += float(a.get("total_value") or 0.0)
        except (TypeError, ValueError):
            continue
    return total


def _render_allocation(label: str, entry: dict) -> None:
    """The published book itself: tickers, percentages, the total, and which published
    version it is (number, effective date, when it was published)."""
    rows, total_pct = _pct_rows(entry["rows"])

    # The sleeve column is OMITTED for custom models: _sleeve_of maps a ticker to one of
    # Strategy 0's four sleeves and returns a dash for anything else, so every hand-authored
    # ticker would render a blank column. A hand-authored book has no sleeve concept at all
    # — Andrew picks the tickers directly — so showing an always-empty column would be worse
    # than showing no column.
    _render_holdings_table(rows, show_sleeve=False)

    meta = entry.get("meta")
    version_txt = "—"
    effective_txt = "—"
    published_txt = "—"
    if meta is not None:
        version_txt = ("—" if meta.version_number is None
                       else f"version {meta.version_number}")
        effective_txt = _fmt_date(meta.effective_from)
        published_txt = _fmt_datetime(meta.published_at)
    elif entry["rows"]:
        first = entry["rows"][0]
        vnum = first.get("version_number")
        version_txt = "—" if vnum is None else f"version {vnum}"
        effective_txt = _fmt_date(first.get("effective_from"))
        published_txt = _fmt_datetime(first.get("published_at"))

    total_tier = "good" if abs(total_pct - 100.0) < 1e-6 else "bad"
    total_phrase = (f"{total_pct:.2f}% — fully invested"
                    if total_tier == "good"
                    else f"{total_pct:.2f}% — does NOT add up to 100%")
    st.markdown(
        theme.card(
            f"{label} — which published version this is",
            "".join([
                theme.row("Published version",
                          f'<span style="color:{theme.TEXT};font-weight:650">'
                          f'{theme._esc(version_txt)}</span>',
                          "a published version is frozen — a change is published as a new "
                          "version, never an edit of this one"),
                theme.row("Effective from",
                          f'<span style="color:{theme.TEXT};font-weight:650">'
                          f'{theme._esc(effective_txt)}</span>'),
                theme.row("Published on",
                          f'<span style="color:{theme.TEXT};font-weight:650">'
                          f'{theme._esc(published_txt)}</span>'),
                theme.row("Percentages add up to",
                          theme.pill(total_phrase, total_tier)),
                theme.row("Number of positions",
                          f'<span style="color:{theme.TEXT};font-weight:650">'
                          f'{len(rows)}</span>'),
            ]),
        ),
        unsafe_allow_html=True,
    )


def _render_priceability(label: str, entry: dict) -> None:
    """VALIDATION C-1 — ticker priceability. Only the desk has price history, so only the
    desk can find this. Shown LOUDLY and verbatim, never swallowed."""
    if entry.get("error"):
        st.markdown(
            theme.status_card(
                f"{label} — this allocation CANNOT be traded as published",
                "bad",
                "A ticker in this model has no price history the desk can size from",
                "Until this is fixed, an account on this model cannot be rebalanced to it. "
                "A position the desk cannot price would be sized at zero shares — never "
                "bought, and invisible to the drift check — so the desk refuses the whole "
                "book instead. Fix it either by loading price history for the ticker, or by "
                "publishing a new version of the allocation without it.",
            ),
            unsafe_allow_html=True,
        )
        st.error(entry["error"])
        return
    target = entry.get("target")
    if target is None:
        return
    tickers = ", ".join(str(t) for t in target.weights.index)
    st.markdown(
        theme.status_card(
            f"{label} — every ticker can be priced and sized",
            "good",
            f"{len(target.weights)} of {len(target.weights)} tickers have usable price "
            f"history",
            f"Checked against real local price history for exactly these tickers "
            f"({tickers}) as of {_fmt_date(target.price_date)}. This is the check the "
            f"client system cannot do — it does not hold prices.",
        ),
        unsafe_allow_html=True,
    )


def _render_whole_share(label: str, entry: dict) -> None:
    """VALIDATION C-2 — whole-share viability. Whether every position in the book is
    actually buyable in WHOLE shares at the relevant account size, and which ones round to
    nothing. Checked against the SMALLEST assigned account; with none assigned, against a
    clearly-labelled example size."""
    target = entry.get("target")
    if target is None:
        return

    accounts = entry.get("accounts") or []
    navs = [float(a.get("total_value") or 0.0) for a in accounts]
    navs = [n for n in navs if n > 0]
    if navs:
        nav = min(navs)
        smallest = min((a for a in accounts if float(a.get("total_value") or 0.0) > 0),
                       key=lambda a: float(a.get("total_value") or 0.0))
        basis = (f"the SMALLEST account assigned to this model "
                 f"({smallest.get('account_number')}, ${nav:,.0f})")
        example = False
    else:
        nav = EXAMPLE_NAV_SMALL if entry.get("small") else EXAMPLE_NAV_FULL
        basis = (f"an EXAMPLE account of ${nav:,.0f} — no account is assigned to this "
                 f"model yet, so there is no real account to check")
        example = True

    weights = {str(k): float(v) for k, v in target.weights.items()}
    prices = {str(k): float(v) for k, v in target.prices.items()}
    checks = whole_share_rows(weights, prices, nav)
    unbuyable = [c for c in checks if not c["buyable"]]
    floor_nav = min_nav_for_whole_book(weights, prices)

    if unbuyable:
        names = ", ".join(c["ticker"] for c in unbuyable)
        st.markdown(
            theme.status_card(
                f"{label} — whole-share check at ${nav:,.0f}"
                + (" (example size)" if example else ""),
                "bad",
                f"{len(unbuyable)} position(s) round to ZERO shares: {names}",
                f"Checked against {basis}. The desk buys whole shares only, so a position "
                f"whose dollar target is smaller than one share is simply never held — and "
                f"the miss never breaches a rebalance band, so nothing would ever flag it. "
                + (f"Every position in this model buys at least one whole share once the "
                   f"account is worth about ${floor_nav:,.0f}."
                   if floor_nav else ""),
            ),
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            theme.status_card(
                f"{label} — whole-share check at ${nav:,.0f}"
                + (" (example size)" if example else ""),
                "good",
                "Every position buys at least one whole share",
                f"Checked against {basis}. "
                + (f"Every position in this model stays buyable in whole shares down to an "
                   f"account of about ${floor_nav:,.0f}." if floor_nav else ""),
            ),
            unsafe_allow_html=True,
        )

    table = [{
        "Ticker": c["ticker"],
        "Target percentage": f"{c['target_weight'] * 100:.2f}%",
        "Dollars targeted": round(c["target_dollars"], 2),
        "Price used": "—" if c["price"] is None else round(c["price"], 2),
        "Whole shares bought": "—" if c["whole_shares"] is None else c["whole_shares"],
        "Dollars actually invested": round(c["invested"], 2),
        "Dollars left over": round(c["leftover"], 2),
        "Buyable in whole shares": "yes" if c["buyable"] else "NO",
        "What that means": c["note"] or "—",
    } for c in checks]
    st.dataframe(pd.DataFrame(table), hide_index=True, use_container_width=True)
    st.caption(
        f"Prices are the last close the desk holds ({_fmt_date(target.price_date)}) — the "
        f"same prices the rebalance sizing would use. Read-only arithmetic; no order of any "
        f"kind is built here.")


def _verdict_table(verdicts) -> pd.DataFrame:
    """The per-account drift readout, in spelled-out plain English column names."""
    return pd.DataFrame([{
        "Account": v["account"],
        "Advisor": v.get("advisor_name") or "—",
        "Entity": v.get("entity") or "—",
        "Verdict": ("HELD BACK — needs a look" if v.get("blocked")
                    else "OUT OF LINE with the allocation" if v.get("out_of_spec")
                    else "in line with the allocation"),
        "Account value": round(float(v.get("net_liq") or 0.0), 2),
        "Value the model manages": round(
            float(v.get("managed_net_liq", v.get("net_liq")) or 0.0), 2),
        "Value we never trade": round(float(v.get("held_aside_value") or 0.0), 2),
        "Positions": v.get("n_positions", 0),
        "Trades it would take": v.get("n_legs", 0),
    } for v in verdicts])


def _render_accounts_and_drift(label: str, entry: dict) -> None:
    """Which accounts are assigned to this model, how far they have drifted, and the
    read-only preview of the trades that would bring them back.

    Both halves are always rendered — the accounts that need attention AND the ones already
    in line. The retired Custom allocation page hid the in-line ones behind a checkbox; that
    filter was NOT carried across, so this page simply shows both, which is strictly more
    than that checkbox showed by default."""
    accounts = entry.get("accounts") or []
    if not accounts:
        st.markdown(
            theme.status_card(
                f"{label} — accounts assigned",
                "unknown",
                "No account is assigned to this model yet",
                "Nothing to rebalance and nothing to drift. Assigning an account is done in "
                "the client system, not here.",
            ),
            unsafe_allow_html=True,
        )
        return

    if entry.get("scan_error"):
        st.warning(
            f"{len(accounts)} account(s) are assigned to {label}, but their drift could not "
            f"be worked out right now ({entry['scan_error']}). The published allocation "
            f"above is unaffected, and nothing has been placed or sent.")
        return
    if entry.get("target") is None:
        st.warning(
            f"{len(accounts)} account(s) are assigned to {label}, but the allocation itself "
            f"cannot be turned into a tradeable book (see the message above), so no drift "
            f"read is possible until that is fixed.")
        return

    scan = entry.get("scan") or {}
    verdicts = scan.get("verdicts", [])
    skipped = scan.get("skipped", [])
    excluded = scan.get("excluded", [])
    n_oos = scan.get("n_out_of_spec", 0)

    c1, c2, c3 = st.columns(3)
    c1.metric("Accounts on this model", len(accounts))
    c2.metric("Out of line with the allocation", n_oos)
    c3.metric("In line with the allocation", scan.get("n_in_spec", 0))
    st.caption(
        f"Checked with the same read-only engine the desk uses everywhere else to look for "
        f"accounts that are out of line with their allocation — it sizes the account against "
        f"the published book and reports what would trade. It builds and sends nothing. "
        + (f"{len(skipped)} unfunded/no-snapshot account(s) skipped. " if skipped else "")
        + (f"{len(excluded)} account(s) held out for manual review (recorded value "
           f"disagrees with holdings). " if excluded else ""))

    needs_attention = [v for v in verdicts if v.get("out_of_spec") or v.get("blocked")]
    attention_ids = {id(v) for v in needs_attention}
    in_line = [v for v in verdicts if id(v) not in attention_ids]

    if not needs_attention:
        st.success("Every assigned account is in line with the published allocation — "
                   "nothing would trade.")
    else:
        st.markdown(f"**Accounts that are out of line with {label}**")
        st.dataframe(_verdict_table(needs_attention), hide_index=True,
                     use_container_width=True)

        st.markdown(f"**The trades that WOULD bring these {len(needs_attention)} "
                    f"account(s) to {label}** — read-only preview, nothing is sent")
        st.caption(
            "This is a preview of what the rebalance would do, worked out in this page from "
            "the published allocation and the latest holdings. No program is started, no "
            "broker connection is opened, and no order object exists.")
        any_legs = False
        for v in needs_attention:
            if not v.get("legs"):
                continue
            any_legs = True
            st.markdown(f"**{v['account']}** · {v.get('advisor_name') or '—'} — "
                        f"{v['n_legs']} trade(s)")
            st.dataframe(pd.DataFrame(v["legs"]), hide_index=True,
                         use_container_width=True)
        if not any_legs:
            st.caption("No account in this list would trade.")

    if in_line:
        st.markdown(f"**Accounts already in line with {label}** — nothing would trade for "
                    f"these")
        st.dataframe(_verdict_table(in_line), hide_index=True, use_container_width=True)


def _render_custom_model(label: str, entry: dict) -> None:
    """One hand-written model: its heading and badge at the top level, and everything about
    it — the published book, the version card, both desk-only validations, how much of the
    book is on it, the accounts assigned, their drift and the corrective trades — inside one
    collapsed section."""
    accounts = entry.get("accounts") or []
    n_accounts = len(accounts)

    if not entry.get("published"):
        st.markdown(
            _section_with_badge(
                label,
                f'<span style="color:{theme.MUTED};font-weight:600">'
                f"— written by Andrew · nothing published yet</span>"),
            unsafe_allow_html=True)
        st.markdown(
            theme.status_card(
                label, "unknown", "No holdings published yet",
                "Andrew has named this model in the client system but has not published a "
                "list of holdings for it, so there is no book to show here and nothing the "
                "desk could trade an account to. That is NOT the same as an empty or "
                "all-cash book: the desk refuses to build a target at all, because an empty "
                "target would sell an account down to nothing. Publishing a version in the "
                "client system makes it appear here.",
            ),
            unsafe_allow_html=True)
        if accounts:
            st.error(
                f"{n_accounts} account(s) are assigned to {label}, which has NO published "
                f"allocation. Those accounts have no book to be rebalanced to.")
        return

    st.markdown(
        _section_with_badge(
            label,
            f'<span style="color:{theme.ACCENT};font-weight:700">'
            f"— written by Andrew · {n_accounts} account(s) on it</span>"),
        unsafe_allow_html=True)

    with st.expander(f"Show what {label} holds, which accounts are on it, and how far they "
                     f"have drifted"):
        st.caption("Andrew chose these holdings and percentages himself in the client "
                   "system. There is no strategy engine behind this model and no regime "
                   "band applies to it — it holds exactly what is listed below until "
                   "Andrew publishes a new version.")

        _render_allocation(label, entry)

        # How much of the book is on this model.
        value = _assigned_value(accounts)
        st.markdown(
            theme.card(
                f"{label} — how much of the book is on this model",
                "".join([
                    theme.row("Accounts assigned to this model",
                              f'<span style="color:{theme.TEXT};font-weight:650">'
                              f'{n_accounts}</span>',
                              "assigning an account to a model is done in the client "
                              "system, never on this page"),
                    theme.row("Total recorded value of those accounts",
                              f'<span style="color:{theme.TEXT};font-weight:650">'
                              f'${value:,.0f}</span>' if value > 0 else
                              f'<span style="color:{theme.MUTED}">not recorded</span>'),
                    theme.row("Who decides the holdings",
                              f'<span style="color:{theme.TEXT}">Andrew, by hand, in the '
                              f'client system</span>',
                              "read here one way and read-only — this page cannot publish, "
                              "edit or send anything"),
                ]),
            ),
            unsafe_allow_html=True)

        # The two checks only the desk can make.
        _render_priceability(label, entry)
        _render_whole_share(label, entry)

        # The accounts on it, their drift, and the corrective trades.
        _render_accounts_and_drift(label, entry)


def _render_custom_family(custom: dict) -> None:
    """The whole hand-written family: its plain-English explanation, the read summary, then
    one collapsed section per model. If the client system cannot be read, says so in one
    sentence and stops — everything else on the page still renders."""
    st.markdown(theme.section("Models Andrew writes himself"), unsafe_allow_html=True)
    st.caption("These are the models Andrew authors by hand in the client system: he picks "
               "the funds and the percentages directly, and the desk reads them one way, "
               "read-only. Most of the accounts the desk actually manages are on one of "
               "these. They are shown separately from the models above because the "
               "difference is simply WHO CHOSE THE HOLDINGS — the strategy code chose the "
               "ones above, Andrew chose the ones here. Each one also carries the two "
               "checks only the desk can make: whether every ticker can be priced, and "
               "whether every position is buyable in whole shares.")

    if custom.get("error") == "not_configured":
        st.warning(
            "The connection to the client system is not set up on this machine, so the "
            "models Andrew writes himself cannot be read right now. Andrew must set the "
            "`TRADINGDESK_CRM_DSN` environment variable to the read-only role's connection "
            "string before this section can show anything. No credential is stored in code, "
            "nothing here transmits, and the models the strategy code works out are "
            "unaffected.")
        return
    if custom.get("error"):
        st.warning(
            "The models Andrew writes himself could not be read from the client system "
            f"right now ({custom['error']}). Nothing about any model has changed, nothing "
            "has been placed or sent, and the models the strategy code works out are "
            "unaffected.")
        return

    labels = custom.get("labels", [])
    models = custom.get("models", {})
    if not labels:
        st.info("The client system has no hand-written models to show.")
        return

    n_published = sum(1 for m in models.values() if m.get("published"))
    n_broken = sum(1 for m in models.values() if m.get("error"))

    s1, s2, s3 = st.columns(3)
    s1.metric("Models Andrew writes himself", len(labels))
    s2.metric("With an allocation published", n_published)
    s3.metric("Accounts assigned to one", custom.get("assigned_total", 0))
    st.caption(f"Read {custom.get('built_at_str', '—')} from the client system's read-only "
               f"view, across a book of {custom.get('n_roster', 0)} accounts. The read "
               f"renews itself automatically once it is {_CACHE_TTL_SECS // 60} minutes "
               f"old; to see a model published just now, use the re-read button at the top "
               f"of this page.")

    if n_broken:
        st.error(
            f"{n_broken} published allocation(s) CANNOT be traded as published — a ticker "
            f"has no price history the desk can size from. The affected model(s) are marked "
            f"below in red with the exact reason.")

    for label in labels:
        _render_custom_model(label, models.get(label, {"published": False}))


# =========================================================================== #
# Page entry point.                                                           #
# =========================================================================== #
def render_models() -> None:
    """Render the one read-only Models page: a list of EVERY model the desk knows about,
    today's live market regime read, then — behind a collapsed section per model — the
    models the strategy code works out (holdings resolved live, allowances, today's band)
    with Strategy 0's full rulebook, and the models Andrew writes himself (published book,
    ticker priceability, whole-share viability, the accounts assigned, their drift and the
    trades that would bring them back). Every number is pulled live — from the frozen
    engine and config for the computed family, and from the client system's read-only view
    for the hand-written one. Nothing here edits, writes, arms, or transmits. The page
    renders exactly ONE control — a button that re-reads the hand-written allocations from
    the client system — and it only clears a cache."""
    st.subheader("Strategy Models — what each model holds and why")
    st.caption("Every model the desk knows about, in one place: the ones the strategy code "
               "works out automatically and the ones Andrew writes himself. For each one, "
               "its exact holdings; for the automatic ones, today's regime band, the "
               "version allowance, the traded universe and the full rulebook behind them; "
               "for the hand-written ones, which accounts are on them, how far those "
               "accounts have drifted and the trades that would bring them back. Read-only "
               "throughout and pulled live, so nothing on this page can drift from what is "
               "really being traded. There is no edit control here; the one button on this "
               "page only re-reads the hand-written allocations.")

    _render_readonly_banner()

    # THE PAGE'S ONE AND ONLY CONTROL. It clears this page's own cached read of the
    # hand-written allocations and nothing else — it starts no program, opens no broker
    # connection, holds no arm token and sends nothing. It sits HERE, above the read on the
    # next line, so a click takes effect on this same render rather than the next one; that
    # is exactly how the retired Custom allocation page did it. Without it Andrew has to
    # wait out the 10-minute cache to see a model he has just published.
    if st.button("Re-read the models Andrew writes himself from the client system",
                 key="models_reread_custom",
                 help="Throws away this page's 10-minute cached read and reads the "
                      "published allocations from the client system again straight away. "
                      "Reading only — this changes nothing and sends nothing."):
        _load_custom_state.clear()

    # The two live reads, done once each and shared by every section below.
    state = _compute_live_state()
    custom = _custom_state()

    # THE LIST: every model, both families, one row each.
    _render_overview(custom)

    # The market read that governs the computed family.
    _render_live_state(state)

    # FAMILY 1 — the models the strategy code works out, plus their shared rulebook.
    _render_computed_family(state)

    # FAMILY 2 — the models Andrew writes himself.
    _render_custom_family(custom)
