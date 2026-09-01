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

import deskproc  # one place that starts a process with no flashing console window
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

# --- BATCH REBALANCE (multi-account, roster-scoped) ------------------------------
# The transmit-capable multi-account executor, invoked as a SUBPROCESS (no broker socket ever
# opens in this Streamlit process — same discipline as the single-account lane). No args =
# PREVIEW (sizes every roster account + prints margin pre-flight, transmits nothing); the arm
# token runs the per-account two-phase cash-gated rebalance behind the same fail-closed gate.
_BATCH_SCRIPT = REPO / "paperbot" / "batch_rebalance_execute.py"
_BATCH_CWD = REPO / "paperbot"
_BATCH_PREVIEW_TIMEOUT_SEC = 300     # reads + sizes EVERY roster account
_BATCH_EXECUTE_TIMEOUT_SEC = 600     # per-account two-phase transmit across the roster
# The deliberate typed confirmation for the batch send (the single-account rail types the
# account id; the batch spans the whole roster, so it types this fixed phrase instead).
#
# SCOPE-AWARE ON PURPOSE. "REBALANCE ALL" is the honest phrase for a whole-book run and an
# actively MISLEADING one for a run narrowed to a few models — the operator would be typing
# "ALL" while sending a subset, which is exactly the sort of mismatch between what the screen
# says and what the machine does that this rail exists to prevent. A scoped run types
# "REBALANCE SELECTED" instead.
BATCH_CONFIRM_PHRASE = "REBALANCE ALL"
BATCH_CONFIRM_PHRASE_SCOPED = "REBALANCE SELECTED"


def _scope_key(models) -> list[str]:
    """The NORMALISED model scope: stripped, de-duped, sorted, empties dropped. PURE.

    One definition, used everywhere a scope is compared, displayed, or handed to the executor,
    so "Growth (Custom), Balanced (Custom)" and "Balanced (Custom),Growth (Custom)" are the
    SAME scope and never read as a change of plan. An empty/None selection normalises to ``[]``
    — the whole book."""
    return sorted({str(m).strip() for m in (models or ()) if str(m).strip()})


def _batch_confirm_phrase(models=None) -> str:
    """The exact phrase the operator must type for THIS run. PURE. Whole book ->
    'REBALANCE ALL'; any model scope -> 'REBALANCE SELECTED'. Every display of the phrase and
    the comparison against what was typed both read this, so the text on screen and the
    required input can never disagree."""
    return BATCH_CONFIRM_PHRASE_SCOPED if _scope_key(models) else BATCH_CONFIRM_PHRASE


def _batch_models_flag(models=None) -> list[str]:
    """The extra argv for the batch executor for this scope. PURE. ``[]`` when nothing is
    selected — the flag is OMITTED entirely, so an unscoped run is the byte-for-byte
    pre-existing whole-book command line."""
    scope = _scope_key(models)
    return [f"--models={','.join(scope)}"] if scope else []


def _batch_scope_mismatch(selected, previewed) -> str | None:
    """The SAFETY PROPERTY, as one pure decision: None when the currently-selected model scope
    is the same scope the stored batch preview was BUILT with, otherwise the plain-English
    reason the arm gate refuses.

    Why this gate exists: the preview and the send are two separate button presses, and the
    scope selector sits between them. Without this, an operator could preview 14 accounts on
    three models, then widen the selector to the whole book and press Send — arming a 185-
    account run off a 14-account review. A preview may only ever arm the exact scope it
    reviewed; changing the scope means building the preview again."""
    sel = _scope_key(selected)
    prev = _scope_key(previewed)
    if sel == prev:
        return None
    _name = lambda s: ", ".join(s) if s else "the whole book"  # noqa: E731
    return (
        f"The model scope changed after this preview was built. The preview covers "
        f"{_name(prev)}; the selector now says {_name(sel)}. Nothing can be sent from a "
        f"preview of a different set of accounts — build the batch preview again for "
        f"{_name(sel)}, or put the selector back to {_name(prev)}."
    )


def _batch_model_choices() -> list[str]:
    """Every model label present in the blessed roster, for the scope selector's options.

    Read ONCE from the CRM (the same read-only role and the same advisor book the executor
    itself scopes to, so the offered choices are exactly the choices that can select
    accounts) and CACHED in session state, because Streamlit reruns this page on every
    keystroke in the confirm box. NEVER raises into the page: if the CRM is not configured or
    is unreachable, the choice list is empty, which the selector renders as "no scope
    available" — i.e. the whole book, the pre-existing behaviour."""
    cached = st.session_state.get("cp_batch_model_choices")
    if isinstance(cached, list):
        return cached
    choices: list[str] = []
    try:
        import roster
        scan = roster.crm_enrolled_roster_scan()
        choices = [str(m) for m in (scan.get("models") or [])]
    except Exception:  # noqa: BLE001 — a scope selector must never break the page
        choices = []
    st.session_state["cp_batch_model_choices"] = choices
    return choices

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
# FAIL LOUD, NOT SILENT. _RE_LEG used to be the only test of "is this a leg line?", so a leg
# the pattern could not read simply VANISHED from the table with no trace — the operator saw
# a plan that looked complete and was not. This deliberately-loose marker asks the weaker
# question "does this line LOOK like an order leg?"; anything that looks like one but does
# not parse is counted and surfaced as a plain-English warning instead of being dropped.
_RE_LEG_MARKER = re.compile(r"^\s*(?:SELL|BUY)\s+\S+\s+.*LIMIT ~")


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
    total_sell, total_buy, already_conforms (bool), transmission_blocked (bool),
    unreadable_leg_lines (list of raw lines that looked like order legs but could not be
    read — surfaced to the operator, never silently dropped), parse_error (str or None)."""
    out: dict = {
        "account": None, "net_liq": None, "open_positions": None,
        "legs": [], "total_sell": None, "total_buy": None,
        "already_conforms": False, "transmission_blocked": False,
        "unreadable_leg_lines": [], "parse_error": None,
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
                # A line that LOOKS like an order leg but does not parse is recorded, not
                # skipped — an order that quietly does not appear is the failure this rail
                # exists to prevent.
                if _RE_LEG_MARKER.match(line):
                    out["unreadable_leg_lines"].append(line.strip())
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
    except Exception as exc:  # noqa: BLE001 — best-effort; raw log is the source of truth
        # Record it. A parse that died half way through leaves a SHORT leg list, which is
        # indistinguishable from a small plan unless we say so out loud.
        out["parse_error"] = f"{type(exc).__name__}: {exc}"
    return out


def _preview_parse_warning(parsed: dict) -> str | None:
    """Plain-English warning about anything the preview parser could NOT read, or None when
    it read everything. Pure (no Streamlit) so it can be tested directly. Spelled out in full
    words — this is the sentence that stops an operator trusting an incomplete table."""
    if not isinstance(parsed, dict):
        return None
    bad = list(parsed.get("unreadable_leg_lines") or [])
    err = parsed.get("parse_error")
    if not bad and not err:
        return None
    parts: list[str] = []
    if bad:
        n = len(bad)
        parts.append(
            f"{n} order line{'s' if n != 1 else ''} could not be read from the preview "
            f"output and {'are' if n != 1 else 'is'} missing from the table above."
        )
    if err:
        parts.append(
            "Reading the preview output stopped early because of an unexpected problem "
            f"({err}), so the table above may be incomplete."
        )
    parts.append(
        "Do not treat the table above as the full plan. Read the full preview log below, "
        "which is the source of truth. Nothing was transmitted."
    )
    return " ".join(parts)


# =========================================================================== #
# DID THE PREVIEW ACTUALLY RUN? — the positive-evidence test the gate needs.   #
# =========================================================================== #
# THE DEFECT THIS CLOSES. Nothing on this page ever looked at the preview subprocess's
# RETURN CODE. A preview whose executor crashed printed nothing, parsed to zero legs, and was
# stored with a fresh wall-clock timestamp — indistinguishable from a healthy preview of an
# account that simply has nothing to trade. So the top-of-page verdict rendered the GREEN
# "In line — nothing to trade" card (its branch was a bare `else` on "no legs", consulting no
# error state at all), and the arm gate — a freshness check AND a typed confirm, where
# freshness was only an age check on that unconditionally-stored timestamp — was satisfied.
# The operator could arm and send off a check that never read the account.
#
# THE RULE NOW. A preview is trusted only on POSITIVE evidence: the program exited cleanly
# AND printed either an order list or its own "already conforms" confirmation. Anything short
# of that is a FAILURE, and the failure is stored in session state next to `built_at` (see
# _store_last_preview) so it survives every rerun — the operator cannot click once more and
# be shown only the green card. This only ever TIGHTENS the gate; it can never enable a
# transmit the old preview+confirm gate would have blocked.
_UNRECORDED_PREVIEW_FAILURE = (
    "The last check on this rail was not recorded as a completed read of the account, so "
    "nothing may be sent from it. Build the preview again."
)


def _preview_failure(parsed: dict, returncode: int | None) -> str | None:
    """Plain-English reason this preview must NOT be armed from, or reported as 'in line',
    or None when it ran cleanly and produced a readable plan. Pure (no Streamlit) so it can
    be tested directly. Spelled out in full words — this is the sentence that stops an
    operator sending against a check that never happened."""
    rc = None
    if returncode is not None:
        try:
            rc = int(returncode)
        except (TypeError, ValueError):
            rc = None
    if rc is not None and rc != 0:
        return (
            f"The preview program stopped with an error instead of finishing normally "
            f"(exit code {rc}). Nothing on this page can tell you what the account holds or "
            f"what would trade. Read the full preview log below for what it managed to "
            f"report, then build the preview again."
        )
    if not isinstance(parsed, dict):
        return _UNRECORDED_PREVIEW_FAILURE
    err = parsed.get("parse_error")
    if err:
        return (
            "Reading the preview output stopped early because of an unexpected problem "
            f"({err}), so the plan shown may be incomplete. Read the full preview log below, "
            "which is the source of truth, and build the preview again."
        )
    if parsed.get("legs"):
        return None
    if parsed.get("already_conforms"):
        return None
    return (
        "The preview produced no readable plan: it listed no orders, and it did not confirm "
        "that the account already matches its target. That is not the same as nothing to "
        "trade — as far as this page can tell, the account was never read. Read the full "
        "preview log below and build the preview again."
    )


def _stored_preview_failure(key: str) -> str | None:
    """The stored failure note for the last preview on one rail — ``cp_last_preview`` (single
    account) or ``cp_batch_last_preview`` (batch) — or None when the stored preview ran
    cleanly. FAIL-CLOSED: a stored preview carrying no explicit success marker counts as
    failed. Returns None when there is NO stored preview at all, because 'not checked yet' is
    its own state, already blocked by the no-preview path, and must not be reported as a
    failed check."""
    last = st.session_state.get(key)
    if not isinstance(last, dict):
        return None
    note = last.get("failure")
    if note:
        return str(note)
    if not last.get("ok"):
        return _UNRECORDED_PREVIEW_FAILURE
    return None


def _preview_is_armable(is_fresh: bool, failure: str | None) -> bool:
    """The preview half of the arm gate, as one pure decision both rails use: a preview may be
    armed from ONLY when it is inside the freshness window AND it was recorded as a completed
    read. Was `preview_fresh` alone, which a crashed run satisfied because the timestamp was
    stored unconditionally."""
    return bool(is_fresh) and failure is None


def _store_failed_preview(key: str, note: str) -> None:
    """Record that a preview ATTEMPT did not produce a reviewable plan, under the SAME
    session key the arm gate reads. Overwriting is the whole point: a failed attempt must
    RETIRE the previous preview rather than leave an older, still-fresh one armable behind a
    failure card. Never raises."""
    try:
        now = datetime.now()
        st.session_state[key] = {
            "built_at": now,
            "built_at_str": now.strftime("%H:%M"),
            "n_legs": 0, "sells": "—", "buys": "—",
            "account": PREVIEW_ACCOUNT, "net_liq": None,
            "already_conforms": False,
            "n_out_of_spec": None, "n_roster": None, "total_legs": None,
            "returncode": None,
            "ok": False,
            "failure": note,
            "summary": "the last check did not complete",
        }
    except Exception:  # noqa: BLE001 — recording a failure must never break the page
        pass


def _audit_preview(failure: str | None = None) -> None:
    """Best-effort durable audit that a read-only preview was ATTEMPTED. Lazily imports the
    event log; swallows ALL errors — logging must never break the page. A run that did not
    produce a reviewable plan is logged as exactly that, not as a built preview: the audit
    trail must not claim a read of the account that did not happen."""
    try:
        from eventlog import record_event
        if failure:
            record_event(
                ts=datetime.now().isoformat(timespec="seconds"),
                source="Control Plane",
                category="control_plane_preview",
                message=("A read-only Strategy 0 rebalance preview for account U14438624 "
                         f"did NOT complete: {failure} Nothing was transmitted, and nothing "
                         "may be sent from it."),
                severity="warn",
            )
            return
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


def _store_last_preview(stdout: str, returncode: int | None = None) -> None:
    """Bind the arm/execute controls to the LAST read-only preview ATTEMPT. Parses the
    executor's PREVIEW stdout into a compact, plain summary + a wall-clock timestamp and
    stores it in session state under ``cp_last_preview``. Never raises — a parse miss
    simply yields a sparser summary; the executor recomputes authoritatively at fire time
    regardless, so this is a review-binding aid, not the source of truth.

    WHY A FAILED RUN IS STORED, NOT DROPPED. The obvious fix for a crashed preview is to
    refuse to store it — but refusing leaves the PREVIOUS preview in session state, and if
    that one is still inside the freshness window the operator can arm off it while the crash
    shows only as a transient note that the next rerun wipes. So a failed run is stored, with
    an explicit ``ok``/``failure`` marker that BOTH the arm gate (_stored_preview_failure) and
    the top-of-page verdict consult. Storing retires the older preview and makes the failure
    survive every rerun, which is exactly what the gate needs."""
    try:
        parsed = _parse_preview(stdout or "")
        n_legs = len(parsed.get("legs") or [])
        total_sell = parsed.get("total_sell")
        total_buy = parsed.get("total_buy")
        sells = f"${total_sell:,.2f}" if total_sell is not None else "—"
        buys = f"${total_buy:,.2f}" if total_buy is not None else "—"
        failure = _preview_failure(parsed, returncode)
        now = datetime.now()
        st.session_state["cp_last_preview"] = {
            "built_at": now,                       # datetime — used for the 30-min age check
            "built_at_str": now.strftime("%H:%M"),
            "n_legs": n_legs,
            "sells": sells,
            "buys": buys,
            "account": parsed.get("account") or PREVIEW_ACCOUNT,
            "net_liq": parsed.get("net_liq"),
            # POSITIVE EVIDENCE the account was read and is in line — the ONLY thing the
            # green "nothing to trade" verdict is allowed to be drawn from.
            "already_conforms": bool(parsed.get("already_conforms")),
            "returncode": returncode,
            "ok": failure is None,
            "failure": failure,
            "summary": f"{n_legs} leg(s), sells {sells}, buys {buys}",
        }
    except Exception as exc:  # noqa: BLE001 — binding is best-effort; never break the preview
        # A binding that dies must not leave the PREVIOUS preview standing as armable.
        _store_failed_preview(
            "cp_last_preview",
            f"The last check could not be recorded because of an unexpected problem "
            f"({type(exc).__name__}). Build the preview again.")


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


def _render_preview_result(stdout: str, stderr: str,
                           returncode: int | None = None) -> None:
    """Turn the executor's PREVIEW output into a plain-English summary + leg table, then
    ALWAYS show the full raw log in an expander (the raw text is the source of truth).

    ``returncode`` is passed ONLY by the read-only preview path. The guarded execute path
    calls this exactly as before (no return code), so its rendering is unchanged."""
    parsed = _parse_preview(stdout)
    _preview_warning = _preview_parse_warning(parsed)

    # THE RUN ITSELF — did it finish and read the account? Said first, in red, above
    # everything else. (The same judgement is stored in session state, so it also survives
    # into the top-of-page verdict on every later rerun.)
    if returncode is not None:
        _run_failure = _preview_failure(parsed, returncode)
        if _run_failure:
            st.markdown(
                theme.status_card(
                    "Read-only preview",
                    "bad",
                    "This check did not complete — there is nothing to review",
                    _run_failure + " Nothing was transmitted, and nothing can be sent until "
                                   "a preview completes.",
                ),
                unsafe_allow_html=True,
            )

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

    # ANYTHING THE PARSER COULD NOT READ — said out loud, above the table, every time.
    if _preview_warning:
        st.error(_preview_warning)

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
    # The stored verdict on whether that preview actually ran. Read every rerun, so a failed
    # check keeps saying so instead of decaying into the green card on the next click.
    failure = _stored_preview_failure("cp_last_preview")
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
            if failure:
                # The check did not complete. Say that — never the green card, and never a
                # count of legs read out of output that was never produced.
                st.markdown(
                    theme.status_card(
                        "Rebalance status", "bad",
                        "The last check did not complete — the account was not read",
                        f"The {checked} check did not produce a plan to review, so this page "
                        f"cannot tell you what the account holds or whether it is in line "
                        f"with its target. {failure} Nothing has transmitted, and the Send "
                        f"button stays off until a check completes.",
                    ),
                    unsafe_allow_html=True,
                )
            elif n_legs and n_legs > 0:
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
            elif last.get("already_conforms"):
                # GREEN ONLY ON POSITIVE EVIDENCE: the executor itself printed that the
                # account already conforms. The ABSENCE of legs is not evidence of anything —
                # a preview that never ran has no legs either.
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
            else:
                # No legs, no conformance confirmation, and no recorded failure — a shape
                # nothing should produce. Backstop, and it never reads as 'in line'.
                st.markdown(
                    theme.status_card(
                        "Rebalance status", "warn",
                        "Not confirmed in line — check again",
                        f"The {checked} check listed no trades, but it also did not confirm "
                        f"that the account matches the Strategy 0 Growth target, so this "
                        f"page cannot say the account is in line. Build the preview again. "
                        f"Nothing has transmitted.",
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
        elif failure:
            # A young timestamp on a check that never completed must not read as "fresh".
            st.markdown(
                theme.card(
                    "Last checked",
                    f'<span style="color:{theme.TIER["bad"]["c"]};font-weight:650">'
                    f'{theme._esc(str(last.get("built_at_str", "—")))} · did not complete'
                    f'</span>',
                    "The last check did not produce a plan to review, so there is nothing to "
                    "arm from. Build the preview again.",
                ),
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
    """Run the hardened S0 executor in PREVIEW mode (no args) and render the plan. Same
    existence check, same subprocess call, same arguments/timeout, same parsing as before.
    What changed: the run's RETURN CODE is now carried into the render and into the stored
    preview, and every path that ends WITHOUT a reviewable plan (missing executor, timeout,
    launch failure, non-zero exit) records that failure under the arm gate's session key —
    so a failed attempt retires the previous preview instead of leaving an older, still-fresh
    one armable. Transmits nothing."""
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
        _store_failed_preview(
            "cp_last_preview",
            "The preview could not start: the executor or its Python could not be found on "
            "this machine.")
        return

    try:
        with st.spinner("Building the read-only preview (reading the live-trade "
                        "gateway on port 4003)…"):
            proc = deskproc.run(
                [VENV_PYTHON, str(_DEPLOY_SCRIPT)],
                cwd=str(_DEPLOY_CWD),
                capture_output=True,
                text=True,
                timeout=_PREVIEW_TIMEOUT_SEC,
            )
        stdout, stderr = proc.stdout or "", proc.stderr or ""
        returncode = proc.returncode
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
        _store_failed_preview(
            "cp_last_preview",
            "The preview did not finish in time, so the account was never read. The "
            "live-trade Gateway on port 4003 may be down or not logged in.")
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
        _store_failed_preview(
            "cp_last_preview",
            f"The preview could not be run ({type(exc).__name__}), so the account was never "
            f"read. The live-trade Gateway on port 4003 may be down or not logged in.")
        return

    _render_preview_result(stdout, stderr, returncode)
    # Bind the arm/execute step to THIS attempt — including, deliberately, a FAILED one, so a
    # crash retires the previous preview and keeps saying so on every rerun.
    _store_last_preview(stdout, returncode)
    _audit_preview(_stored_preview_failure("cp_last_preview"))  # best-effort durable audit


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
            proc = deskproc.run(
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
            proc = deskproc.run(
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
# WHOLE-BOOK OUT-OF-SPEC READ (read-only, all roster accounts).                #
# =========================================================================== #
# This extends the page beyond the single pinned account (PREVIEW_ACCOUNT) with a
# READ-ONLY, multi-account out-of-spec surface built from the CRM roster view
# (v_tradingdesk_roster) and the frozen rebalance engine. It reuses the exact
# crm_execute.preview_crm posture — the UNCHANGED pure rebalance_engine.build_plan, with no
# `ib` and armed=False — so it builds and transmits NOTHING. It shares no state with, and
# touches none of, the gated single-account Send rail above.
@st.cache_resource(show_spinner="Running the frozen desk model (validated engine)…")
def _target_for(version: str):
    """The frozen desk model target for one version, as a strategy_target.Target. Cached as a
    resource (non-serialisable object, and the backtest is expensive) so a whole-book scan
    runs the engine once per distinct version. Broker-free."""
    import strategy_target
    return strategy_target.current_target(version=version)


def _scan_whole_book() -> dict:
    """Read the whole blessed roster + latest holdings from the CRM (read-only role) and run
    the frozen engine to get every account's in-spec / out-of-spec verdict + would-trade legs.

    Returns the crm_outofspec.scan_out_of_spec dict plus a 'built_at' stamp, or a dict with an
    'error' key if the CRM is not configured/reachable. Builds and transmits NOTHING."""
    import crm_roster
    import crm_outofspec

    if not crm_roster.is_configured():
        return {"error": "not_configured"}
    try:
        rows = crm_roster.fetch_roster(advisor_name=None)  # whole book; filter in-app
        holdings = crm_roster.fetch_holdings_latest([r["account_id"] for r in rows])
    except crm_roster.CrmRosterUnavailable as exc:
        return {"error": str(exc)}

    # Build a target per DISTINCT model present; drop rows whose model has no frozen target.
    versions = sorted({(r.get("model") or "") for r in rows if r.get("model")})
    targets: dict = {}
    bad_versions: list[str] = []
    for v in versions:
        try:
            targets[v] = _target_for(v)
        except Exception as exc:  # noqa: BLE001 — a model with no validated engine is skipped
            bad_versions.append(f"{v} ({exc})")
    rows = [r for r in rows if (r.get("model") or "") in targets]

    scan = crm_outofspec.scan_out_of_spec(rows, holdings, targets)
    scan["built_at_str"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    scan["bad_versions"] = bad_versions
    return scan


def _render_whole_book_outofspec() -> None:
    """The read-only whole-book out-of-spec panel: a Scan button, summary tiles, an
    advisor/model filter, and a per-account verdict table with the would-trade legs.
    Read-only end to end — nothing is armed, placed, or transmitted."""
    st.markdown(theme.section("Whole-book out-of-spec read (read-only, all accounts)"),
                unsafe_allow_html=True)
    st.caption(
        "Every blessed account across the book, checked against the desk's frozen model with "
        "the SAME pure engine the single-account preview uses (crm_execute.preview_crm "
        "posture: no broker, armed=False — it builds and transmits nothing). Reads the CRM "
        "roster view and the latest holdings snapshot; places, arms, and sends nothing."
    )

    if st.button("Scan the whole book (read-only)", key="cp_wholebook_scan"):
        with st.spinner("Reading the CRM roster + holdings and running the frozen engine "
                        "(read-only)…"):
            st.session_state["cp_wholebook"] = _scan_whole_book()

    scan = st.session_state.get("cp_wholebook")
    if not isinstance(scan, dict):
        st.info("Press 'Scan the whole book' to read every account's out-of-spec verdict. "
                "Read-only — nothing is placed, armed, or transmitted.")
        return

    if scan.get("error") == "not_configured":
        st.warning(
            "The CRM connection is not wired yet. Andrew must set the `TRADINGDESK_CRM_DSN` "
            "environment variable to the read-only role's connection string (the "
            "`tradingdesk_readonly` Postgres role) before this whole-book read can run. No "
            "credential is stored in code; nothing here transmits."
        )
        return
    if scan.get("error"):
        st.error(f"Could not read the CRM roster (read-only): {scan['error']}")
        return

    verdicts = scan.get("verdicts", [])
    skipped = scan.get("skipped", [])

    # Filters (operate in-memory on the already-scanned book — no re-query, no re-run).
    advisors = sorted({(v.get("advisor_name") or "— unassigned —") for v in verdicts})
    models = sorted({(v.get("version") or "") for v in verdicts})
    fc1, fc2, fc3 = st.columns([2, 1, 1])
    with fc1:
        adv_pick = st.selectbox("Advisor", ["(whole book)"] + advisors, index=0,
                                key="cp_wb_advisor")
    with fc2:
        model_pick = st.selectbox("Model", ["(all)"] + models, index=0, key="cp_wb_model")
    with fc3:
        only_oos = st.checkbox("Out-of-spec only", value=True, key="cp_wb_oos_only")

    def _keep(v: dict) -> bool:
        if adv_pick != "(whole book)" and (v.get("advisor_name") or "— unassigned —") != adv_pick:
            return False
        if model_pick != "(all)" and (v.get("version") or "") != model_pick:
            return False
        if only_oos and not v.get("out_of_spec"):
            return False
        return True

    shown = [v for v in verdicts if _keep(v)]
    scoped = [v for v in verdicts
              if (adv_pick == "(whole book)"
                  or (v.get("advisor_name") or "— unassigned —") == adv_pick)
              and (model_pick == "(all)" or (v.get("version") or "") == model_pick)]
    n_oos = sum(1 for v in scoped if v.get("out_of_spec"))

    t1, t2, t3 = st.columns(3)
    t1.metric("Accounts in scope", len(scoped))
    t2.metric("Out of spec", n_oos)
    t3.metric("In spec", len(scoped) - n_oos)
    st.caption(f"Scanned {scan.get('n_accounts', 0)} funded accounts across the book · "
               f"{scan.get('n_out_of_spec', 0)} out of spec · checked "
               f"{scan.get('built_at_str', '—')}. "
               + (f"{len(skipped)} unfunded/no-snapshot accounts skipped. " if skipped else "")
               + "Read-only — nothing transmitted.")
    if scan.get("bad_versions"):
        st.caption("Models with no frozen engine target (skipped): "
                   + ", ".join(scan["bad_versions"]))

    if not shown:
        st.success("No accounts match the current filter"
                   + (" (nothing out of spec in scope)." if only_oos else "."))
    else:
        table = [{
            "Account": v["account"],
            "Advisor": v.get("advisor_name") or "—",
            "Entity": v.get("entity") or "—",
            "Model": v.get("version") or "—",
            "Verdict": ("HELD BACK" if v.get("blocked")
                        else "OUT OF SPEC" if v["out_of_spec"] else "in spec"),
            "Account value": round(float(v.get("net_liq") or 0.0), 2),
            # Holdings the desk never trades sit OUTSIDE the model allocation, so the
            # model's 100% applies to this remainder — and that is what the would-trade
            # legs rebalance. The two columns always sum back to Account value.
            "Value the model manages": round(
                float(v.get("managed_net_liq", v.get("net_liq")) or 0.0), 2),
            "Value we never trade": round(float(v.get("held_aside_value") or 0.0), 2),
            "Positions": v.get("n_positions", 0),
            "Would-trade legs": v.get("n_legs", 0),
            "Alien": v.get("n_alien", 0),
            "Holdings we never trade": v.get("n_held_aside", 0),
            "Holdings we could not identify": v.get("n_unclassified", 0),
        } for v in shown]
        st.dataframe(pd.DataFrame(table), hide_index=True, use_container_width=True)

        # Per-account would-trade legs (the conform plan), collapsed.
        with st.expander(f"Show the would-trade legs for the {len(shown)} shown account(s)"):
            for v in shown:
                if not v.get("legs"):
                    continue
                st.markdown(f"**{v['account']}** · {v.get('advisor_name') or '—'} · "
                            f"{v.get('version') or '—'} — {v['n_legs']} leg(s)")
                st.dataframe(pd.DataFrame(v["legs"]), hide_index=True,
                             use_container_width=True)

        # HOLDINGS THE DESK NEVER TRADES (individual bonds first among them). They are on a
        # no-trade list, not a pending manual sale: priced, counted and named here, sitting
        # outside the model allocation. No order is ever emitted for one.
        if any(v.get("held_aside") for v in shown):
            with st.expander("Holdings we never trade (priced and counted, outside the "
                             "model allocation)"):
                st.caption("These are held aside by decision. They are not drift, not "
                           "untracked, and not awaiting a sale — the model applies to the "
                           "rest of the account as its own 100%.")
                for v in shown:
                    if not v.get("held_aside"):
                        continue
                    st.markdown(f"**{v['account']}** · {v.get('advisor_name') or '—'} · "
                                f"{v.get('version') or '—'} — {v['n_held_aside']} holding(s), "
                                f"${float(v.get('held_aside_value') or 0.0):,.2f}")
                    st.dataframe(pd.DataFrame(v["held_aside"]), hide_index=True,
                                 use_container_width=True)

        # Accounts whose trades were HELD BACK for a data reason (a never-traded holding we
        # could not price, so the rest of the account cannot be sized safely).
        if any(v.get("blocked") for v in shown):
            with st.expander("Accounts with ALL trades held back (needs a look)"):
                for v in shown:
                    for reason in (v.get("blocked_reasons") or []):
                        st.markdown(f"**{v['account']}** — {reason}")


# =========================================================================== #
# BATCH REBALANCE — the multi-account, roster-scoped review -> arm -> transmit  #
# rail. It shells out to the UNCHANGED batch_rebalance_execute.py executor,     #
# modelled exactly on the single-account U14438624 rail above: PREVIEW by       #
# default (sizes every roster account, transmits nothing), and a gated ARM +    #
# SEND that runs the per-account two-phase cash-gated rebalance ONLY behind the  #
# same review -> arm -> transmit gate (fresh preview + typed confirm + a         #
# physically armed 4003 Gateway). Execution is sandboxed to roster.enrolled_     #
# roster() by the executor itself (the account wall); nothing here widens it.    #
# =========================================================================== #
# BATCH-ACCOUNT line: "    BATCH-ACCOUNT account=U... version=Growth status=... legs=3
#                       sells=2 buys=1 margin_preflight_ok=True"
# version= is the CRM's MODEL LABEL and real labels contain spaces and brackets —
# "Growth (Small)", "Growth (Custom)", "Balanced (Small, Custom)". The field is therefore read
# non-greedily up to its next known delimiter (" status=") rather than as \S+; \S+ stopped at
# the first space, the whole line failed to match, and every account on a spaced label was
# dropped from the table without a trace. The producer's line format is left alone on purpose:
# other consumers read it, and " status=" is already an unambiguous delimiter.
_RE_BATCH_ACCT = re.compile(
    r"BATCH-ACCOUNT\s+account=(\S+)\s+version=(.+?)\s+status=(\S+)\s+legs=(\d+)\s+"
    r"sells=(\d+)\s+buys=(\d+)\s+margin_preflight_ok=(\w+)")
# Deliberately-loose markers: "does this line CLAIM to be a batch account/summary row?".
# Anything that claims to be one but does not parse gets counted and shown, never skipped.
_RE_BATCH_ACCT_MARKER = re.compile(r"BATCH-ACCOUNT")
_RE_BATCH_SUMMARY_MARKER = re.compile(r"BATCH-SUMMARY")
# BATCH-SUMMARY line: "    BATCH-SUMMARY roster=2 out_of_spec=1 in_spec=1 skipped=0
#                       total_legs=3 total_sells=... total_buys=..."
_RE_BATCH_SUMMARY = re.compile(
    r"BATCH-SUMMARY\s+roster=(\d+)\s+out_of_spec=(\d+)\s+in_spec=(\d+)\s+skipped=(\d+)\s+"
    r"total_legs=(\d+)\s+total_sells=([\d\.]+)\s+total_buys=([\d\.]+)")
_RE_BATCH_ARMED_COMPLETE = re.compile(r"BATCH ARMED COMPLETE", re.IGNORECASE)
_RE_BATCH_BLOCKED = re.compile(r"BATCH TRANSMISSION BLOCKED|PREVIEW ONLY", re.IGNORECASE)


def _parse_batch_preview(stdout: str) -> dict:
    """Best-effort structured view of the batch executor's stdout. NEVER raises — every field
    is optional and the caller falls back to the raw log. Returns keys: accounts (list of
    per-account dicts), summary (dict or None), transmission_blocked (bool),
    unreadable_account_lines / unreadable_summary_lines (raw lines that announced themselves
    as batch rows but could not be read — counted and surfaced, NEVER silently dropped) and
    parse_error (str or None)."""
    out: dict = {"accounts": [], "summary": None, "transmission_blocked": False,
                 "unreadable_account_lines": [], "unreadable_summary_lines": [],
                 "parse_error": None}
    try:
        for line in stdout.splitlines():
            m = _RE_BATCH_ACCT.search(line)
            if m:
                out["accounts"].append({
                    "account": m.group(1),
                    "version": m.group(2),
                    "status": m.group(3),
                    "legs": int(m.group(4)),
                    "sells": int(m.group(5)),
                    "buys": int(m.group(6)),
                    "margin_preflight_ok": m.group(7) == "True",
                })
            elif _RE_BATCH_ACCT_MARKER.search(line):
                # It said BATCH-ACCOUNT and we could not read it. That account is NOT in the
                # table below; say so rather than letting it disappear.
                out["unreadable_account_lines"].append(line.strip())
            if (_RE_BATCH_SUMMARY_MARKER.search(line)
                    and not _RE_BATCH_SUMMARY.search(line)):
                out["unreadable_summary_lines"].append(line.strip())
        sm = _RE_BATCH_SUMMARY.search(stdout)
        if sm:
            out["summary"] = {
                "roster": int(sm.group(1)),
                "out_of_spec": int(sm.group(2)),
                "in_spec": int(sm.group(3)),
                "skipped": int(sm.group(4)),
                "total_legs": int(sm.group(5)),
                "total_sells": _fmt_num(sm.group(6)),
                "total_buys": _fmt_num(sm.group(7)),
            }
        out["transmission_blocked"] = bool(_RE_BATCH_BLOCKED.search(stdout))
    except Exception as exc:  # noqa: BLE001 — best-effort; raw log is the source of truth
        out["parse_error"] = f"{type(exc).__name__}: {exc}"
    return out


def _batch_reconciliation_parts(parsed: dict) -> list[str]:
    """The plain-English sentences describing where the batch TOTALS line disagrees with the
    account rows rendered under it — empty when the two agree. Pure.

    WHY THIS EXISTS. The four tiles at the top of the batch panel are read from the
    BATCH-SUMMARY line; the table beneath them is built from the BATCH-ACCOUNT lines. Nothing
    compared the two, so a summary claiming three out-of-spec accounts over a table holding
    two rows rendered clean and armed. The executor prints exactly ONE BATCH-ACCOUNT line per
    out-of-spec account, and its total_legs is the sum of those rows' legs
    (batch_rebalance_execute.summarize_batch), so the two MUST agree. When they do not, one of
    them is wrong, this page cannot tell which, and neither may be armed from."""
    if not isinstance(parsed, dict):
        return []
    sm = parsed.get("summary")
    if not isinstance(sm, dict):
        return []
    rows = list(parsed.get("accounts") or [])
    parts: list[str] = []

    claimed = sm.get("out_of_spec")
    if isinstance(claimed, int) and claimed != len(rows):
        parts.append(
            f"The batch totals say {claimed} "
            f"{'account is' if claimed == 1 else 'accounts are'} out of spec, but the table "
            f"below holds {len(rows)} account "
            f"{'row' if len(rows) == 1 else 'rows'}."
        )

    claimed_legs = sm.get("total_legs")
    row_legs = 0
    for row in rows:
        try:
            row_legs += int(row.get("legs") or 0)
        except (TypeError, ValueError):  # a row we could not read contributes nothing
            continue
    if isinstance(claimed_legs, int) and claimed_legs != row_legs:
        parts.append(
            f"The batch totals say {claimed_legs} order "
            f"{'leg' if claimed_legs == 1 else 'legs'} in total, but the account rows in the "
            f"table below add up to {row_legs}."
        )

    if parts:
        parts.append(
            "The totals above and the table below therefore do not describe the same batch, "
            "and there is no way to tell from this page which of the two is right."
        )
    return parts


def _batch_reconciliation_warning(parsed: dict) -> str | None:
    """The standalone plain-English warning that the batch totals and the batch table
    disagree, or None when they agree. Pure. Also folded into _batch_parse_warning so the
    operator sees ONE block of plain English above the table, not two competing ones."""
    parts = _batch_reconciliation_parts(parsed)
    if not parts:
        return None
    parts.append(
        "Do not treat this table as the full list of accounts, and do not arm from this "
        "preview. Read the full batch preview log below, which is the source of truth, and "
        "build the preview again. Nothing was transmitted."
    )
    return " ".join(parts)


def _batch_parse_warning(parsed: dict) -> str | None:
    """Plain-English warning about anything the BATCH parser could not read, or None when it
    read everything. Pure (no Streamlit) so it can be tested directly. This is the sentence
    that stops an operator arming off a table that quietly omits accounts."""
    if not isinstance(parsed, dict):
        return None
    bad_accts = list(parsed.get("unreadable_account_lines") or [])
    bad_summary = list(parsed.get("unreadable_summary_lines") or [])
    err = parsed.get("parse_error")
    # Rows that disagree with the totals belong in the SAME plain-English block as rows that
    # could not be read: both mean "this table is not the batch you think it is".
    reconcile = _batch_reconciliation_parts(parsed)
    if not bad_accts and not bad_summary and not err and not reconcile:
        return None
    parts: list[str] = []
    if bad_accts:
        n = len(bad_accts)
        parts.append(
            f"{n} account row{'s' if n != 1 else ''} could not be read from the preview "
            f"output and {'are' if n != 1 else 'is'} missing from this table."
        )
    if bad_summary:
        n = len(bad_summary)
        parts.append(
            f"{n} batch total line{'s' if n != 1 else ''} could not be read, so the "
            "account counts and dollar totals shown may be wrong."
        )
    if err:
        parts.append(
            "Reading the preview output stopped early because of an unexpected problem "
            f"({err}), so this table may be incomplete."
        )
    parts.extend(reconcile)
    parts.append(
        "Do not treat this table as the full list of accounts. Read the full batch preview "
        "log below, which is the source of truth. Nothing was transmitted."
    )
    return " ".join(parts)


def _batch_failure(parsed: dict, returncode: int | None) -> str | None:
    """Plain-English reason this BATCH preview must NOT be armed from, or None when it ran
    cleanly and its totals agree with its table. Pure. Same positive-evidence rule as the
    single-account rail (_preview_failure), plus the tiles-versus-table reconciliation."""
    rc = None
    if returncode is not None:
        try:
            rc = int(returncode)
        except (TypeError, ValueError):
            rc = None
    if rc is not None and rc != 0:
        return (
            f"The batch preview program stopped with an error instead of finishing normally "
            f"(exit code {rc}). No roster account was read, so there is nothing to review. "
            f"Read the full batch preview log below, then build the batch preview again."
        )
    if not isinstance(parsed, dict):
        return _UNRECORDED_PREVIEW_FAILURE
    err = parsed.get("parse_error")
    if err:
        return (
            "Reading the batch preview output stopped early because of an unexpected problem "
            f"({err}), so the account list shown may be incomplete. Build the batch preview "
            "again."
        )
    if not isinstance(parsed.get("summary"), dict):
        # No BATCH-SUMMARY line at all: there is nothing to check the table against, and no
        # confirmation the executor finished its pass over the roster.
        return (
            "The batch preview printed no totals line, so this page cannot confirm that the "
            "executor finished reading the roster, and there is nothing to check the account "
            "table against. Read the full batch preview log below and build the batch "
            "preview again."
        )
    return _batch_reconciliation_warning(parsed)


def _store_batch_last_preview(stdout: str, returncode: int | None = None,
                              models=None) -> None:
    """Bind the batch arm/send controls to the LAST batch preview ATTEMPT. Stores a compact
    summary + a wall-clock timestamp under ``cp_batch_last_preview`` (the freshness key), plus
    the same explicit ``ok``/``failure`` marker the single-account rail stores, so a crashed
    run or a totals-versus-table mismatch keeps refusing the arm gate on every rerun instead
    of decaying into a clean-looking preview. Never raises — the executor recomputes
    authoritatively at fire time regardless.

    ``models`` is the MODEL SCOPE this preview was actually built with, stored alongside it so
    the Step 3 gate can refuse to arm a scope the operator never reviewed
    (:func:`_batch_scope_mismatch`)."""
    try:
        parsed = _parse_batch_preview(stdout or "")
        sm = parsed.get("summary") or {}
        failure = _batch_failure(parsed, returncode)
        now = datetime.now()
        st.session_state["cp_batch_last_preview"] = {
            "built_at": now,                       # datetime — used for the 30-min age check
            "built_at_str": now.strftime("%H:%M"),
            "n_out_of_spec": sm.get("out_of_spec"),
            "n_roster": sm.get("roster"),
            "total_legs": sm.get("total_legs"),
            "returncode": returncode,
            "scope": _scope_key(models),           # the scope this preview reviewed
            "ok": failure is None,
            "failure": failure,
        }
    except Exception as exc:  # noqa: BLE001 — binding is best-effort; never break the preview
        # A binding that dies must not leave the PREVIOUS batch preview standing as armable.
        _store_failed_preview(
            "cp_batch_last_preview",
            f"The last batch check could not be recorded because of an unexpected problem "
            f"({type(exc).__name__}). Build the batch preview again.")


def _batch_preview_freshness(now: datetime | None = None) -> tuple[float | None, bool]:
    """(age_secs, is_fresh) for the LAST reviewed BATCH preview. Reuses the same pure freshness
    decision + window as the single-account rail (a reviewed batch preview also expires after
    30 min and must be rebuilt before an arm re-enables)."""
    last = st.session_state.get("cp_batch_last_preview")
    built_at = last.get("built_at") if isinstance(last, dict) else None
    return _freshness_of(built_at, now or datetime.now())


def _render_batch_preview_result(stdout: str, stderr: str,
                                 returncode: int | None = None) -> None:
    """Turn the batch executor's stdout into a per-account table + margin pre-flight column +
    aggregate summary, then ALWAYS show the raw log (the source of truth).

    ``returncode`` is passed ONLY by the read-only batch preview path; the guarded batch
    execute path calls this exactly as before, so its rendering is unchanged."""
    parsed = _parse_batch_preview(stdout)
    sm = parsed.get("summary")
    batch_warning = _batch_parse_warning(parsed)

    # THE RUN ITSELF — did it finish and read the roster? Said first, in red, above the tiles,
    # because a crashed batch preview used to render as a clean, armable, empty roster.
    if returncode is not None:
        _run_failure = _batch_failure(parsed, returncode)
        if _run_failure:
            st.markdown(
                theme.status_card(
                    "Batch read-only preview",
                    "bad",
                    "This batch check did not complete — there is nothing to review",
                    _run_failure + " Nothing was transmitted, and nothing can be sent until "
                                   "a batch preview completes.",
                ),
                unsafe_allow_html=True,
            )

    if sm is not None:
        t1, t2, t3, t4 = st.columns(4)
        t1.metric("Roster accounts", sm["roster"])
        t2.metric("Out of spec", sm["out_of_spec"])
        t3.metric("In spec", sm["in_spec"])
        t4.metric("Total legs", sm["total_legs"])
        st.caption(
            f"Total sells ~${sm['total_sells']:,.2f} · total buys ~${sm['total_buys']:,.2f} "
            f"(buys are re-sized to each account's realized cash at transmit time) · "
            + (f"{sm['skipped']} unfunded/invisible account(s) skipped · " if sm["skipped"]
               else "")
            + "Read-only preview — nothing transmitted."
        )

    # ANYTHING THE PARSER COULD NOT READ — said out loud, above the table, every time, and
    # in every branch below (including the "nothing to trade" one, where a dropped account
    # row is the single most dangerous thing that could go unmentioned).
    if batch_warning:
        st.error(batch_warning)
        with st.expander("Show the account rows that could not be read"):
            for raw in ((parsed.get("unreadable_account_lines") or [])
                        + (parsed.get("unreadable_summary_lines") or [])):
                st.code(raw, language=None)

    accts = parsed.get("accounts") or []
    if accts:
        st.markdown(
            theme.card(
                "Out-of-spec roster accounts (read-only preview)",
                f"{len(accts)} account(s) would be rebalanced to their model — each with its "
                f"own per-account margin pre-flight (#57) shown below. Every order routes "
                f"through the same fail-closed engine as the single-account lane; buys are "
                f"re-sized to realized cash at real transmit time.",
            ),
            unsafe_allow_html=True,
        )
        table = [{
            "Account": a["account"],
            "Model": a["version"],
            "Sells": a["sells"],
            "Buys": a["buys"],
            "Legs": a["legs"],
            "Margin pre-flight": "OK" if a["margin_preflight_ok"] else "REFUSED",
            "Executor status": a["status"],
        } for a in accts]
        st.dataframe(pd.DataFrame(table), hide_index=True, use_container_width=True)
    elif sm is not None and sm["out_of_spec"] == 0:
        st.markdown(
            theme.status_card(
                "Batch rebalance plan", "good", "Nothing to trade",
                "Every account on the blessed roster already conforms to its model — there is "
                "nothing to trade. Nothing was transmitted.",
            ),
            unsafe_allow_html=True,
        )
    else:
        st.warning(
            "The structured batch view was unavailable (the preview output did not match the "
            "expected format). See the full log below for exactly what the executor reported. "
            "Nothing was transmitted."
        )

    if parsed.get("transmission_blocked"):
        st.markdown(
            theme.status_card(
                "Transmission", "info", "Read-only preview — nothing transmitted",
                "The batch executor confirmed this was a preview only: transmission was "
                "blocked on every account and nothing was placed, armed, or sent.",
            ),
            unsafe_allow_html=True,
        )

    with st.expander("Show the full batch preview log"):
        st.code((stdout or "") + (("\n" + stderr) if stderr else ""), language=None)


def _run_batch_preview_and_render(models=None) -> None:
    """Run the batch executor in PREVIEW mode and render the per-account plan. Mirrors
    _run_preview_and_render: same existence check, same subprocess posture, same plain-English
    failure cards. Transmits nothing.

    ``models`` is the operator's MODEL SCOPE. It is passed to the executor as ``--models=...``
    and stored with the preview, so Step 3 can refuse to arm a different scope than the one
    reviewed here. Nothing selected -> the flag is omitted and this is the whole-book preview
    exactly as before."""
    if not os.path.exists(VENV_PYTHON) or not _BATCH_SCRIPT.exists():
        st.markdown(
            theme.status_card(
                "Batch read-only preview", "bad", "Could not start the batch preview",
                "The batch executor or its Python could not be found on this machine. Nothing "
                "was transmitted.",
            ),
            unsafe_allow_html=True,
        )
        _store_failed_preview(
            "cp_batch_last_preview",
            "The batch preview could not start: the batch executor or its Python could not "
            "be found on this machine.")
        return
    try:
        with st.spinner("Building the read-only BATCH preview (reading every roster account "
                        "on the port-4003 gateway)…"):
            proc = deskproc.run(
                [VENV_PYTHON, str(_BATCH_SCRIPT)] + _batch_models_flag(models),
                cwd=str(_BATCH_CWD), capture_output=True, text=True,
                timeout=_BATCH_PREVIEW_TIMEOUT_SEC,
            )
        stdout, stderr = proc.stdout or "", proc.stderr or ""
        returncode = proc.returncode
    except subprocess.TimeoutExpired:
        st.markdown(
            theme.status_card(
                "Batch read-only preview", "bad", "Timed out reaching the live-trade gateway",
                "The batch preview did not finish in time. The live-trade Gateway (port 4003) "
                "may be down or not logged in. Nothing was transmitted.",
            ),
            unsafe_allow_html=True,
        )
        _store_failed_preview(
            "cp_batch_last_preview",
            "The batch preview did not finish in time, so the roster was never fully read. "
            "The live-trade Gateway on port 4003 may be down or not logged in.")
        return
    except Exception as exc:  # noqa: BLE001 — any failure is a plain-English card, never a crash
        st.markdown(
            theme.status_card(
                "Batch read-only preview", "bad", "Couldn't reach the live-trade gateway",
                f"Couldn't reach the live-trade gateway (port 4003) — is it up and logged in? "
                f"({type(exc).__name__}). Nothing was transmitted.",
            ),
            unsafe_allow_html=True,
        )
        _store_failed_preview(
            "cp_batch_last_preview",
            f"The batch preview could not be run ({type(exc).__name__}), so the roster was "
            f"never read. The live-trade Gateway on port 4003 may be down or not logged in.")
        return

    _render_batch_preview_result(stdout, stderr, returncode)
    # Bind the batch arm/send step to THIS attempt — including, deliberately, a failed one —
    # and to the exact model scope this attempt was built with.
    _store_batch_last_preview(stdout, returncode, models=models)
    _batch_note = _stored_preview_failure("cp_batch_last_preview")
    _arm_execute_audit(
        category="control_plane_batch_preview",
        message=(("A read-only multi-account BATCH rebalance preview across the blessed "
                  f"roster did NOT complete: {_batch_note} Nothing was transmitted, and "
                  "nothing may be sent from it.") if _batch_note else
                 ("Built a read-only multi-account BATCH rebalance preview across the "
                  "blessed roster — nothing was transmitted.")),
        severity=("warn" if _batch_note else "info"))


def _classify_batch_output(stdout: str, stderr: str) -> str:
    """Classify the batch executor's real-run output into 'filled' | 'blocked' | 'error'.
    'filled' wins when the batch armed-complete line is present; otherwise any block marker
    means nothing transmitted; anything else is an unexpected error."""
    combined = ((stdout or "") + "\n" + (stderr or "")).lower()
    if "batch armed complete" in combined:
        return "filled"
    if any(mk in combined for mk in ("batch transmission blocked", "preview only",
                                     "not armed", "would transmit")):
        return "blocked"
    return "error"


def _run_batch_execute_and_render(can_press: bool, pressed: bool, models=None) -> None:
    """The batch transmit path — modelled on _run_execute_and_render: same
    `if not can_press or not pressed: return` gate, same executor-existence check, same guarded
    audit, same in-handler arm-token construction, same subprocess invocation of the batch
    executor with the token, same result classification + audits. Returns immediately unless
    BOTH gates hold and the button was pressed.

    ``models`` is the MODEL SCOPE, passed to the executor as ``--models=...``. `can_press`
    already carries the caller's scope gate: it is False whenever the selected scope differs
    from the one the stored preview was built with, so a widened selector can never fire off
    a narrower review."""
    if not can_press or not pressed:
        return
    if not os.path.exists(VENV_PYTHON) or not _BATCH_SCRIPT.exists():
        st.markdown(
            theme.status_card(
                "Batch execute", "bad", "Could not start the batch executor",
                "The batch executor or its Python could not be found on this machine. Nothing "
                "was transmitted.",
            ),
            unsafe_allow_html=True,
        )
        return

    _arm_execute_audit(
        category="control_plane_batch_execute_fired",
        message=("Operator armed and fired the multi-account BATCH rebalance across the "
                 "blessed roster (typed confirm + reviewed preview). The executor transmits "
                 "only if the 4003 Gateway is physically armed and each account's gate passes."),
        severity="warn")

    # The arm token is built ONLY here, inside the guarded, gated handler.
    arm_token = "--arm-i-" + "understand"
    try:
        with st.spinner("Transmitting the BATCH rebalance to the live-trade Gateway "
                        "(port 4003) — per-account two-phase cash-gated across the roster…"):
            proc = deskproc.run(
                [VENV_PYTHON, str(_BATCH_SCRIPT), arm_token] + _batch_models_flag(models),
                cwd=str(_BATCH_CWD), capture_output=True, text=True,
                timeout=_BATCH_EXECUTE_TIMEOUT_SEC,
            )
        stdout, stderr = proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired:
        _arm_execute_audit(
            category="control_plane_batch_execute_result",
            message=("The BATCH rebalance execute run timed out before the executor reported "
                     "a result; the Gateway may be down. Transmission state is unconfirmed — "
                     "verify in TWS."),
            severity="bad")
        st.markdown(
            theme.status_card(
                "Batch execute", "bad", "Timed out before the executor reported back",
                "The batch run did not finish in time. The live-trade Gateway (port 4003) may "
                "be down or not logged in. Check each account and open orders in TWS to "
                "confirm state before trying again.",
            ),
            unsafe_allow_html=True,
        )
        return
    except Exception as exc:  # noqa: BLE001 — any failure is a plain-English card, never a crash
        _arm_execute_audit(
            category="control_plane_batch_execute_result",
            message=(f"The BATCH rebalance execute run failed to start or crashed "
                     f"({type(exc).__name__}). Nothing was confirmed transmitted — verify in "
                     f"TWS."),
            severity="bad")
        st.markdown(
            theme.status_card(
                "Batch execute", "bad", "Couldn't run the batch executor",
                f"Couldn't run the batch executor ({type(exc).__name__}) — the live-trade "
                f"Gateway (port 4003) may be down. Nothing was confirmed transmitted; check "
                f"the accounts in TWS.",
            ),
            unsafe_allow_html=True,
        )
        return

    verdict = _classify_batch_output(stdout, stderr)
    if verdict == "filled":
        st.markdown(
            theme.status_card(
                "Batch execute", "good", "The batch rebalance was transmitted",
                "The per-account rebalance was transmitted across the roster — review each "
                "account's fills below and DISARM the Gateway (re-check 'Read-Only API' on the "
                "port 4003 Gateway in TWS) now that you are finished.",
            ),
            unsafe_allow_html=True,
        )
        result_msg = ("The multi-account BATCH rebalance was transmitted across the blessed "
                      "roster (per-account two-phase cash-gated). Review fills and disarm the "
                      "Gateway.")
        result_sev = "good"
    elif verdict == "blocked":
        st.markdown(
            theme.status_card(
                "Batch execute", "warn", "Nothing was transmitted — the Gateway was not armed",
                "Nothing was transmitted on any account — the Gateway was not armed (or a "
                "safety gate blocked it). Arm the 4003 Gateway in TWS (uncheck 'Read-Only "
                "API') and try again.",
            ),
            unsafe_allow_html=True,
        )
        result_msg = ("The BATCH rebalance execute run transmitted NOTHING — the 4003 Gateway "
                      "was not armed or a safety gate blocked every account.")
        result_sev = "warn"
    else:
        st.markdown(
            theme.status_card(
                "Batch execute", "bad", "The batch executor returned an unexpected result",
                "The executor did not report either a completed batch or a clean block. Read "
                "the full log below carefully and verify every account and open orders in TWS "
                "before doing anything else.",
            ),
            unsafe_allow_html=True,
        )
        result_msg = ("The BATCH rebalance execute run returned an UNEXPECTED result (neither "
                      "completed nor a clean block) — verify in TWS.")
        result_sev = "bad"

    _render_batch_preview_result(stdout, stderr)
    _arm_execute_audit(category="control_plane_batch_execute_result",
                       message=result_msg, severity=result_sev)


def _render_batch_rebalance() -> None:
    """The multi-account BATCH rebalance rail: Review (build a read-only preview of every
    out-of-spec roster account) -> Arm (check the 4003 Gateway armed state + type the confirm
    phrase) -> Send (run the per-account two-phase rebalance). Modelled on the single-account
    rail; execution is sandboxed to roster.enrolled_roster() by the executor's own account
    wall. Nothing sends until the operator refreshes a preview, physically arms the gateway in
    TWS, types the confirm phrase, and presses Send."""
    st.caption(
        "Rebalance every out-of-spec account on the blessed roster to its model — or only the "
        "models you select in Step 1 — one account at a time, behind the SAME review -> arm -> "
        "transmit gate as the single-account lane. "
        "Each account routes through the same fail-closed engine (per-account margin pre-flight "
        "+ two-phase cash-gated transmit). Execution is scoped to the roster allow-list "
        "(roster.enrolled_roster()); it never widens beyond the blessed accounts. Read-only "
        "until you deliberately refresh a preview, arm the gateway by hand in TWS, type the "
        "confirm phrase, and press Send."
    )

    plan_slot = st.container()
    cols = st.columns(3)

    # Step 1 · Review.
    with cols[0]:
        st.markdown(theme.section("Step 1 · Review"), unsafe_allow_html=True)
        # THE MODEL SCOPE. Choosing nothing means the whole book (the pre-existing run). This
        # is the ONLY way to narrow the run, and it narrows the ROSTER itself — the account
        # wall then refuses everything outside it, so a scoped run cannot reach an account the
        # operator did not select.
        _model_choices = _batch_model_choices()
        selected_models = st.multiselect(
            "Which models to rebalance (leave empty for every model — the whole book)",
            options=_model_choices, key="cp_batch_model_scope",
            placeholder="Every model on the blessed roster (the whole book)")
        if not _model_choices:
            st.caption("The list of models could not be read from the client records right "
                       "now, so this run covers the whole blessed roster.")
        st.caption("Build a read-only preview of every out-of-spec account in the selected "
                   "scope. Reads the gateway; transmits nothing.")
        pressed_build = st.button("Build read-only batch preview", key="cp_batch_build_btn")

    if pressed_build:
        with plan_slot:
            _run_batch_preview_and_render(selected_models)

    batch_age_secs, batch_fresh = _batch_preview_freshness()
    # ...and whether that batch preview completed AND its totals agree with its table. Read
    # from session state, so a crash or a mismatch keeps refusing across reruns.
    batch_failure = _stored_preview_failure("cp_batch_last_preview")
    batch_usable = _preview_is_armable(batch_fresh, batch_failure)
    has_batch_preview = isinstance(st.session_state.get("cp_batch_last_preview"), dict)
    with cols[0]:
        if not has_batch_preview:
            st.markdown(theme.pill("No batch preview yet — press Build", "unknown"),
                        unsafe_allow_html=True)
        elif batch_failure:
            st.markdown(
                theme.pill("The last batch check did not complete — build it again", "bad"),
                unsafe_allow_html=True)
        elif batch_fresh:
            st.markdown(
                theme.pill(f"Preview fresh (under {int(PREVIEW_FRESHNESS_SECS // 60)} min)",
                           "good"), unsafe_allow_html=True)
        else:
            _mins = int((batch_age_secs or 0) // 60)
            st.markdown(theme.pill(f"Preview expired ({_mins} min old) — rebuild it", "warn"),
                        unsafe_allow_html=True)

    # THE SCOPE THE STORED PREVIEW WAS BUILT WITH, and whether the selector still says the same
    # thing. A preview may only ever arm the exact set of accounts it reviewed.
    _stored_batch = st.session_state.get("cp_batch_last_preview")
    previewed_scope = (_stored_batch.get("scope")
                       if isinstance(_stored_batch, dict) else None)
    scope_mismatch = _batch_scope_mismatch(selected_models, previewed_scope)

    # Step 2 · Arm — reuse the SAME read-only 4003 armed-state probe as the single-account rail,
    # plus a typed confirm phrase. The phrase is SCOPE-AWARE: the whole book types
    # "REBALANCE ALL"; a run narrowed to selected models types "REBALANCE SELECTED", so the
    # words the operator types always describe what is actually about to be sent.
    confirm_phrase = _batch_confirm_phrase(selected_models)
    with cols[1]:
        st.markdown(theme.section("Step 2 · Arm"), unsafe_allow_html=True)
        st.caption("Uncheck 'Read-Only API' on the port-4003 Gateway in TWS by hand, then "
                   f"type '{confirm_phrase}' to confirm you reviewed the batch preview "
                   f"and armed it.")
        pressed_arm = st.button("Check whether the 4003 Gateway is armed",
                                key="cp_batch_arm_probe_btn")
        confirm_val = st.text_input(
            f"Type '{confirm_phrase}' to confirm", value="",
            key="cp_batch_execute_confirm",
            placeholder=f"type {confirm_phrase} here")
        confirmed = confirm_val.strip().upper() == confirm_phrase
        if confirmed:
            st.markdown(theme.pill("Confirm phrase typed", "good"), unsafe_allow_html=True)
        else:
            st.markdown(theme.pill(f"Type {confirm_phrase} to confirm", "warn"),
                        unsafe_allow_html=True)

    arm_slot = st.container()
    if pressed_arm:
        with arm_slot:
            _run_arm_probe_and_render()   # reuse the single-account rail's armed-state probe

    # Step 3 · Send.
    with cols[2]:
        st.markdown(theme.section("Step 3 · Send"), unsafe_allow_html=True)
        # THE GATE. A batch preview must be young, a completed read of the roster, internally
        # consistent (its totals matching its table), AND built with the SAME model scope the
        # selector still shows, before it can be armed from.
        can_press = batch_usable and confirmed and scope_mismatch is None
        if scope_mismatch:
            st.markdown(
                theme.status_card(
                    "Batch send", "warn", "The selected models changed since the preview",
                    scope_mismatch + " Nothing was transmitted.",
                ),
                unsafe_allow_html=True,
            )
        pressed = st.button("Send batch rebalance to IBKR", key="cp_batch_execute_btn",
                            disabled=not can_press, use_container_width=True)
        if batch_failure:
            step1_mark = ("• the last batch check did not complete, or its totals did not "
                          "match its table — build the batch preview again")
        elif batch_fresh:
            step1_mark = "✓ fresh batch preview reviewed"
        elif batch_age_secs is not None:
            step1_mark = (f"• expired — rebuild the batch preview "
                          f"({int(batch_age_secs // 60)} min old)")
        else:
            step1_mark = "• not yet — build the batch preview"
        step2_mark = ("✓ confirm phrase typed" if confirmed
                      else f"• not yet — type {confirm_phrase}")
        step3_mark = ("• the selected models changed — build the batch preview again"
                      if scope_mismatch else
                      ("✓ sending only " + ", ".join(_scope_key(selected_models))
                       if _scope_key(selected_models)
                       else "✓ sending every model on the blessed roster"))
        st.markdown(
            f"- {step1_mark}\n"
            f"- {step2_mark}\n"
            f"- {step3_mark}\n"
            f"- Even with both ✓, nothing sends unless the port-4003 Gateway is physically "
            f"armed in TWS ('Read-Only API' unchecked) — the executor measures it per account "
            f"and refuses otherwise. Execution stays scoped to the blessed roster."
        )

    send_slot = st.container()
    with send_slot:
        _run_batch_execute_and_render(can_press, pressed, selected_models)


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
    # ...and whether that preview actually completed. A young timestamp on a crashed run is
    # not a reviewed preview; this is read from session state, so it survives every rerun.
    preview_failure = _stored_preview_failure("cp_last_preview")
    preview_usable = _preview_is_armable(preview_fresh, preview_failure)
    has_preview = isinstance(st.session_state.get("cp_last_preview"), dict)
    with cols[0]:
        if not has_preview:
            st.markdown(theme.pill("No preview yet — press Build", "unknown"),
                        unsafe_allow_html=True)
        elif preview_failure:
            st.markdown(
                theme.pill("The last check did not complete — build the preview again",
                           "bad"),
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
        # THE GATE. A preview must be BOTH young AND a completed read of the account before
        # it can be armed from — a crashed run stores a failure marker and refuses here.
        can_press = preview_usable and confirmed
        pressed = st.button(
            "Send order to IBKR",
            key="cp_execute_btn",
            disabled=not can_press,
            use_container_width=True,
        )
        if preview_failure:
            step1_mark = ("• the last check did not complete — build the preview again "
                          "(nothing can be sent from a check that did not read the account)")
        elif preview_fresh:
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

    # --- WHOLE-BOOK OUT-OF-SPEC READ (read-only, multi-account). ------------------------ #
    # Extends the page past the single pinned account with a read-only, all-accounts
    # out-of-spec surface (CRM roster + frozen engine). Builds/transmits nothing; it shares
    # no state with the gated Send rail above.
    st.divider()
    with st.expander("Whole-book out-of-spec read (read-only — every account)",
                     expanded=False):
        _render_whole_book_outofspec()

    # --- BATCH REBALANCE (transmit-capable, multi-account, roster-scoped). --------------- #
    # The transmit-capable counterpart to the read-only whole-book read above: it rebalances
    # every OUT-OF-SPEC roster account to its model behind the SAME review -> arm -> transmit
    # gate as the single-account rail. Sends NOTHING until a human refreshes a preview, arms
    # the 4003 Gateway by hand, types the confirm phrase, and presses Send; the executor keeps
    # execution scoped to roster.enrolled_roster() (the account wall).
    st.divider()
    st.markdown(theme.section("Batch rebalance — every out-of-spec roster account"),
                unsafe_allow_html=True)
    with st.expander("Batch rebalance the whole roster (review -> arm -> transmit)",
                     expanded=False):
        _render_batch_rebalance()

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
