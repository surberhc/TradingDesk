"""page_control_plane.py — the desk's Control Plane page for Strategy 0 rebalancing.

The in-app Control Plane (docs/PRODUCTION_REBALANCE_CONTROL_PLANE.md). It surfaces, for
the funded trust account U14438624, three things:
  * a scannable VERDICT (what would trade) + tiles read from the last reviewed preview;
  * an on-demand READ-ONLY rebalance preview, produced by running the hardened paperbot
    executor (s0_live_deploy.py) with NO arguments (its PREVIEW mode: it sizes + prints
    the order list and transmits nothing); and
  * a gated ARM + SEND step that CAN transmit a real rebalance order — but only behind
    the sacred review -> arm -> transmit gate.

TRANSMISSION IS DELIBERATE AND HUMAN-GATED. The Send button is inert until the operator
has (a) built and reviewed a FRESH preview and (b) typed the account id to confirm; AND the
operator must physically arm the port-4003 Gateway by hand in TWS (uncheck Read-Only API)
— an act no software here performs. Send shells out to the UNCHANGED s0_live_deploy
executor, which is itself fail-closed: it refuses to transmit unless the Gateway is
physically armed and its own caps / kill-switch / single-account gates all pass. Nothing
here transmits on its own, on a schedule, or from the AI.

IMPORT DISCIPLINE (mirrors page_s0.py): module-top imports are CHEAP only (stdlib, pandas,
streamlit, theme). Every heavy import — strategy_target, eventlog, investable — is LAZY
(inside the function that needs it), and the executor is invoked as a SUBPROCESS, so
importing this module opens no socket and runs no backtest.

PRESENTATION NOTE (2026-07-31): this file was re-laid-out into a scannable verdict/tiles +
3-column send rail. The gate LOGIC is unchanged — every subprocess call, token construction,
audit call, freshness check, and the confirm/arm/send guard is byte-identical to before; only
how those are DISPLAYED changed. The transmit path lives in _run_execute_and_render(), whose
body is the verbatim old Step-3 handler.
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

# The standalone read-only, zero-transmission 4003 armed-state probe (its own script so no
# broker socket ever opens inside this Streamlit process). Shelled out from Step 2 below.
_ARM_PROBE_SCRIPT = REPO / "dashboard" / "desk" / "gateway_arm_probe.py"
_ARM_PROBE_CWD = REPO / "dashboard" / "desk"
_ARM_PROBE_TIMEOUT_SEC = 45

# --- Reviewed-preview freshness / expiry (decision D, 2026-07-31) -----------------
# The operator reviews a read-only preview (Step 1), physically arms the gateway in TWS,
# then Executes (Step 3). The executor ALWAYS recomputes the order live against current
# cash/quotes at fire time, so what transmits is never the stale preview's numbers. But the
# human REVIEW must still be current: a preview reviewed long ago (the account or prices may
# have moved since) should not be the basis for an arm. So a reviewed preview EXPIRES after
# this window and must be rebuilt (regenerated) before Execute re-enables — the in-app
# enforcement of the propose-and-arm freshness policy. This only ever TIGHTENS the gate; it
# never enables a transmit the existing preview+confirm gate would have blocked.
PREVIEW_FRESHNESS_SECS = 1800.0  # 30 min: a reviewed preview older than this must be rebuilt


def _freshness_of(built_at, now: datetime,
                  window_secs: float = PREVIEW_FRESHNESS_SECS) -> tuple[float | None, bool]:
    """Pure freshness decision — unit-testable without Streamlit. Returns (age_secs,
    is_fresh). A None/unparseable built_at -> (None, False). Fresh iff age <= window (a small
    negative age from clock skew is treated as fresh)."""
    if built_at is None:
        return (None, False)
    try:
        age = (now - built_at).total_seconds()
    except Exception:  # noqa: BLE001 — a bad timestamp is simply "not fresh"
        return (None, False)
    return (age, age <= window_secs)


def _preview_freshness(now: datetime | None = None) -> tuple[float | None, bool]:
    """(age_secs, is_fresh) for the LAST reviewed preview in session state. Reads
    cp_last_preview['built_at'] (a datetime set by _store_last_preview); no preview -> (None,
    False)."""
    last = st.session_state.get("cp_last_preview")
    built_at = last.get("built_at") if isinstance(last, dict) else None
    return _freshness_of(built_at, now or datetime.now())


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
# Target panel — the broker-free Strategy 0 Growth target book.                #
# =========================================================================== #
def _render_target_panel() -> None:
    st.markdown(theme.section("Strategy 0 Growth target (broker-free model)"),
                unsafe_allow_html=True)
    st.caption("What the Strategy 0 Growth deployment target wants to hold right now, as a "
               "share of the portfolio — INCLUDING the standing cash reserve the engine "
               "holds back. Computed read-only through the validated backtest engine "
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

    # The raw model book normalizes to ~100% across RISK holdings with NO cash line, but
    # the deployment engine holds back a standing cash reserve (investable.buffer_pct());
    # see investable.compute_investable / cash_line. Surface that here so the DISPLAYED
    # book matches what actually deploys and sums to exactly 100% (risk lines scaled by
    # (1 - buffer) + a synthetic CASH line at `buffer`). This is READOUT ONLY — it sizes
    # nothing and places nothing. Defensive: any failure falls back to the raw book so a
    # readout helper can never crash the page.
    buffer = None
    display_weights = dict(weights)
    try:
        import investable
        buffer = investable.buffer_pct()
        display_weights = {t: f * (1.0 - buffer) for t, f in weights.items()}
        display_weights["CASH"] = buffer
    except Exception:  # noqa: BLE001 — a readout helper must never crash the page
        buffer = None
        display_weights = dict(weights)

    rows = []
    for ticker, frac in sorted(display_weights.items(), key=lambda kv: kv[1], reverse=True):
        pct_html = (f'<span style="color:{theme.TEXT};font-weight:650">'
                    f'{frac * 100:.2f}%</span>')
        if buffer is not None and ticker == "CASH":
            meta = (f"reserved cash — held to cover monthly fees "
                    f"(standing {buffer * 100:.1f}% reserve)")
        else:
            meta = "target share of the portfolio"
        rows.append(theme.row(ticker, pct_html, meta=meta))
    st.markdown("".join(rows), unsafe_allow_html=True)
    if buffer is not None:
        st.caption(f"Target as of {data.get('as_of', '—')} · prices as of "
                   f"{data.get('price_date', '—')}. Includes the standing "
                   f"{buffer * 100:.1f}% cash reserve (held to pay monthly fees); the book "
                   f"sums to 100%.")
    else:
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


def _store_last_preview(stdout: str) -> None:
    """Bind the arm/execute controls to the LAST reviewed read-only preview. Parses the
    executor's PREVIEW stdout into a compact, plain summary + a wall-clock timestamp and
    stores it in session state under ``cp_last_preview``. Never raises — a parse miss
    simply yields a sparser summary; the executor recomputes authoritatively at fire time
    regardless, so this is a review-binding aid, not the source of truth."""
    try:
        parsed = _parse_preview(stdout or "")
        n_legs = len(parsed.get("legs") or [])
        total_sell = parsed.get("total_sell")
        total_buy = parsed.get("total_buy")
        sells = f"${total_sell:,.2f}" if total_sell is not None else "—"
        buys = f"${total_buy:,.2f}" if total_buy is not None else "—"
        now = datetime.now()
        st.session_state["cp_last_preview"] = {
            "built_at": now,                       # datetime — used for the 30-min age check
            "built_at_str": now.strftime("%H:%M"),
            "n_legs": n_legs,
            "sells": sells,
            "buys": buys,
            "account": parsed.get("account") or PREVIEW_ACCOUNT,
            "net_liq": parsed.get("net_liq"),
            "summary": f"{n_legs} leg(s), sells {sells}, buys {buys}",
        }
    except Exception:  # noqa: BLE001 — binding is best-effort; never break the preview
        pass


def _render_leg_table(legs: list[dict]) -> None:
    """Render the ordered leg list as a clean table: Side / Symbol / Shares / Limit
    price / Notional. SELL rows are tinted the 'bad' (red) tier colour and BUY rows the
    'good' (green) tier colour so the sell-first / then-buy flow reads at a glance. Colour
    is presentation only — the leg data itself is unchanged; any Styler failure falls back
    to the plain table so a cosmetic helper can never break the page."""
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

    def _side_color(val: object) -> str:
        v = str(val).upper()
        if v == "SELL":
            return f"color: {theme.TIER['bad']['c']}; font-weight: 650"
        if v == "BUY":
            return f"color: {theme.TIER['good']['c']}; font-weight: 650"
        return ""

    try:
        styled = df.style.map(_side_color, subset=["Side"])
        st.dataframe(styled, hide_index=True, use_container_width=True)
    except Exception:  # noqa: BLE001 — styling is cosmetic; fall back to the plain table
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


# =========================================================================== #
# Verdict + tiles — the scannable top-of-page summary, read from session.      #
# =========================================================================== #
def _render_verdict_and_tiles() -> None:
    """The at-a-glance top row: a wide VERDICT card + two tiles (account value, last
    checked). Pure display — it only READS st.session_state['cp_last_preview'] (the summary
    _store_last_preview bound) and the freshness helper. It builds nothing and transmits
    nothing."""
    last = st.session_state.get("cp_last_preview")
    age_secs, fresh = _preview_freshness()
    c1, c2, c3 = st.columns([2, 1, 1])

    with c1:
        if not isinstance(last, dict):
            st.markdown(
                theme.status_card(
                    "Rebalance status", "info",
                    "Not checked yet — press Build below",
                    "No read-only preview has been built this session yet. Use Step 1 "
                    "(Review) in the send rail below to see exactly what would trade. "
                    "Nothing has transmitted.",
                ),
                unsafe_allow_html=True,
            )
        else:
            n_legs = last.get("n_legs") or 0
            checked = last.get("built_at_str", "—")
            if n_legs and n_legs > 0:
                headline = f"{n_legs} trade(s) to rebalance"
                base = (f"About {last.get('sells', '—')} to sell then "
                        f"{last.get('buys', '—')} to buy · checked {checked}.")
                if fresh:
                    st.markdown(
                        theme.status_card("Rebalance status", "info", headline, base),
                        unsafe_allow_html=True,
                    )
                else:
                    st.markdown(
                        theme.status_card(
                            "Rebalance status", "warn",
                            f"{headline} — preview expired",
                            base + " This preview has expired; rebuild it before you send.",
                        ),
                        unsafe_allow_html=True,
                    )
            else:
                st.markdown(
                    theme.status_card(
                        "Rebalance status", "good",
                        "In line — nothing to trade",
                        f"As of the {checked} check the account already matches the "
                        f"Strategy 0 Growth target — there is nothing to trade. Nothing "
                        f"has transmitted.",
                    ),
                    unsafe_allow_html=True,
                )

    with c2:
        net_liq = last.get("net_liq") if isinstance(last, dict) else None
        nl_str = (f"${net_liq:,.0f}" if isinstance(net_liq, (int, float)) else "—")
        st.markdown(
            theme.card(
                "Account value",
                f'<span style="color:{theme.TEXT};font-weight:650">{theme._esc(nl_str)}</span>',
                f"Net liquidation value of account {PREVIEW_ACCOUNT} at the last check.",
            ),
            unsafe_allow_html=True,
        )

    with c3:
        if not isinstance(last, dict):
            st.markdown(
                theme.card("Last checked",
                           f'<span style="color:{theme.MUTED}">—</span>',
                           "No preview has been built yet this session."),
                unsafe_allow_html=True,
            )
        elif fresh:
            st.markdown(
                theme.card(
                    "Last checked",
                    f'<span style="color:{theme.TIER["good"]["c"]};font-weight:650">'
                    f'{theme._esc(str(last.get("built_at_str", "—")))} · fresh</span>',
                    f"Still within the {int(PREVIEW_FRESHNESS_SECS // 60)}-minute freshness "
                    f"window — good to arm from.",
                ),
                unsafe_allow_html=True,
            )
        else:
            mins = int((age_secs or 0) // 60)
            st.markdown(
                theme.card(
                    "Last checked",
                    f'<span style="color:{theme.TIER["warn"]["c"]};font-weight:650">'
                    f'{theme._esc(str(last.get("built_at_str", "—")))} · expired</span>',
                    f"About {mins} minute(s) old, over the "
                    f"{int(PREVIEW_FRESHNESS_SECS // 60)}-minute window — rebuild before you "
                    f"send.",
                ),
                unsafe_allow_html=True,
            )


# =========================================================================== #
# Read-only preview run — the byte-identical old Step-1 handler body.          #
# =========================================================================== #
def _run_preview_and_render() -> None:
    """Run the hardened S0 executor in PREVIEW mode (no args) and render the plan. This is
    the byte-identical body of the old Step-1 handler — same existence check, same
    subprocess call, same _render_preview_result / _store_last_preview / _audit_preview — the
    only change is that the surrounding per-step status cards were lifted out into the
    verdict tiles + the Step-1 freshness pill. Transmits nothing."""
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
    _store_last_preview(stdout)  # bind the arm/execute step to THIS reviewed preview
    _audit_preview()  # best-effort durable audit; never breaks the page


# =========================================================================== #
# ARM + SEND — the deliberate human gate on top of the executor's own wall.    #
# The executor (s0_live_deploy.py) is fail-closed: it transmits ONLY when the  #
# 4003 Gateway is physically armed AND its own caps/kill-switch/single-account #
# gates all pass. This UI is the human review -> arm -> transmit gate on top;  #
# it fires no order itself, it shells out to that executor.                    #
# =========================================================================== #
def _arm_execute_audit(category: str, message: str, severity: str) -> None:
    """Best-effort durable audit for an arm/execute action. Lazily imports the event log
    and swallows ALL errors — auditing must never break (or block) the page."""
    try:
        from eventlog import record_event
        record_event(
            ts=datetime.now().isoformat(timespec="seconds"),
            source="Control Plane",
            category=category,
            message=message,
            severity=severity,
        )
    except Exception:  # noqa: BLE001 — audit is best-effort
        pass


def _classify_execute_output(stdout: str, stderr: str) -> str:
    """Classify the executor's real-run output into 'filled' | 'blocked' | 'error'.
    'filled' wins when the two-phase completion line is present; otherwise any block
    marker means nothing transmitted; anything else is an unexpected error."""
    combined = ((stdout or "") + "\n" + (stderr or "")).lower()
    if "two-phase cash-gated deploy complete" in combined:
        return "filled"
    block_markers = ("transmission blocked", "read-only", "not armed", "would transmit")
    if any(mk in combined for mk in block_markers):
        return "blocked"
    return "error"


def _run_arm_probe_and_render() -> None:
    """Run the standalone read-only, zero-transmission 4003 armed-state probe
    (gateway_arm_probe.py) and SHOW armed / not-armed / unreachable. This is the byte-
    identical body of the old _render_arm_probe (only the leading button was lifted into the
    send rail). No socket opens in this Streamlit process; the probe places and transmits
    NOTHING.

    The probe prints exactly one uppercase token (READONLY / ARMED / UNREACHABLE) on its
    LAST stdout line. Any failure is a plain-English 'bad' card — never a crash."""
    if not os.path.exists(VENV_PYTHON) or not _ARM_PROBE_SCRIPT.exists():
        st.markdown(
            theme.status_card(
                "Gateway armed-state check",
                "bad",
                "Could not run the armed-state check",
                "The probe script or its Python could not be found on this machine. "
                "Nothing was transmitted.",
            ),
            unsafe_allow_html=True,
        )
        return

    try:
        with st.spinner("Checking the port-4003 Gateway's armed state (read-only, "
                        "transmits nothing)…"):
            proc = subprocess.run(
                [VENV_PYTHON, str(_ARM_PROBE_SCRIPT)],
                cwd=str(_ARM_PROBE_CWD),
                capture_output=True,
                text=True,
                timeout=_ARM_PROBE_TIMEOUT_SEC,
            )
        stdout, stderr = proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired:
        st.markdown(
            theme.status_card(
                "Gateway armed-state check",
                "bad",
                "Timed out reaching the 4003 Gateway",
                "The armed-state check did not finish in time. The live-trade Gateway "
                "(port 4003) may be down or not logged in. Nothing was transmitted.",
            ),
            unsafe_allow_html=True,
        )
        return
    except Exception as exc:  # noqa: BLE001 — any failure is a plain-English card, never a crash
        st.markdown(
            theme.status_card(
                "Gateway armed-state check",
                "bad",
                "Could not run the armed-state check",
                f"Could not reach the live-trade Gateway (port 4003) to check "
                f"({type(exc).__name__}) — is it up and logged in? Nothing was "
                f"transmitted.",
            ),
            unsafe_allow_html=True,
        )
        return

    # Parse the LAST non-empty stdout line for the token.
    token = ""
    for line in reversed((stdout or "").splitlines()):
        if line.strip():
            token = line.strip().upper()
            break

    if token == "ARMED":
        st.markdown(
            theme.status_card(
                "Gateway armed-state check",
                "warn",
                "Armed — Read-Only API is OFF; the Gateway can transmit",
                "Armed — Read-Only API is OFF; the Gateway can transmit. Only arm it right "
                "before you Execute, and disarm right after.",
            ),
            unsafe_allow_html=True,
        )
    elif token == "READONLY":
        st.markdown(
            theme.status_card(
                "Gateway armed-state check",
                "good",
                "Not armed — Read-Only API is ON; nothing can transmit",
                "Not armed — Read-Only API is ON; nothing can transmit. Uncheck it in TWS "
                "only when you are ready to Execute.",
            ),
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            theme.status_card(
                "Gateway armed-state check",
                "bad",
                "Could not reach the 4003 Gateway to check",
                "Could not reach the 4003 Gateway to check — is it up and logged in? "
                "Nothing was transmitted.",
            ),
            unsafe_allow_html=True,
        )


def _run_execute_and_render(can_press: bool, pressed: bool) -> None:
    """The transmit path — BYTE-IDENTICAL to the old Step-3 handler from the guard onward:
    same `if not can_press or not pressed: return` gate, same executor-existence check, same
    guarded audit, same arm_token/conform_flag construction, same subprocess.run invocation
    of s0_live_deploy with those tokens, same timeouts, same result classification + audits.
    Only the section header / expiry notice / button / checklist that used to precede this
    block were moved out into the send rail; this handler's logic is unchanged."""
    # The pressed-handler is unreachable until BOTH prerequisites hold. (When the button is
    # disabled, `pressed` is already False; this guard is the belt-and-suspenders backstop.)
    if not can_press or not pressed:
        return

    # --- Pressed AND both gates satisfied. This is the ONLY place the arm/conform tokens
    # are ever constructed — never as module constants, never in rendered text. ---
    if not os.path.exists(VENV_PYTHON) or not _DEPLOY_SCRIPT.exists():
        st.markdown(
            theme.status_card(
                "Execute", "bad", "Could not start the executor",
                "The executor or its Python could not be found on this machine. Nothing "
                "was transmitted.",
            ),
            unsafe_allow_html=True,
        )
        return

    # Guarded audit BEFORE running.
    _arm_execute_audit(
        category="control_plane_execute_fired",
        message=("Operator armed and fired the S0 rebalance for account U14438624 "
                 "(typed confirm + reviewed preview). The executor transmits only if the "
                 "4003 Gateway is physically armed."),
        severity="warn",
    )

    # Tokens built ONLY here, inside the guarded, gated handler.
    arm_token = "--arm-i-" + "understand"
    conform_flag = "--" + "conform"
    try:
        with st.spinner("Transmitting the S0 rebalance to the live-trade Gateway "
                        "(port 4003) — this runs the two-phase cash-gated deploy…"):
            proc = subprocess.run(
                [VENV_PYTHON, str(_DEPLOY_SCRIPT), arm_token, conform_flag],
                cwd=str(_DEPLOY_CWD),
                capture_output=True,
                text=True,
                timeout=300,
            )
        stdout, stderr = proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired:
        _arm_execute_audit(
            category="control_plane_execute_result",
            message=("The S0 rebalance execute run timed out before the executor "
                     "reported a result; the Gateway may be down. Transmission state is "
                     "unconfirmed — verify in TWS."),
            severity="bad",
        )
        st.markdown(
            theme.status_card(
                "Execute", "bad", "Timed out before the executor reported back",
                "The execute run did not finish in time. The live-trade Gateway "
                "(port 4003) may be down or not logged in. Check the account and open "
                "orders in TWS to confirm state before trying again.",
            ),
            unsafe_allow_html=True,
        )
        return
    except Exception as exc:  # noqa: BLE001 — any failure is a plain-English card, never a crash
        _arm_execute_audit(
            category="control_plane_execute_result",
            message=(f"The S0 rebalance execute run failed to start or crashed "
                     f"({type(exc).__name__}). Nothing was confirmed transmitted — "
                     f"verify in TWS."),
            severity="bad",
        )
        st.markdown(
            theme.status_card(
                "Execute", "bad", "Couldn't run the executor",
                f"Couldn't run the executor ({type(exc).__name__}) — the live-trade "
                f"Gateway (port 4003) may be down. Nothing was confirmed transmitted; "
                f"check the account in TWS.",
            ),
            unsafe_allow_html=True,
        )
        return

    # Top-line status card from the executor's own output.
    verdict = _classify_execute_output(stdout, stderr)
    if verdict == "filled":
        st.markdown(
            theme.status_card(
                "Execute", "good", "The rebalance was transmitted and filled",
                "The rebalance was transmitted and filled — review the fills below and "
                "DISARM the Gateway (re-check 'Read-Only API' on the port 4003 Gateway in "
                "TWS) now that you are finished.",
            ),
            unsafe_allow_html=True,
        )
        result_msg = ("The S0 rebalance for account U14438624 was transmitted and the "
                      "two-phase cash-gated deploy completed. Review fills and disarm the "
                      "Gateway.")
        result_sev = "good"
    elif verdict == "blocked":
        st.markdown(
            theme.status_card(
                "Execute", "warn", "Nothing was transmitted — the Gateway was not armed",
                "Nothing was transmitted — the Gateway was not armed (or a safety gate "
                "blocked it). Arm the 4003 Gateway in TWS (uncheck 'Read-Only API') and "
                "try again.",
            ),
            unsafe_allow_html=True,
        )
        result_msg = ("The S0 rebalance execute run transmitted NOTHING — the 4003 "
                      "Gateway was not armed or a safety gate blocked it.")
        result_sev = "warn"
    else:
        st.markdown(
            theme.status_card(
                "Execute", "bad", "The executor returned an unexpected result",
                "The executor did not report either a completed deploy or a clean block. "
                "Read the full log below carefully and verify the account and open orders "
                "in TWS before doing anything else.",
            ),
            unsafe_allow_html=True,
        )
        result_msg = ("The S0 rebalance execute run returned an UNEXPECTED result "
                      "(neither completed nor a clean block) — verify in TWS.")
        result_sev = "bad"

    # Reuse the existing preview-result renderer for the leg/summary view + raw log.
    _render_preview_result(stdout, stderr)

    # Guarded result audit.
    _arm_execute_audit(category="control_plane_execute_result",
                       message=result_msg, severity=result_sev)


# =========================================================================== #
# Page entry point — scannable verdict/tiles + 3-column send rail.             #
# =========================================================================== #
def render_control_plane() -> None:
    """Render the Control Plane page: a scannable VERDICT + tiles read from the last
    reviewed preview, a collapsible full target book, and a 3-column send rail
    (Review / Arm / Send). Everything is read-only until the operator deliberately refreshes
    a preview, physically arms the port-4003 Gateway in TWS, types the account id, and
    presses Send — which shells out to the unchanged s0_live_deploy executor. Nothing
    transmits on its own, on a schedule, or from the AI."""
    st.subheader("Strategy 0 — Rebalance")
    st.caption(
        f"What Strategy 0 (Growth) would trade on account {PREVIEW_ACCOUNT} to conform to "
        f"its target. Read-only until you deliberately refresh a preview, arm the gateway by "
        f"hand in TWS, type the account id, and press Send."
    )

    # Reserve the top slots first so a preview built lower in the send rail can fill the
    # verdict tiles + the plan detail up here in the same run (Streamlit containers render in
    # place but can be written to later in the code).
    verdict_slot = st.container()
    plan_slot = st.container()

    # The full broker-free target book — collapsed so it no longer dominates the page.
    with st.expander("Show the full target book"):
        _render_target_panel()

    # --- SEND RAIL: three deliberate steps, side by side ------------------------- #
    st.markdown(theme.section("Send the rebalance — three deliberate steps"),
                unsafe_allow_html=True)
    cols = st.columns(3)

    # Step 1 · Review — the build button. Its handler runs BELOW (into plan_slot) before
    # freshness is computed, mirroring the original order (Step 1 ran before _preview_freshness).
    with cols[0]:
        st.markdown(theme.section("Step 1 · Review"), unsafe_allow_html=True)
        st.caption("Build a read-only preview of exactly what would trade. Reads the "
                   "gateway; transmits nothing.")
        pressed_build = st.button("Build read-only preview (reads the live-trade gateway)")

    if pressed_build:
        with plan_slot:
            _run_preview_and_render()

    # Freshness now reflects any just-built preview.
    preview_age_secs, preview_fresh = _preview_freshness()
    has_preview = isinstance(st.session_state.get("cp_last_preview"), dict)
    with cols[0]:
        if not has_preview:
            st.markdown(theme.pill("No preview yet — press Build", "unknown"),
                        unsafe_allow_html=True)
        elif preview_fresh:
            st.markdown(
                theme.pill(f"Preview fresh (under {int(PREVIEW_FRESHNESS_SECS // 60)} min)",
                           "good"),
                unsafe_allow_html=True)
        else:
            _mins = int((preview_age_secs or 0) // 60)
            st.markdown(
                theme.pill(f"Preview expired ({_mins} min old) — rebuild it", "warn"),
                unsafe_allow_html=True)

    # Step 2 · Arm — the armed-state check button + the typed-confirm gate. The physical arm
    # is a human act in TWS; the button only CHECKS state (read-only, transmits nothing).
    with cols[1]:
        st.markdown(theme.section("Step 2 · Arm"), unsafe_allow_html=True)
        st.caption("Uncheck 'Read-Only API' on the port-4003 Gateway in TWS by hand, then "
                   "type the account id to confirm you reviewed the preview and armed it.")
        pressed_arm = st.button("Check whether the 4003 Gateway is armed",
                                key="cp_arm_probe_btn")
        confirm_val = st.text_input(
            f"Type the account id {PREVIEW_ACCOUNT} to confirm",
            value="", key="cp_execute_confirm",
            placeholder=f"type {PREVIEW_ACCOUNT} here",
        )
        confirmed = confirm_val.strip() == PREVIEW_ACCOUNT
        if confirmed:
            st.markdown(theme.pill("Account id confirmed", "good"), unsafe_allow_html=True)
        else:
            st.markdown(theme.pill(f"Type {PREVIEW_ACCOUNT} to confirm", "warn"),
                        unsafe_allow_html=True)

    # Armed-state check result renders full width below the rail.
    arm_slot = st.container()
    if pressed_arm:
        with arm_slot:
            _run_arm_probe_and_render()

    # Step 3 · Send — the Execute button. Same key, same disabled condition
    # (preview_fresh AND confirmed), same guarded handler; only the label reads "Send".
    with cols[2]:
        st.markdown(theme.section("Step 3 · Send"), unsafe_allow_html=True)
        can_press = preview_fresh and confirmed
        pressed = st.button(
            "Send order to IBKR",
            key="cp_execute_btn",
            disabled=not can_press,
            use_container_width=True,
        )
        if preview_fresh:
            step1_mark = "✓ fresh preview reviewed"
        elif preview_age_secs is not None:
            step1_mark = (f"• expired — rebuild the preview "
                          f"({int(preview_age_secs // 60)} min old)")
        else:
            step1_mark = "• not yet — build the preview"
        step2_mark = ("✓ account id typed" if confirmed
                      else f"• not yet — type {PREVIEW_ACCOUNT}")
        st.markdown(
            f"- {step1_mark}\n"
            f"- {step2_mark}\n"
            f"- Even with both ✓, nothing sends unless the port-4003 Gateway is physically "
            f"armed in TWS ('Read-Only API' unchecked) — the executor measures it and "
            f"refuses otherwise."
        )

    # The transmit handler renders full width below the rail. Its guard/gate/token/subprocess
    # logic is byte-identical to the old Step-3 handler; it returns immediately unless BOTH
    # gates hold and the button was pressed.
    send_slot = st.container()
    with send_slot:
        _run_execute_and_render(can_press, pressed)

    # Now fill the reserved top slots from the (possibly just-updated) session state.
    with verdict_slot:
        _render_verdict_and_tiles()

    # --- Safety line + the full gate prose (moved here from the old top gate card). ------ #
    st.caption(
        "Nothing sends until you refresh the preview, arm the gateway by hand in TWS "
        "(uncheck 'Read-Only API'), type the account id, and press Send."
    )
    with st.expander("How the safety gate works"):
        st.markdown(
            theme.status_card(
                "Real-money transmission",
                "warn",  # amber: capable, but only behind the deliberate human gate
                "Possible here — only behind the review -> arm -> transmit gate",
                "This page CAN transmit a real Strategy 0 rebalance order — but ONLY when you "
                "review the preview, physically arm the port-4003 Gateway by hand in TWS "
                "(uncheck Read-Only API), type the account id to confirm, and press Execute. It "
                "never transmits on its own, on a schedule, or from the AI, and the executor "
                "independently refuses unless the Gateway is physically armed. Until you do all "
                "of that, nothing is placed, armed, or sent.",
            ),
            unsafe_allow_html=True,
        )
        st.markdown(
            theme.status_card(
                "You arm the Gateway by hand in TWS — the check is a convenience",
                "warn",
                "Uncheck 'Read-Only API' on the port 4003 Gateway in TWS before you Execute",
                "The physical arm is a human act: before you press Send, make sure YOU have "
                "unchecked 'Read-Only API' (Configure > Settings > API > Settings) on the "
                "port 4003 live-trade Gateway in TWS. If it is still checked (Read-Only ON), "
                "the Send run transmits NOTHING and reports that it was blocked — the "
                "executor measures the Gateway itself and refuses. When you are finished, "
                "re-check that box to disarm. The 'Check whether the 4003 Gateway is armed' "
                "button is read-only (it transmits nothing) — it does not arm anything.",
            ),
            unsafe_allow_html=True,
        )
