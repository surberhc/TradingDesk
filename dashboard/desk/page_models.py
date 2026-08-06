"""page_models.py — "Strategy Models" hub. READ-ONLY, MODEL-DRIVEN.

STAGE 1 of the Models management surface (Models_Management_Spec_2026-08-05.md §2):
generalize the single-model `page_s0_model.py` view to show EVERY client-facing
strategy model concretely — its exact resolved holdings (ticker + %), today's regime
band, its version allowance, its tradeable universe, and a version/changelog line —
all pulled LIVE from the frozen shared brain so the page can never drift from the code.

Two non-negotiables shape this page (CLAUDE.md §"two non-negotiables"):
  * RULE #1 (never curve-fit): every weight, band, allowance, and ticker is IMPORTED
    LIVE — resolved holdings come from `strategy_target.current_target(version)` →
    `run_backtest` (the exact validated engine the paperbot executes), and the ladders /
    allowances / universe come from the frozen `strategies.config`. NOTHING is hardcoded,
    so the view cannot silently disagree with the executed book.
  * READ-ONLY / broker-free: no gateway, no order, no arm, no transmit, and — asserted by
    test — ZERO edit/input/button widgets. Model changes are a LATER stage: they flow
    through the gated review→validate→deploy pipeline (spec §3), never an in-app edit.

This module holds a small DISPLAY-ONLY model registry (which frozen version/tier to
resolve and show). It holds no weights and steers no decision — adding a row surfaces a
model, it does not define one. "Growth (Small)" is APPROVED but NOT YET DEPLOYED in
`strategies/` (SmallAccount_Tier_Proposal_2026-08-05.md), so it is shown as a clearly
labelled PROPOSED card — never fabricated as live.

IMPORT DISCIPLINE: module-top imports are CHEAP only. Every heavy import (the frozen
config, the resolver/backtester) is LAZY and cached (30-min TTL) so opening the page
opens no socket and runs no backtest needlessly. Reuses page_s0_model's renderers.
"""
from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

import theme
import page_s0_model as s0m

# --- Make the existing packages importable (reuse, don't rebuild) --------------
# Same sys.path bootstrap desk_app.py / page_s0_model.py use.
REPO = Path(__file__).resolve().parents[2]
for _sub in ("paperbot", "backtester", "connections", "strategies", "dailyreport",
             "livebot"):
    _p = REPO / _sub
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
_conn = REPO / "connections"
if str(_conn) not in sys.path:
    sys.path.insert(0, str(_conn))


# --- Display-only model registry ------------------------------------------------
# Which frozen version/tier to resolve and render. Descriptive metadata only — no
# weights, no decision. Growth-Small is PROPOSED (undefined in strategies/); its
# intended holdings live here purely as display text, flagged not-yet-deployed.
MODELS = [
    {
        "key": "growth", "label": "Growth", "version": "Growth", "status": "live",
        "note": "Strategy 0 (Adaptive All-Weather Core) runs this version live on the "
                "paper account. Highest equity allowance, no minimum cash floor.",
    },
    {
        "key": "balanced", "label": "Balanced", "version": "Balanced", "status": "resolves",
        "note": "Same engine as Growth with a 5% minimum cash (T-bill) floor — resolves "
                "to a distinct book (more USFR, less equity per sleeve).",
    },
    {
        "key": "conservative", "label": "Conservative", "version": "Conservative",
        "status": "resolves",
        "note": "Same engine with an 80% equity allowance and a 10% cash floor — the "
                "most defensive version; resolves to a distinct, lower-equity book.",
    },
    {
        "key": "growth_small", "label": "Growth (Small)", "version": "Growth",
        "status": "proposed",
        "note": "A whole-share-feasible PROXY of Growth for small accounts — Growth's "
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


# =========================================================================== #
# Live resolver — the exact validated engine the paperbot executes.           #
# =========================================================================== #
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


# =========================================================================== #
# Renderers.                                                                   #
# =========================================================================== #
def _render_frozen_banner() -> None:
    """Models-hub frozen banner — worded for the gated change pipeline (spec §3)."""
    st.markdown(
        theme.status_card(
            "These models are frozen — this page only displays them",
            "warn",
            "Read-only — no in-app editing",
            "Every holding, weight, band, allowance and ticker below is pulled LIVE from "
            "the frozen, anti-curve-fit strategy code — the resolved holdings come from "
            "the exact validated engine the paperbot executes, so this view can never "
            "drift from the real book. Changing any model is NOT an in-app edit: it goes "
            "through the gated review → validate → deploy flow (out-of-sample and "
            "per-regime checks, maker-checker approval) — a later stage. Nothing on this "
            "page connects to a broker, places, arms, or transmits.",
        ),
        unsafe_allow_html=True,
    )


def _render_holdings_table(rows: list[tuple], *, proposed: bool = False) -> None:
    """Render a ticker -> sleeve -> weight table from `rows` = [(ticker, weight, sleeve?)].
    For live models sleeve is looked up from config; for proposed models it is passed in."""
    header = (
        '<tr>'
        f'<th style="text-align:left;padding:.4rem .6rem;color:{theme.MUTED};'
        f'font-weight:600;border-bottom:1px solid {theme.BORDER}">Ticker</th>'
        f'<th style="text-align:left;padding:.4rem .6rem;color:{theme.MUTED};'
        f'font-weight:600;border-bottom:1px solid {theme.BORDER}">Sleeve</th>'
        f'<th style="text-align:right;padding:.4rem .6rem;color:{theme.MUTED};'
        f'font-weight:600;border-bottom:1px solid {theme.BORDER}">Target weight</th></tr>'
    )
    body = []
    total = 0.0
    for entry in rows:
        if proposed:
            tkr, wt, sleeve = entry
        else:
            tkr, wt = entry
            sleeve = _sleeve_of(tkr)
        total += wt
        body.append(
            '<tr>'
            f'<td style="padding:.45rem .6rem;border-bottom:1px solid {theme.BORDER};'
            f'color:{theme.TEXT};font-family:monospace;font-weight:650">{theme._esc(tkr)}</td>'
            f'<td style="padding:.45rem .6rem;border-bottom:1px solid {theme.BORDER};'
            f'color:{theme.MUTED};font-size:12.5px">{theme._esc(sleeve)}</td>'
            f'<td style="padding:.45rem .6rem;border-bottom:1px solid {theme.BORDER};'
            f'color:{theme.TEXT};text-align:right;font-weight:650">{wt * 100:.3f}%</td></tr>'
        )
    body.append(
        '<tr>'
        f'<td style="padding:.45rem .6rem;color:{theme.MUTED}">Total</td>'
        f'<td style="padding:.45rem .6rem"></td>'
        f'<td style="padding:.45rem .6rem;color:{theme.MUTED};text-align:right">'
        f'{total * 100:.1f}%</td></tr>'
    )
    st.markdown(
        f'<table style="border-collapse:collapse;width:100%;font-size:13.5px">'
        + header + "".join(body) + '</table>',
        unsafe_allow_html=True,
    )


def _render_model_card(model: dict, state: dict) -> None:
    """Render one model's block: header, resolved (or proposed) holdings, and a facts
    card (version allowance, today's version-scaled equity band, universe, version line)."""
    from strategies import config as scfg

    label = model["label"]
    version = model["version"]
    status = model["status"]
    proposed = status == "proposed"

    # --- Header with status marker ---
    if status == "live":
        badge = (f'<span style="color:{theme.TIER["good"]["c"]};font-weight:700">'
                 f'— LIVE (Strategy 0)</span>')
    elif proposed:
        badge = (f'<span style="color:{theme.TIER["warn"]["c"]};font-weight:700">'
                 f'— PROPOSED · not yet deployed</span>')
    else:
        badge = (f'<span style="color:{theme.MUTED};font-weight:600">'
                 f'— resolves to a distinct book</span>')
    st.markdown(theme.section(f"{theme._esc(label)} {badge}"), unsafe_allow_html=True)
    st.caption(model["note"])

    # --- Resolved (or proposed) holdings — the headline ---
    if proposed:
        st.markdown(
            theme.status_card(
                "Intended holdings (PROPOSED — not resolved from code)",
                "warn",
                "SCHB 85% + USFR 15% · whole-share proxy of Growth",
                "Growth (Small) is APPROVED but NOT YET DEFINED in strategies/, so these "
                "weights are the intended design shape — NOT a live engine resolution. "
                "In production it would track Growth's dynamic equity/defensive split "
                "(the 85/15 moves with the regime band, 0–100%). It is whole-share "
                "feasible down to $500. Auto-tier: an account assigned Growth with NAV "
                "below $25,000 resolves to Growth (Small); it auto-promotes to full "
                "Growth past $25k (hysteresis: promote ≥ $27,500, demote < $22,500).",
            ),
            unsafe_allow_html=True,
        )
        _render_holdings_table(model["proposed_rows"], proposed=True)
        st.caption("Display-only intended weights — no engine was run for this card. "
                   "Growth (Small) is not present in strategies/; defining it in code is "
                   "a later stage.")
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
        conf = s0m.REGIME_PLAIN.get(state["confirmed_regime"], state["confirmed_regime"])
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


def _render_shared_engine() -> None:
    """The engine detail shared by Growth / Balanced / Conservative (identical frozen
    config): regime ladder, re-entry ladder + whipsaw, the full version-allowances table,
    and the full ticker universe. Reuses page_s0_model's renderers verbatim."""
    st.markdown(theme.section("The shared engine behind these models"),
                unsafe_allow_html=True)
    st.caption("Growth, Balanced and Conservative are one validated brain — they share "
               "the SAME regime ladder, re-entry ladder, whipsaw controls and ticker "
               "universe; they differ only in the version allowance / cash floor shown on "
               "each card above (which is what makes their resolved books distinct). "
               "Growth (Small) shares this engine too, over a two-ticker universe.")
    s0m._render_regime_ladder()
    s0m._render_reentry_and_whipsaw()
    s0m._render_version_allowances()
    s0m._render_universe()


# =========================================================================== #
# Page entry point.                                                           #
# =========================================================================== #
def render_models() -> None:
    """Render the read-only Models hub: frozen banner, today's shared market regime read,
    one card per client-facing model (Growth / Balanced / Conservative resolved live, plus
    the PROPOSED Growth (Small) card), and the shared engine detail. Every number is pulled
    live from the frozen engine/config; nothing here edits, writes, arms, or transmits."""
    st.subheader("Strategy Models — every model, concretely")
    st.caption("What each strategy model IS right now: its exact resolved holdings, "
               "today's regime band, version allowance, tradeable universe and version. "
               "Read-only and pulled live from the frozen shared brain — there is no edit "
               "control on this page. This extends the single-model Strategy 0 view to all "
               "models (Models management, Stage 1).")

    _render_frozen_banner()

    # Shared market regime read — the regime is a market-wide fact, the same for every
    # model; each card then shows its own version-scaled band. Reused verbatim.
    st.markdown(theme.section("Where the market is right now "
                              "(shared by every model below)"),
                unsafe_allow_html=True)
    state = s0m._compute_live_state()
    if "error" in state:
        st.warning("Today's regime could not be computed right now "
                   f"({state['error']}). The frozen model definitions below are "
                   "unaffected.")
    else:
        conf = s0m.REGIME_PLAIN.get(state["confirmed_regime"], state["confirmed_regime"])
        tier = "good" if state["confirmed_regime"] in ("RiskOn", "RiskOnNarrowing") else (
            "warn" if state["confirmed_regime"] == "Caution" else "bad")
        st.markdown(
            theme.status_card(
                f"Market Health Score (as of {state['as_of']})",
                tier,
                f"{state['score']:.1f} out of 100 — {conf}",
                "This confirmed regime governs the equity band for every model; each "
                "model's card scales this band by its own version allowance.",
            ),
            unsafe_allow_html=True,
        )

    # One card per model.
    for model in MODELS:
        _render_model_card(model, state)

    # The engine detail shared across the Growth-family models (rendered once).
    _render_shared_engine()
