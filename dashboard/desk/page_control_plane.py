"""page_control_plane.py — the desk's Control Plane page. READ-ONLY PREVIEW (Phase 1).

This is the FIRST, deliberately-minimal slice of the in-app Control Plane
(docs/PRODUCTION_REBALANCE_CONTROL_PLANE.md, Phase 1). It shows what Strategy 0
(Growth tier) would trade on the funded trust account U14438624 to conform to its
target — computed READ-ONLY. It places NOTHING, arms NOTHING, and transmits NOTHING.

There is NO arm control and NO execute button here, on purpose. A real order will
only ever transmit later, behind the sacred, human-armed review -> arm -> transmit
gate — a separate, deliberately-later step that this page does not touch.

Two read-only reads live here:
  * the broker-FREE Strategy 0 Growth target (via strategy_target.current_target),
    computed through the validated backtest engine — needs no gateway; and
  * on an explicit button press, a preview of the actual rebalance plan, produced by
    running the hardened paperbot executor (s0_live_deploy.py) with NO arguments —
    which is the executor's PREVIEW mode: it sizes + prints the order list and
    transmits nothing. We NEVER pass its arm/conform tokens from this page.

IMPORT DISCIPLINE (mirrors page_s0.py): module-top imports are CHEAP only (stdlib,
pandas, streamlit, theme). Every heavy import — strategy_target, eventlog — is LAZY
(inside the function that needs it), and the executor is invoked as a SUBPROCESS, so
importing this module opens no socket and runs no backtest.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

import theme

# --- Make the existing packages importable (reuse, don't rebuild) --------------
# Same sys.path bootstrap desk_app.py / page_s0.py use. This module lives at
# dashboard/desk/page_control_plane.py, so the repo root is parents[2].
REPO = Path(__file__).resolve().parents[2]
for _sub in ("paperbot", "backtester", "connections", "strategies", "dailyreport",
             "livebot"):
    _p = REPO / _sub
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
_conn = REPO / "connections"
if str(_conn) not in sys.path:
    sys.path.insert(0, str(_conn))

# The single account this page previews — the funded trust account the S0 deploy
# executor is itself pinned to (s0_live_deploy.ALLOWED_ACCOUNT). Display-only here.
PREVIEW_ACCOUNT = "U14438624"

# The read-only executor invoked (as a SUBPROCESS, no args = preview) for the plan
# preview, and the venv python that must run it.
VENV_PYTHON = r"C:\TradingDesk-Local\venv\Scripts\python.exe"
_DEPLOY_SCRIPT = REPO / "paperbot" / "s0_live_deploy.py"
_DEPLOY_CWD = REPO / "paperbot"
_PREVIEW_TIMEOUT_SEC = 150


# =========================================================================== #
# Broker-free Strategy 0 Growth target — cached 1h (validated engine).         #
# =========================================================================== #
@st.cache_data(ttl=3600,
               show_spinner="Running the Strategy 0 model (validated engine, cached 1 hour)…")
def _growth_target() -> dict:
    """Run the shared strategy brain for the GROWTH tier and return its target as
    plain data. Lazily imports strategy_target; broker-free (no gateway, no order).
    Returns {"weights": {ticker: fraction}, "as_of": iso, "price_date": iso}."""
    import strategy_target
    tgt = strategy_target.current_target(version="Growth")
    weights = {str(k): float(v) for k, v in tgt.weights.items()}
    return {
        "weights": weights,
        "as_of": tgt.as_of.date().isoformat(),
        "price_date": tgt.price_date.date().isoformat(),
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
            "This page only previews and audits — it connects to no broker to place "
            "anything, and it holds no arm control and no execute button. A real order "
            "only ever transmits behind the sacred review -> arm -> transmit wall: a "
            "human reviews the plan, arms the Gateway by hand, and makes an explicit, "
            "gated transmit decision. Those arm and execute controls are a separate, "
            "later, human-gated step that this first version does not include.",
        ),
        unsafe_allow_html=True,
    )


# =========================================================================== #
# Target panel — the broker-free Strategy 0 Growth target book.                #
# =========================================================================== #
def _render_target_panel() -> None:
    st.markdown(theme.section("Strategy 0 Growth target (broker-free model)"),
                unsafe_allow_html=True)
    st.caption("What the Strategy 0 Growth model wants to hold right now, as a share of "
               "the portfolio. Computed read-only through the validated backtest engine "
               "(the same code the strategy uses) — no broker, no live account.")
    try:
        data = _growth_target()
    except Exception as exc:  # noqa: BLE001 — a model failure must not crash the page
        st.markdown(
            theme.status_card(
                "Strategy 0 Growth target",
                "warn",
                "Could not compute the target right now",
                f"The validated model did not return a target this time "
                f"({type(exc).__name__}). Nothing on this page connects to a broker, so "
                f"nothing about trading changed. Try again shortly.",
            ),
            unsafe_allow_html=True,
        )
        return

    weights = data.get("weights", {})
    if not weights:
        st.info("The model returned no target holdings this time. Nothing was transmitted.")
        return

    rows = []
    for ticker, frac in sorted(weights.items(), key=lambda kv: kv[1], reverse=True):
        pct_html = (f'<span style="color:{theme.TEXT};font-weight:650">'
                    f'{frac * 100:.2f}%</span>')
        rows.append(theme.row(ticker, pct_html, meta="target share of the portfolio"))
    st.markdown("".join(rows), unsafe_allow_html=True)
    st.caption(f"Target as of {data.get('as_of', '—')} · prices as of "
               f"{data.get('price_date', '—')}. Weights sum to roughly 100%.")


# =========================================================================== #
# Read-only plan preview — parse the executor's PREVIEW stdout, defensively.   #
# =========================================================================== #
# [4] line:  "    account=U14438624   NetLiq=123,456.78   open_positions=5"
_RE_ACCOUNT = re.compile(
    r"account=(\S+)\s+NetLiq=([\d,\.]+)\s+open_positions=(\d+)")
# [7] leg:   "    SELL SPY    x100      LIMIT ~    500.00  notional ~   50,000.00  [plan]  -> target ~0.00%"
_RE_LEG = re.compile(
    r"^\s*(SELL|BUY)\s+(\S+)\s+x(\d+)\s+LIMIT ~\s*([\d,\.]+)\s+"
    r"notional ~\s*([\d,\.]+)\s+\[(\w+)\]")
# [7] "no legs" case.
_RE_NO_LEGS = re.compile(r"no legs\b.*(already conforms|nothing to trade)", re.IGNORECASE)
# [9] the final preview-only confirmation.
_RE_BLOCKED = re.compile(r"TRANSMISSION BLOCKED", re.IGNORECASE)


def _fmt_num(raw: str) -> float | None:
    """'123,456.78' -> 123456.78, or None if unparseable."""
    try:
        return float(raw.replace(",", ""))
    except (TypeError, ValueError, AttributeError):
        return None


def _parse_preview(stdout: str) -> dict:
    """Best-effort structured view of the executor's PREVIEW stdout. NEVER raises —
    every field is optional and a caller falls back to the raw log if parsing missed
    anything. Returns keys: account, net_liq, open_positions, legs (list of dicts),
    total_sell, total_buy, already_conforms (bool), transmission_blocked (bool)."""
    out: dict = {
        "account": None, "net_liq": None, "open_positions": None,
        "legs": [], "total_sell": None, "total_buy": None,
        "already_conforms": False, "transmission_blocked": False,
    }
    try:
        m = _RE_ACCOUNT.search(stdout)
        if m:
            out["account"] = m.group(1)
            out["net_liq"] = _fmt_num(m.group(2))
            try:
                out["open_positions"] = int(m.group(3))
            except (TypeError, ValueError):
                out["open_positions"] = None

        legs = []
        for line in stdout.splitlines():
            lm = _RE_LEG.match(line)
            if not lm:
                continue
            legs.append({
                "side": lm.group(1),
                "symbol": lm.group(2),
                "qty": lm.group(3),
                "limit": _fmt_num(lm.group(4)),
                "notional": _fmt_num(lm.group(5)),
                "source": lm.group(6),
            })
        out["legs"] = legs
        out["total_sell"] = sum(l["notional"] for l in legs
                                if l["side"] == "SELL" and l["notional"] is not None)
        out["total_buy"] = sum(l["notional"] for l in legs
                               if l["side"] == "BUY" and l["notional"] is not None)

        if not legs and _RE_NO_LEGS.search(stdout):
            out["already_conforms"] = True
        out["transmission_blocked"] = bool(_RE_BLOCKED.search(stdout))
    except Exception:  # noqa: BLE001 — parsing is best-effort; raw log is source of truth
        pass
    return out


def _audit_preview() -> None:
    """Best-effort durable audit that a read-only preview was built. Lazily imports the
    event log; swallows ALL errors — logging must never break the page."""
    try:
        from eventlog import record_event
        record_event(
            ts=datetime.now().isoformat(timespec="seconds"),
            source="Control Plane",
            category="control_plane_preview",
            message=("Built a read-only Strategy 0 rebalance preview for account "
                     "U14438624 — nothing was transmitted."),
            severity="info",
        )
    except Exception:  # noqa: BLE001 — audit is best-effort
        pass


def _render_leg_table(legs: list[dict]) -> None:
    """Render the ordered leg list as a clean table: Side / Symbol / Shares / Limit
    price / Notional."""
    df = pd.DataFrame([
        {
            "Side": l["side"],
            "Symbol": l["symbol"],
            "Shares": l["qty"],
            "Limit price": (f"${l['limit']:,.2f}" if l["limit"] is not None else "—"),
            "Notional": (f"${l['notional']:,.2f}" if l["notional"] is not None else "—"),
        }
        for l in legs
    ])
    st.dataframe(df, hide_index=True, use_container_width=True)


def _render_preview_result(stdout: str, stderr: str) -> None:
    """Turn the executor's PREVIEW output into a plain-English summary + leg table, then
    ALWAYS show the full raw log in an expander (the raw text is the source of truth)."""
    parsed = _parse_preview(stdout)

    # Account context line (from [4]).
    if parsed["account"]:
        nl = (f"${parsed['net_liq']:,.2f}" if parsed["net_liq"] is not None
              else "not readable")
        op = (parsed["open_positions"] if parsed["open_positions"] is not None
              else "not readable")
        st.markdown(
            theme.card(
                "Account read (read-only)",
                f"Account {theme._esc(str(parsed['account']))} · net liquidation value "
                f"{theme._esc(nl)} · {theme._esc(str(op))} open positions.",
            ),
            unsafe_allow_html=True,
        )

    # The plan itself.
    if parsed["already_conforms"]:
        st.markdown(
            theme.status_card(
                "Rebalance plan",
                "good",
                "Nothing to trade",
                "The account already conforms to the Strategy 0 Growth target — there is "
                "nothing to trade. Nothing was transmitted.",
            ),
            unsafe_allow_html=True,
        )
    elif parsed["legs"]:
        n_sell = sum(1 for l in parsed["legs"] if l["side"] == "SELL")
        n_buy = sum(1 for l in parsed["legs"] if l["side"] == "BUY")
        ts = (f"${parsed['total_sell']:,.2f}" if parsed["total_sell"] is not None
              else "—")
        tb = (f"${parsed['total_buy']:,.2f}" if parsed["total_buy"] is not None else "—")
        st.markdown(
            theme.card(
                "Rebalance plan (read-only preview)",
                f"{len(parsed['legs'])} leg(s) would be traded — {n_sell} sell(s) then "
                f"{n_buy} buy(s), sells first to raise cash. Total sells {ts} · total "
                f"buys {tb} (buys are re-sized to realized cash at real transmit time).",
            ),
            unsafe_allow_html=True,
        )
        _render_leg_table(parsed["legs"])
    else:
        # Parsing found no legs and no explicit "already conforms" marker — fall back.
        st.warning(
            "The structured plan view was unavailable (the preview output did not match "
            "the expected format). See the full preview log below for exactly what the "
            "executor reported. Nothing was transmitted."
        )

    # The final block confirmation (from [9]).
    if parsed["transmission_blocked"]:
        st.markdown(
            theme.status_card(
                "Transmission",
                "info",
                "Read-only preview — nothing transmitted",
                "The executor confirmed this was a preview only: transmission was blocked "
                "and nothing was placed, armed, or sent.",
            ),
            unsafe_allow_html=True,
        )

    # ALWAYS show the raw executor output — the source of truth if parsing missed anything.
    with st.expander("Show the full preview log"):
        st.code((stdout or "") + (("\n" + stderr) if stderr else ""), language=None)


def _render_plan_preview() -> None:
    st.markdown(theme.section("Read-only rebalance plan preview"),
                unsafe_allow_html=True)
    st.caption(
        f"Runs the hardened Strategy 0 executor in its PREVIEW mode (no arguments) to "
        f"read account {PREVIEW_ACCOUNT} on the live-trade Gateway and size the exact "
        f"rebalance it would trade. This reads the Gateway but transmits NOTHING — no "
        f"arm token or conform flag is ever passed from this page."
    )

    if not st.button("Build read-only preview (reads the live-trade gateway)"):
        st.caption("The preview runs only when you press the button above.")
        return

    if not os.path.exists(VENV_PYTHON) or not _DEPLOY_SCRIPT.exists():
        st.markdown(
            theme.status_card(
                "Read-only preview",
                "bad",
                "Could not start the preview",
                "The executor or its Python could not be found on this machine. Nothing "
                "was transmitted.",
            ),
            unsafe_allow_html=True,
        )
        return

    try:
        with st.spinner("Building the read-only preview (reading the live-trade "
                        "gateway on port 4003)…"):
            proc = subprocess.run(
                [VENV_PYTHON, str(_DEPLOY_SCRIPT)],
                cwd=str(_DEPLOY_CWD),
                capture_output=True,
                text=True,
                timeout=_PREVIEW_TIMEOUT_SEC,
            )
        stdout, stderr = proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired:
        st.markdown(
            theme.status_card(
                "Read-only preview",
                "bad",
                "Timed out reaching the live-trade gateway",
                "The preview did not finish in time. The live-trade Gateway (port 4003) "
                "may be down or not logged in. Nothing was transmitted.",
            ),
            unsafe_allow_html=True,
        )
        return
    except Exception as exc:  # noqa: BLE001 — any failure is a plain-English card, never a crash
        st.markdown(
            theme.status_card(
                "Read-only preview",
                "bad",
                "Couldn't reach the live-trade gateway",
                f"Couldn't reach the live-trade gateway (port 4003) — is it up and logged "
                f"in? ({type(exc).__name__}). Nothing was transmitted.",
            ),
            unsafe_allow_html=True,
        )
        return

    _render_preview_result(stdout, stderr)
    _audit_preview()  # best-effort durable audit; never breaks the page


# =========================================================================== #
# Page entry point.                                                           #
# =========================================================================== #
def render_control_plane() -> None:
    """Render the Control Plane page (Phase 1, read-only): the real-money gate card, the
    broker-free Strategy 0 Growth target, and an on-demand read-only rebalance plan
    preview. Places, arms, and transmits NOTHING; holds no arm or execute control."""
    st.subheader("Control Plane — Strategy 0 rebalance (read-only preview)")
    st.caption(
        f"This page shows what Strategy 0 (Growth) would trade on account "
        f"{PREVIEW_ACCOUNT} to conform to its target, computed read-only. In this first "
        f"version it places, arms, and transmits NOTHING. A real order will only ever "
        f"transmit later, behind the explicit, human-armed review -> arm -> transmit gate."
    )

    _render_gate_card()
    _render_target_panel()
    _render_plan_preview()
