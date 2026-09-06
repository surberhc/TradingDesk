"""page_models.py — "Strategy Models" hub. READ-ONLY, MODEL-DRIVEN.

STAGE 1 of the Models management surface (Models_Management_Spec_2026-08-05.md §2):
generalize the single-model `page_s0_model.py` view to show EVERY client-facing
strategy model concretely — its exact resolved holdings (ticker + %), today's regime
band, its version allowance, its tradeable universe, and a version/changelog line —
all pulled LIVE from the frozen shared brain so the page can never drift from the code.

TWO FAMILIES OF MODEL, BOTH SHOWN (2026-09-05). "All models" previously meant only the
four versions Strategy 0's code computes. The book that is actually traded runs mostly on
the models Andrew writes HIMSELF in the client system, so those are shown here too and the
page is honest about the word "all". The two families are rendered as two clearly-labelled
sections and are never mixed:
  * The computed family keeps the "S0 " prefix on its DISPLAY label ("S0 Growth", "S0
    Balanced", "S0 Conservative", "S0 Growth (Small)") so a computed Growth can never be
    read as the hand-written Growth. The prefix is display text ONLY — the `version` value
    the engine resolves against stays the frozen identifier ("Growth", "Balanced",
    "Conservative"), untouched.
  * The hand-written family is read LIVE from the client system by CALLING the Custom
    allocation page's own single loader (`page_custom_alloc._load_state`) and its own book
    renderer (`_render_allocation`). This page runs no query of its own: one read path,
    one presentation, so the two model pages cannot drift apart or disagree. If the client
    system cannot be reached, that section says so in one plain sentence and the computed
    section still renders in full.

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

# page_s0_model.py lives in the strategy_views/ sub-folder, and this file does not.
# That folder has to be on sys.path BEFORE the import just below runs, because this
# page reuses page_s0_model's renderers. desk_app.py adds the same folder when the
# dashboard starts; this line is what lets this page also be imported on its own,
# such as from a test.
_STRATEGY_VIEWS = str(Path(__file__).resolve().parent / "strategy_views")
if _STRATEGY_VIEWS not in sys.path:
    sys.path.insert(0, _STRATEGY_VIEWS)

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
def _section_with_badge(label: str, badge_html: str) -> str:
    """A section heading reading "<model name> — <coloured status badge>".

    ``theme.section()`` escapes its ENTIRE argument, so handing it a ``<span>`` puts the raw
    tag on screen as literal text — which is exactly what this page did until 2026-09-05.
    The heading is assembled here instead, using the same ``dk-section`` class so it looks
    identical to every other heading: the model name is escaped (it is data), the badge is
    kept as markup (it is this module's own fixed string, never user input)."""
    return f'<div class="dk-section">{theme._esc(label)} {badge_html}</div>'


def _render_frozen_banner() -> None:
    """Models-hub frozen banner — worded for the gated change pipeline (spec §3)."""
    st.markdown(
        theme.status_card(
            "This page only displays these models — it cannot change any of them",
            "warn",
            "Read-only — no in-app editing",
            "Nothing below is written down in this page. The automatic models are pulled "
            "LIVE from the frozen, anti-curve-fit strategy code — their holdings come from "
            "the exact validated engine the paperbot executes — and the models Andrew "
            "writes himself are read LIVE from the client system, read-only and one way. "
            "So this view can never drift from the real book, either way. Changing an "
            "automatic model is NOT an in-app edit: it goes through the gated review → "
            "validate → deploy flow (out-of-sample and per-regime checks, maker-checker "
            "approval) — a later stage. Changing a hand-written model means Andrew "
            "publishing a new version in the client system, which is also not done here. "
            "Nothing on this page connects to a broker, places, arms, or transmits.",
        ),
        unsafe_allow_html=True,
    )


def _render_holdings_table(rows: list[tuple], *, proposed: bool = False,
                           show_sleeve: bool = True) -> None:
    """Render a ticker -> sleeve -> weight table from `rows` = [(ticker, weight, sleeve?)].
    For live models sleeve is looked up from config; for proposed models it is passed in.

    `show_sleeve=False` drops the sleeve column entirely and expects 2-tuples. It exists for
    books that HAVE no sleeve concept — a hand-authored ("custom") allocation, where Andrew
    picks the tickers directly (page_custom_alloc.py). `_sleeve_of` maps a ticker to one of
    Strategy 0's four sleeves and returns a dash for everything else, so such a book would
    otherwise render an always-blank column; omitting the column is honest, a column of
    dashes is not. The default is unchanged, so every existing caller renders identically."""
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
    st.markdown(_section_with_badge(label, badge), unsafe_allow_html=True)
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
    st.markdown(theme.section("The shared engine behind the four models above"),
                unsafe_allow_html=True)
    st.caption("S0 Growth, S0 Balanced and S0 Conservative are one validated brain — they "
               "share the SAME regime ladder, re-entry ladder, whipsaw controls and ticker "
               "universe; they differ only in the version allowance / cash floor shown on "
               "each card above (which is what makes their resolved books distinct). "
               "S0 Growth (Small) shares this engine too, over a two-ticker universe. None "
               "of this applies to the models Andrew writes himself, further down the page "
               "— those have no engine behind them at all, because Andrew picks their "
               "holdings directly.")
    s0m._render_regime_ladder()
    s0m._render_reentry_and_whipsaw()
    s0m._render_version_allowances()
    s0m._render_universe()


# =========================================================================== #
# The models Andrew writes himself — read LIVE from the client system by       #
# CALLING the Custom allocation page's loader. This page runs no query.        #
# =========================================================================== #
def _load_custom_models() -> dict:
    """Return the hand-written models exactly as the Custom allocation page sees them.

    REUSE, NOT A SECOND COPY. ``page_custom_alloc._load_state`` is the one read path to the
    client system's read-only view ``v_tradingdesk_custom_allocations`` (a SELECT under the
    ``tradingdesk_readonly`` role), and it is called here verbatim. No query, no connection
    string and no ticker/percentage is written down in this file — so a hand-written book
    shown on this page IS the published book, and the two model pages cannot disagree.
    It is also the SAME cached loader object, so opening both pages reads the client system
    once, not twice.

    Never raises to the page: any problem comes back as ``{"error": ...}`` so the section
    degrades to one plain-English sentence and the computed models still render."""
    try:
        import page_custom_alloc          # lives in strategy_views/, already on sys.path
    except Exception as exc:  # noqa: BLE001 — never take the page down
        return {"error": f"{type(exc).__name__}: {exc}"}
    try:
        return page_custom_alloc._load_state()
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def _assigned_value(accounts) -> float:
    """Total recorded value of the accounts assigned to a model. Display only."""
    total = 0.0
    for a in accounts or []:
        try:
            total += float(a.get("total_value") or 0.0)
        except (TypeError, ValueError):
            continue
    return total


def _render_custom_model_card(label: str, entry: dict) -> None:
    """One hand-written model: its holdings (ticker + percent), which published version it
    is, and how many accounts are on it. The book and the version card are rendered by the
    Custom allocation page's OWN renderer, so the two pages present a book identically."""
    import page_custom_alloc as ca

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
                "desk could trade an account to. Publishing a version in the client system "
                "makes it appear here."
                + (f" {n_accounts} account(s) are already assigned to it, and they have no "
                   f"book to be brought in line with." if n_accounts else ""),
            ),
            unsafe_allow_html=True)
        return

    st.markdown(
        _section_with_badge(
            label,
            f'<span style="color:{theme.ACCENT};font-weight:700">'
            f"— written by Andrew · {n_accounts} account(s) on it</span>"),
        unsafe_allow_html=True)
    st.caption("Andrew chose these holdings and percentages himself in the client system. "
               "There is no strategy engine behind this model and no regime band applies to "
               "it — it holds exactly what is listed below until Andrew publishes a new "
               "version.")

    # The published book + its version card, rendered by the Custom allocation page itself.
    ca._render_allocation(label, entry)

    # What that page does not show and this one needs: how much of the book is on this model.
    value = _assigned_value(accounts)
    st.markdown(
        theme.card(
            f"{label} — how much of the book is on this model",
            "".join([
                theme.row("Accounts assigned to this model",
                          f'<span style="color:{theme.TEXT};font-weight:650">'
                          f'{n_accounts}</span>',
                          "assigning an account to a model is done in the client system, "
                          "never on this page"),
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


def _render_custom_section() -> None:
    """The whole hand-written family: its plain-English explanation, then one card per
    model. If the client system cannot be read, says so in one sentence and stops."""
    st.markdown(theme.section("Models Andrew writes himself"), unsafe_allow_html=True)
    st.caption("These are the models Andrew authors by hand in the client system: he picks "
               "the funds and the percentages directly, and the desk reads them one way, "
               "read-only. Most of the accounts the desk actually manages are on one of "
               "these. They are shown separately from the four models above because the "
               "difference is simply WHO CHOSE THE HOLDINGS — the strategy code chose the "
               "ones above, Andrew chose the ones here.")

    state = _load_custom_models()

    if state.get("error") == "not_configured":
        st.warning(
            "The connection to the client system is not set up on this machine, so the "
            "models Andrew writes himself cannot be read right now. The four computed "
            "models above are unaffected.")
        return
    if state.get("error"):
        st.warning(
            "The models Andrew writes himself could not be read from the client system "
            f"right now ({state['error']}). Nothing about any model has changed, and the "
            "four computed models above are unaffected.")
        return

    labels = state.get("labels", [])
    models = state.get("models", {})
    if not labels:
        st.info("The client system has no hand-written models to show.")
        return

    n_published = sum(1 for m in models.values() if m.get("published"))
    st.caption(f"{len(labels)} hand-written model(s), {n_published} of them with holdings "
               f"published, covering {state.get('assigned_total', 0)} account(s). Read "
               f"{state.get('built_at_str', '—')} from the client system's read-only view, "
               f"across a book of {state.get('n_roster', 0)} accounts — the exact same read "
               f"the Custom allocation page uses, so the two pages can never disagree.")

    for label in labels:
        _render_custom_model_card(label, models.get(label, {"published": False}))


# =========================================================================== #
# Page entry point.                                                           #
# =========================================================================== #
def render_models() -> None:
    """Render the read-only Models hub in TWO clearly-separated families: the models the
    strategy code computes (S0 Growth / S0 Balanced / S0 Conservative resolved live, plus
    the PROPOSED S0 Growth (Small) card) with the shared market regime read and the shared
    engine detail; then the models Andrew writes himself, read live from the client system.
    Every number is pulled live — from the frozen engine/config for the computed family, and
    from the client system's read-only view for the hand-written one. Nothing here edits,
    writes, arms, or transmits."""
    st.subheader("Strategy Models — every model, concretely")
    st.caption("Every model the desk knows about, in two groups: the ones the strategy code "
               "works out automatically, and the ones Andrew writes himself. For each one: "
               "its exact holdings, and — for the automatic ones — today's regime band, "
               "version allowance and tradeable universe. Read-only throughout, pulled live "
               "from the frozen shared brain and from the client system, so nothing on this "
               "page can drift from what is really being traded. There is no edit control "
               "here (Models management, Stage 1).")

    _render_frozen_banner()

    # ---- FAMILY 1: the models the strategy code works out by itself. --------------
    st.markdown(theme.section("Models the strategy code works out automatically"),
                unsafe_allow_html=True)
    st.caption("These four are computed by the desk's own validated strategy code — nobody "
               "picks their holdings by hand; the code decides what to hold from how the "
               "market is behaving. Their names all start with \"S0\", short for Strategy 0, "
               "the strategy that runs them, so that the automatic Growth is never confused "
               "with the Growth that Andrew writes himself further down this page.")

    # Shared market regime read — the regime is a market-wide fact, the same for every
    # computed model; each card then shows its own version-scaled band. Reused verbatim.
    st.markdown(theme.section("Where the market is right now "
                              "(shared by the four automatic models)"),
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
                "This confirmed regime governs the equity band for every automatic model; "
                "each model's card scales this band by its own version allowance. It does "
                "not apply to the models Andrew writes himself — those hold exactly what "
                "he published, whatever the market is doing.",
            ),
            unsafe_allow_html=True,
        )

    # One card per computed model.
    for model in MODELS:
        _render_model_card(model, state)

    # The engine detail shared across the four computed models (rendered once).
    _render_shared_engine()

    # ---- FAMILY 2: the models Andrew writes himself, read live from the client system.
    _render_custom_section()
