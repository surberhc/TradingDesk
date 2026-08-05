"""page_s0_model.py — Strategy 0 "Model & Parameters" view. READ-ONLY, MODEL-DRIVEN.

A plain-English window into HOW Strategy 0 (Adaptive All-Weather Core) is defined:
the regime ladder, the re-entry ladder, the whipsaw controls, the client-version
allowances, and the traded ticker universe — plus TODAY's live regime read so the
owner can see exactly why the book sits where it does.

Two non-negotiables shape this page:
  * RULE #1 (never curve-fit): every threshold, band, gate, allowance, and ticker
    shown here is IMPORTED LIVE from the frozen strategy config (`strategies.config`)
    and regime engine (`strategies.parts.regime`) — NEVER hardcoded. The view can
    therefore never silently drift from the code. These parameters are FROZEN; this
    view DISPLAYS them and offers NO edit/write control of any kind.
  * READ-ONLY / broker-free: no gateway, no paper account, no order, no arm, no
    transmit. The live regime state is COMPUTED from end-of-day data through the
    exact same engine the live path uses (regime.market_health_score /
    apply_hysteresis) — the same code the nightly report and paperbot call — but it
    writes nothing and touches no broker.

IMPORT DISCIPLINE: module-top imports are CHEAP only (stdlib, pandas, streamlit,
theme). Every heavy import — the frozen `strategies.config`, the regime engine, the
backtester data loader, the market calendar — is LAZY (inside the function that needs
it), so importing this module opens no socket and runs no computation.
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import io
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

import theme

# --- Make the existing packages importable (reuse, don't rebuild) --------------
# Same sys.path bootstrap desk_app.py / page_s0.py use. This module lives at
# dashboard/desk/page_s0_model.py, so the repo root is parents[2].
REPO = Path(__file__).resolve().parents[2]
for _sub in ("paperbot", "backtester", "connections", "strategies", "dailyreport",
             "livebot"):
    _p = REPO / _sub
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
_conn = REPO / "connections"
if str(_conn) not in sys.path:
    sys.path.insert(0, str(_conn))

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


# =========================================================================== #
# The live regime state — computed through the SAME engine the live path uses. #
# =========================================================================== #
@st.cache_data(ttl=1800, show_spinner="Reading today's regime (validated engine, cached 30 min)...")
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
# Renderers — every number pulled LIVE from config / regime, never hardcoded.  #
# =========================================================================== #
def _render_frozen_banner() -> None:
    st.markdown(
        theme.status_card(
            "These parameters are frozen",
            "warn",
            "Read-only — no in-app editing",
            "Every threshold, band, gate, allowance, and ticker below is the frozen "
            "anti-curve-fit strategy configuration, imported live from the code. This "
            "view only DISPLAYS them. Changing any of these is a deliberate, validated "
            "code change (out-of-sample and per-regime checks) — never an in-app edit. "
            "Nothing on this page connects to a broker, places, arms, or transmits.",
        ),
        unsafe_allow_html=True,
    )


def _pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def _render_live_state() -> None:
    st.markdown(theme.section("Where the market is right now (live regime read)"),
                unsafe_allow_html=True)
    state = _compute_live_state()
    if "error" in state:
        st.warning("Today's regime could not be computed right now "
                   f"({state['error']}). The frozen model definition below is "
                   "unaffected.")
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
            head_sub,
        ),
        unsafe_allow_html=True,
    )

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

    # --- Raw vs confirmed + Growth band + next rebalance ---
    reb = _next_rebalance_dates()
    reb_txt = (f"Next scheduled monthly rebalance: signal on {reb[0]} (the last "
               f"trading day of the month), executed the next trading day, {reb[1]}."
               if reb else "Next rebalance date is unavailable right now.")
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
                  f"signal {reb[0]}" if reb else ""),
    ]
    st.markdown(theme.card("Raw versus confirmed regime, and what it allows",
                           "".join(detail_rows)),
                unsafe_allow_html=True)
    st.caption("Data sources for the stress component today: volatility from "
               f"{state['vix_src']}, credit spreads from {state['hy_oas_src']}. "
               "Computed from end-of-day data through the same engine the live "
               "strategy uses — read-only, nothing is transmitted.")


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
# Page entry point.                                                           #
# =========================================================================== #
def render_s0_model() -> None:
    """Render the read-only Strategy 0 Model & Parameters view: the frozen-config
    banner, today's live regime state (score + components + raw vs confirmed regime +
    Growth band + next rebalance), and the frozen model definition — regime ladder,
    re-entry ladder, whipsaw controls, version allowances, and ticker universe. Every
    number is pulled live from config/regime; nothing here edits, writes, or transmits."""
    st.subheader("Strategy 0 — Model & Parameters")
    st.caption("What Strategy 0 (Adaptive All-Weather Core) IS: its rules, thresholds, "
               "and current live regime read. Everything is read-only and pulled live "
               "from the frozen strategy code — there is no edit control on this page.")

    _render_frozen_banner()
    _render_live_state()
    _render_regime_ladder()
    _render_reentry_and_whipsaw()
    _render_version_allowances()
    _render_universe()
