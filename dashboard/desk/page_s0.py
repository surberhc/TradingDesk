"""page_s0.py — the rebuilt desk's Strategy 0 page. READ-ONLY, MODEL-DRIVEN.

Strategy 0 (Adaptive All-Weather Core) is the desk's all-weather core. This page
LEADS with the current regime read and the model's Daily / Month-to-date /
Year-to-date performance for the three client versions (Conservative / Balanced /
Growth). Everything is computed from end-of-day data through the VALIDATED backtest
engine — the exact code path the strategy itself uses (rule #1: never a separate,
curve-fit copy). There is NO broker connection anywhere on this page: no gateway, no
live paper account, no order, no arm, no transmit.

RULE #1 (never curve-fit): every regime threshold, band, gate, and allowance shown
here is IMPORTED from the frozen strategy config (`strategies.config`) — never
hardcoded or invented. The live regime score is READ from the nightly-published
status file; it is not recomputed here.

IMPORT DISCIPLINE: module-top imports are CHEAP only (stdlib, pandas, streamlit,
theme). Every heavy import — the frozen `strategies.config`, the backtester `bt`/`mt`,
and `strategy_target` — is LAZY (inside the function that needs it), so importing this
module opens no socket and runs no backtest.
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

import theme

# --- Make the existing packages importable (reuse, don't rebuild) --------------
# Same sys.path bootstrap app.py / desk_app.py use. This module lives at
# dashboard/desk/page_s0.py, so the repo root is parents[2].
REPO = Path(__file__).resolve().parents[2]
for _sub in ("paperbot", "backtester", "connections", "strategies", "dailyreport",
             "livebot"):
    _p = REPO / _sub
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
_conn = REPO / "connections"
if str(_conn) not in sys.path:
    sys.path.insert(0, str(_conn))

BACKTEST_OUTPUT = REPO / "backtester" / "output"
BACKTEST_VERSIONS = ("Conservative", "Balanced", "Growth")

# The nightly-published live regime read + the dashboard's own score-history log.
S0_REGIME_JSON = Path(
    r"C:\TradingDesk-Local\state\dailyreport\status\s0_regime.json")
SCORE_HISTORY_CSV = Path(
    r"C:\TradingDesk-Local\state\desk_dashboard\s0_score_history.csv")

# Plain-English name + colour tier for each regime bucket (spelled out, no shorthand).
REGIME_PLAIN = {
    "RiskOn": "Risk-On",
    "RiskOnNarrowing": "Risk-On narrowing",
    "Caution": "Caution",
    "Defensive": "Defensive",
    "CapitalPreservation": "Capital preservation",
}
REGIME_TIER = {
    "RiskOn": "good", "RiskOnNarrowing": "good",
    "Caution": "warn",
    "Defensive": "bad", "CapitalPreservation": "bad",
}

# Display-only colour grade for the regime ladder (green -> red). These tint the
# ladder rungs; they are NOT strategy knobs — they steer no decision, only the eye.
REGIME_LADDER_COLOR = {
    "RiskOn": "#3fb950",            # strong green (most aggressive)
    "RiskOnNarrowing": "#a3c93a",   # yellow-green
    "Caution": "#d29922",           # amber
    "Defensive": "#db6d28",         # orange
    "CapitalPreservation": "#f85149",  # red (most defensive)
}


def _hex_rgba(hexc: str, alpha: float) -> str:
    """'#3fb950' + alpha -> 'rgba(r,g,b,alpha)' (display tint helper)."""
    h = hexc.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r},{g},{b},{alpha})"

# Plain-English gloss for each headline backtest metric (demoted to the details expander).
METRIC_LABELS = {
    "CAGR": "Compound annual growth rate (CAGR)",
    "Max drawdown": "Maximum drawdown (worst peak-to-trough drop)",
    "Calmar": "Calmar ratio (annual growth / worst drawdown — higher is better)",
    "Sortino": "Sortino ratio (return per unit of downside risk — higher is better)",
    "Down capture vs SPY":
        "Down-capture vs the S&P 500 (how much of the market's losses it takes)",
}
_HEADLINE_METRICS = list(METRIC_LABELS.keys())
_PERCENT_METRICS = {"CAGR", "Max drawdown", "Down capture vs SPY"}
_RISK_METRICS = {"Max drawdown", "Down capture vs SPY"}


# =========================================================================== #
# The validated model — run ONCE per version (cached 1h), reused everywhere.   #
# =========================================================================== #
@st.cache_data(ttl=3600, show_spinner="Running the model (validated engine, cached 1 hour)...")
def _model_result(version: str) -> dict:
    """Run the VALIDATED run_backtest once per version (cached 1h) and return the
    pieces this page needs: the model NAV series, the benchmark NAV frame (SPY /
    60/40 / T-bills / strategy), and the latest target stock exposure. Read-only
    compute — the exact code path the strategy uses, touches no broker."""
    from src import backtest as bt
    from strategies import config as scfg
    with contextlib.redirect_stdout(io.StringIO()):
        res = bt.run_backtest(version=version, end=None)
    # Latest target book's equity-sleeve weight = current target stock exposure.
    latest_w = res["weights"].iloc[-1]
    eq_tickers = set(scfg.EQUITY_CORE) | set(scfg.SECTORS)
    exposure = float(latest_w[[t for t in latest_w.index if t in eq_tickers]].sum())
    return {"nav": res["nav"], "bench": res["benchmark_navs"], "exposure": exposure}


def _period_returns(nav: pd.Series) -> tuple:
    """(daily, month-to-date, year-to-date) simple returns from a daily NAV series.
    MTD baselines to the last value on/before the final trading day of the prior
    month; YTD baselines to the last value on/before Dec 31 of the prior year."""
    nav = nav.dropna()
    if len(nav) < 2:
        return (None, None, None, None, None)  # last date carried for callers
    latest_date = nav.index[-1]
    latest = float(nav.iloc[-1])
    daily = latest / float(nav.iloc[-2]) - 1.0

    month_start = latest_date.replace(day=1)
    prior_month = nav[nav.index < month_start]
    mtd = latest / float(prior_month.iloc[-1]) - 1.0 if len(prior_month) else None

    year_start = latest_date.replace(month=1, day=1)
    prior_year = nav[nav.index < year_start]
    ytd = latest / float(prior_year.iloc[-1]) - 1.0 if len(prior_year) else None
    return (daily, mtd, ytd, latest_date, month_start)


# =========================================================================== #
# Home P&L tile summary — now model-driven (month-to-date), keys unchanged.    #
# =========================================================================== #
def s0_pnl_summary() -> dict:
    """Month-to-date summary of the Strategy 0 MODEL (Balanced version) for the home
    P&L tile. Computed from the validated backtest NAV (end-of-day data) — no broker
    call, no paper account. Returns the SAME keys the home tile expects:
    {"change_pct": float|None, "since": str|None, "as_of": str|None, "note": str},
    where change_pct is a PERCENT (e.g. +0.4 => "+0.4% month-to-date")."""
    slow = ("Strategy 0 is a slow, end-of-day strategy — value moves gradually, "
            "so small month-to-date moves are normal.")
    try:
        nav = _model_result("Balanced")["nav"]
        daily, mtd, ytd, as_of_date, month_start = _period_returns(nav)
    except Exception:
        return {"change_pct": None, "since": None, "as_of": None,
                "note": "Model performance could not be computed right now. " + slow}

    if mtd is None or as_of_date is None:
        return {"change_pct": None, "since": None, "as_of": None,
                "note": "Not enough model history this month yet. " + slow}

    return {
        "change_pct": mtd * 100.0,
        "since": month_start.date().isoformat(),
        "as_of": as_of_date.date().isoformat(),
        "note": "Month-to-date change in the Strategy 0 model (Balanced). " + slow,
    }


# =========================================================================== #
# Real-money gate card — the sacred review -> arm -> transmit wall, plain.     #
# =========================================================================== #
def _render_gate_card() -> None:
    st.markdown(
        theme.status_card(
            "Real-money transmission",
            "bad",  # colour only: red = the wall is deliberately closed
            "OFF — deliberately gated",
            "Strategy 0 runs on the model. A real order only ever transmits behind an "
            "explicit, armed, human decision — the sacred review -> arm -> transmit "
            "gate. Nothing on this page connects to a broker, places, arms, or "
            "transmits anything.",
        ),
        unsafe_allow_html=True,
    )


# =========================================================================== #
# Regime — READ the live score, IMPORT the frozen bands, log the score trend.  #
# =========================================================================== #
def _read_live_regime() -> dict | None:
    """Read the nightly-published live regime score (never recompute it here)."""
    try:
        data = json.loads(S0_REGIME_JSON.read_text())
        m = data.get("metrics", {})
        as_of = str(m.get("as_of", ""))
        return {
            "score": float(m["score"]),
            "raw_regime": m.get("raw_regime"),
            "confirmed_regime": m.get("confirmed_regime"),
            "as_of": as_of,
        }
    except Exception:
        return None


def _as_of_iso(as_of: str) -> str | None:
    """'20260728' -> '2026-07-28'."""
    if len(as_of) == 8 and as_of.isdigit():
        return f"{as_of[:4]}-{as_of[4:6]}-{as_of[6:8]}"
    return as_of or None


def _log_and_read_score_history(as_of_iso: str, score: float) -> pd.DataFrame:
    """Append today's (date, score) to the dashboard's own history CSV — idempotent
    per date — and return the full history sorted by date. This is the dashboard's
    file, safe to write; it changes no strategy knob."""
    try:
        SCORE_HISTORY_CSV.parent.mkdir(parents=True, exist_ok=True)
        if SCORE_HISTORY_CSV.exists():
            hist = pd.read_csv(SCORE_HISTORY_CSV, dtype={"date": str})
        else:
            hist = pd.DataFrame(columns=["date", "score"])
        if as_of_iso and as_of_iso not in set(hist.get("date", [])):
            new_row = pd.DataFrame([{"date": as_of_iso, "score": score}])
            hist = new_row if hist.empty else pd.concat([hist, new_row],
                                                        ignore_index=True)
            hist = hist.drop_duplicates(subset=["date"]).sort_values("date")
            hist.to_csv(SCORE_HISTORY_CSV, index=False)
        else:
            hist = hist.drop_duplicates(subset=["date"]).sort_values("date")
        return hist.reset_index(drop=True)
    except Exception:
        return pd.DataFrame(columns=["date", "score"])


def _score_trend(hist: pd.DataFrame) -> str | None:
    """Plain-English recent trend from >=2 dated score points, or None if too few.
    (Display-only trend; NOT a regime gate, so its tolerance is cosmetic, not a
    frozen config value.)"""
    if hist is None or len(hist) < 2:
        return None
    recent = hist["score"].astype(float)
    change = float(recent.iloc[-1]) - float(recent.iloc[0])
    if change > 0.5:
        return ("score rising (coming out of a drawdown — the health bands are "
                "expanding up)")
    if change < -0.5:
        return "score falling (heading toward a drawdown)"
    return "score steady"


def _score_heading(hist: pd.DataFrame, reg: dict, order_desc: list,
                   cross_pts: float) -> str:
    """Plain-English heading arrow ('if it closed today, we'd ...'), driven by the
    dashboard's own accumulating score history + the frozen dead-zone. order_desc is
    the ladder order TOP->BOTTOM (most-aggressive first). Display-only."""
    confirmed = reg["confirmed_regime"] or reg["raw_regime"]
    cur_plain = REGIME_PLAIN.get(confirmed, confirmed)
    idx = order_desc.index(confirmed) if confirmed in order_desc else -1
    higher = order_desc[idx - 1] if idx > 0 else None            # more aggressive (above)
    lower = order_desc[idx + 1] if 0 <= idx < len(order_desc) - 1 else None  # below

    if hist is None or len(hist) < 2:
        return ("\u2192 Holding today \u2014 the heading arrow appears once a few "
                "nightly scores accumulate. If it closed here today, we'd stay in "
                f"{cur_plain}.")

    scores = hist["score"].astype(float)
    change = float(scores.iloc[-1]) - float(scores.iloc[-2])  # latest vs prior point
    if change >= cross_pts and higher is not None:
        return (f"\u2191 Heading up \u2014 moving toward {REGIME_PLAIN.get(higher, higher)} "
                f"(recovering). If it closed here today, we'd be climbing out of "
                f"{cur_plain}.")
    if change <= -cross_pts and lower is not None:
        return (f"\u2193 Heading down \u2014 drifting toward {REGIME_PLAIN.get(lower, lower)}. "
                f"If it closed here today, we'd be sliding out of {cur_plain}.")
    return (f"\u2192 Holding \u2014 comfortably inside {cur_plain}. If it closed here "
            f"today, we'd stay in {cur_plain}.")


def _render_regime_ladder(reg: dict, scfg, hist: pd.DataFrame) -> None:
    """The 5-rung regime LADDER: most-aggressive at TOP -> most-defensive at BOTTOM,
    colour-graded green->red, current rung highlighted with the live score marked, plus
    a heading arrow. Order is DERIVED from the frozen REGIME_BANDS score floors."""
    bands = scfg.REGIME_BANDS
    score = reg["score"]
    confirmed = reg["confirmed_regime"] or reg["raw_regime"]
    cross_pts = scfg.REGIME_MIN_THRESHOLD_CROSS

    # TOP->BOTTOM = score floor DESCENDING (derived, never hardcoded order).
    order_desc = sorted(bands, key=lambda r: bands[r]["score"][0], reverse=True)

    as_of_iso = _as_of_iso(reg["as_of"]) or reg["as_of"]
    heading = _score_heading(hist, reg, order_desc, cross_pts)

    # --- Header line + heading arrow.
    header = (
        f'<div style="margin:.2rem 0 .55rem 0">'
        f'<span style="font-size:15px;font-weight:650;color:{theme.TEXT}">'
        f'Where the market is right now</span>'
        f'<span style="font-size:11.5px;color:{theme.MUTED};margin-left:.5rem">'
        f'as of {theme._esc(as_of_iso)}</span>'
        f'<div style="font-size:12.5px;color:{theme.TEXT};margin-top:.25rem">'
        f'{theme._esc(heading)}</div>'
        f'</div>'
    )

    # --- The ladder rungs (top = most aggressive, bottom = most defensive).
    rungs = []
    for name in order_desc:
        s_lo, s_hi = bands[name]["score"]
        e_lo, e_hi = bands[name]["equity"]
        plain = REGIME_PLAIN.get(name, name)
        color = REGIME_LADDER_COLOR.get(name, theme.MUTED)
        is_cur = (name == confirmed)

        if is_cur:
            fill = _hex_rgba(color, 0.22)
            border = f"1px solid {color}"
            accent = f"5px solid {color}"
            opacity = "1"
            name_weight = "800"
            marker = (
                f'<div style="margin-left:auto;white-space:nowrap;font-size:12.5px;'
                f'font-weight:700;color:{color}">\u25cf Current \u2014 score '
                f'{score:.1f}</div>'
            )
        else:
            fill = _hex_rgba(color, 0.06)
            border = f"1px solid {theme.BORDER}"
            accent = f"5px solid {_hex_rgba(color, 0.45)}"
            opacity = "0.62"
            name_weight = "550"
            marker = ""

        rungs.append(
            f'<div style="display:flex;align-items:center;gap:.7rem;'
            f'background:{fill};border:{border};border-left:{accent};'
            f'border-radius:10px;padding:.55rem .85rem;margin-bottom:.4rem;'
            f'opacity:{opacity}">'
            f'<span style="flex:0 0 12px;width:12px;height:12px;border-radius:50%;'
            f'background:{color}"></span>'
            f'<div style="min-width:0">'
            f'<div style="font-size:14px;font-weight:{name_weight};color:{theme.TEXT}">'
            f'{theme._esc(plain)}</div>'
            f'<div style="font-size:11.5px;color:{theme.MUTED};margin-top:.1rem">'
            f'score {s_lo:.0f}\u2013{s_hi:.0f} &middot; '
            f'stocks {e_lo * 100:.0f}\u2013{e_hi * 100:.0f}%</div>'
            f'</div>{marker}</div>'
        )

    st.markdown(header + "".join(rungs), unsafe_allow_html=True)


def _render_regime_banner() -> dict | None:
    """The shared regime section: a 5-rung colour-graded LADDER (most-aggressive top ->
    most-defensive bottom) with the current rung highlighted and the live score marked,
    a heading arrow, and the cushion-to-next-lower-band supporting line. Returns the
    regime dict (so the version cards can reuse score/confirmed) or None if the read
    failed."""
    from strategies import config as scfg

    reg = _read_live_regime()
    if reg is None:
        st.warning("The nightly regime read could not be found \u2014 the regime banner "
                   "will light up after the next end-of-day publish.")
        return None

    score = reg["score"]
    confirmed = reg["confirmed_regime"] or reg["raw_regime"]
    bands = scfg.REGIME_BANDS
    plain = REGIME_PLAIN.get(confirmed, confirmed)

    # --- Log tonight's score, read back the accumulating history, draw the ladder.
    as_of_iso = _as_of_iso(reg["as_of"])
    hist = _log_and_read_score_history(as_of_iso, score)
    _render_regime_ladder(reg, scfg, hist)

    # --- Cushion to the next-LOWER band floor, from published score + frozen floors.
    order = sorted(bands, key=lambda r: bands[r]["score"][0])  # low -> high
    cur_floor = bands[confirmed]["score"][0]
    cushion = score - cur_floor
    idx = order.index(confirmed)
    drop_pts = scfg.REGIME_IMMEDIATE_DROP_POINTS
    cross_pts = scfg.REGIME_MIN_THRESHOLD_CROSS
    rule = (f"(the model de-risks immediately on a {drop_pts:.0f}-point drop and needs "
            f"a decisive {cross_pts:.0f}-point move to change buckets)")
    if idx > 0:
        lower = order[idx - 1]
        cushion_line = (f"{cushion:.0f} points of cushion before it would step down to "
                        f"{REGIME_PLAIN.get(lower, lower)}. {rule}")
    else:
        cushion_line = (f"Already at the most defensive bucket ({plain}). {rule}")

    st.markdown(theme.card("How much room before the regime changes",
                           theme._esc(cushion_line)),
                unsafe_allow_html=True)
    return reg


# =========================================================================== #
# The three version cards — PERFORMANCE first, then regime posture.            #
# =========================================================================== #
def _pct_span(x) -> str:
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return f'<span style="color:{theme.MUTED}">not available yet</span>'
    c = theme.TIER["good"]["c"] if x >= 0 else theme.TIER["bad"]["c"]
    return f'<span style="color:{c};font-weight:650">{x * 100:+.2f}%</span>'


def _perf_block(label: str, x) -> str:
    return (
        f'<div style="min-width:96px">'
        f'<div style="font-size:11px;color:{theme.MUTED}">{theme._esc(label)}</div>'
        f'<div style="font-size:1.15rem;margin-top:.1rem">{_pct_span(x)}</div>'
        f'</div>'
    )


def _render_version_cards(reg: dict | None) -> None:
    from strategies import config as scfg

    st.markdown(theme.section("The three versions — performance, then regime posture"),
                unsafe_allow_html=True)
    st.caption("Model performance is computed from end-of-day data through the "
               "validated backtest engine (the same code the strategy uses). Today = "
               "the last day's move; Month to date and Year to date baseline to the "
               "prior month-end and prior year-end.")

    score = reg["score"] if reg else None
    gate_map = scfg.REENTRY_STAGE4_SCORE
    plain_regime = (REGIME_PLAIN.get(reg["confirmed_regime"], reg["confirmed_regime"])
                    if reg else None)

    cols = st.columns(3)
    for col, v in zip(cols, BACKTEST_VERSIONS):
        with col:
            try:
                res = _model_result(v)
            except Exception as exc:
                st.error(f"{v}: model could not be computed ({type(exc).__name__}).")
                continue
            daily, mtd, ytd, _, _ = _period_returns(res["nav"])
            bench = res["bench"]
            spy = bench["SPY"] if "SPY" in bench else None
            b6040 = bench["60/40"] if "60/40" in bench else None
            _, spy_mtd, spy_ytd, _, _ = (_period_returns(spy) if spy is not None
                                         else (None, None, None, None, None))
            _, b_mtd, b_ytd, _, _ = (_period_returns(b6040) if b6040 is not None
                                     else (None, None, None, None, None))

            # --- Performance headline (leads the card) ---
            perf = (
                f'<div style="font-size:11.5px;color:{theme.MUTED};font-weight:600;'
                f'margin-bottom:.35rem">MODEL PERFORMANCE</div>'
                f'<div style="display:flex;gap:.9rem;flex-wrap:wrap">'
                + _perf_block("Today", daily)
                + _perf_block("Month to date", mtd)
                + _perf_block("Year to date", ytd)
                + '</div>'
            )

            # --- Compare vs the market ---
            compare = (
                f'<div style="font-size:11.5px;color:{theme.MUTED};margin-top:.55rem">'
                f'vs S&amp;P 500: {_pct_span(spy_mtd)} MTD &middot; '
                f'{_pct_span(spy_ytd)} YTD &nbsp;|&nbsp; '
                f'vs 60/40: {_pct_span(b_mtd)} MTD &middot; {_pct_span(b_ytd)} YTD</div>'
            )

            # --- Regime posture for THIS version ---
            exposure = res.get("exposure")
            exp_txt = (f"Current target stock exposure: {exposure * 100:.0f}% of the "
                       f"portfolio." if exposure is not None else
                       "Target stock exposure: not available.")
            gate = gate_map.get(v)
            if score is not None and gate is not None:
                if score >= gate:
                    gate_txt = (f"Re-enters full risk at score {gate:.0f} — currently "
                                f"{score:.0f}, so already above.")
                else:
                    gate_txt = (f"Re-enters full risk at score {gate:.0f} — currently "
                                f"{score:.0f}, so {gate - score:.0f} points below.")
            else:
                gate_txt = (f"Re-enters full risk at score {gate:.0f} (live score "
                            f"unavailable)." if gate is not None else "")
            regime_line = (f"Shared regime: {plain_regime}. " if plain_regime else "")

            posture = (
                f'<div style="font-size:12px;color:{theme.TEXT};margin-top:.6rem;'
                f'padding-top:.5rem;border-top:1px solid {theme.BORDER}">'
                f'{theme._esc(regime_line + exp_txt)}<br>{theme._esc(gate_txt)}</div>'
            )

            st.markdown(theme.card(v, perf + compare + posture),
                        unsafe_allow_html=True)


# =========================================================================== #
# Collapsed details — the OLD headline metrics, demoted off the card face.     #
# =========================================================================== #
def _fmt_metric(metric: str, raw) -> str:
    if pd.isna(raw):
        return "—"
    if metric in _PERCENT_METRICS:
        return f"{raw * 100:.1f}%"
    return f"{raw:.2f}"


def _metric_tier(metric: str, raw) -> str:
    if pd.isna(raw):
        return "unknown"
    if metric in _RISK_METRICS:
        return "bad"  # drawdown / down-capture are inherently "bad" magnitudes
    return "good" if raw >= 0 else "bad"


def _render_details() -> None:
    st.markdown(theme.section("Full risk & return details (backtest engine)"),
                unsafe_allow_html=True)
    st.caption("The deeper risk metrics for each version, computed from the same "
               "validated engine. Cached for one hour.")

    from src import metrics as mt

    for v in BACKTEST_VERSIONS:
        with st.expander(f"{v} — CAGR, drawdown, Calmar, Sortino, down-capture"):
            try:
                bench = _model_result(v)["bench"]
                table = mt.compute_metrics(bench)
            except Exception as exc:
                st.error(f"Could not compute metrics: {exc}")
                continue
            mcols = st.columns(len(_HEADLINE_METRICS))
            for mc, metric in zip(mcols, _HEADLINE_METRICS):
                raw = table.loc[metric, "strategy"] if metric in table.index else float("nan")
                with mc:
                    st.markdown(
                        theme.status_card(METRIC_LABELS[metric],
                                          _metric_tier(metric, raw),
                                          _fmt_metric(metric, raw)),
                        unsafe_allow_html=True)
            st.caption("Green = favourable. Red = drawdown / down-capture, where a "
                       "deeper (more negative) number is worse.")

    # Keep the existing interactive HTML backtest-report download links.
    st.markdown(theme.section("Full backtest reports (rich, interactive charts)"),
                unsafe_allow_html=True)
    st.caption("Generated by the backtester. Download to open the full interactive report.")
    from datetime import datetime as _dt
    for v in BACKTEST_VERSIONS:
        f = BACKTEST_OUTPUT / f"backtest_report_{v}.html"
        if f.exists():
            mtime = _dt.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            with open(f, "rb") as fh:
                st.download_button(
                    f"Download the {v} report  (built {mtime})", fh.read(),
                    file_name=f.name, mime="text/html", key=f"s0_dl_{v}")
        else:
            st.caption(f"{v}: report not found")


# =========================================================================== #
# Page entry point.                                                           #
# =========================================================================== #
def render_s0_full() -> None:
    """Render the full Strategy 0 page: the real-money gate card, the shared regime
    banner (live score + frozen bands + cushion + trend), the three version cards led
    by Daily / Month-to-date / Year-to-date model performance with regime posture, and
    a collapsed details section. Model-driven and broker-free throughout."""
    st.subheader("Strategy 0 — Adaptive All-Weather Core")
    st.caption("The desk's all-weather core. Everything here is read-only and computed "
               "from the end-of-day model — no broker, no live account.")

    _render_gate_card()
    reg = _render_regime_banner()
    _render_version_cards(reg)
    _render_details()
