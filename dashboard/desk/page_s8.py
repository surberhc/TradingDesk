"""page_s8.py — Strategy 8 page for the rebuilt Trading Desk (port 8502). READ-ONLY.

A faithful PORT of the S8 implementation in dashboard/app.py, re-skinned into the
new design system (theme.py) and with every user-facing label spelled out in full
plain English (the owner is a non-coder — no shorthand anywhere).

What this page is: an intraday PILOT_MODE monitor over the S8 capture store
(livebot/s8_store.py -> trades.jsonl). livebot/s8_service.py hardcodes
PILOT_MODE=True and transmits ZERO real orders — this page is a read-only window
onto the captured trade records plus a live exit-monitor overlay computed from the
pilot's OWN RECORDED TICKS (read-only parquet, in-memory DuckDB, zero Gateway
contact). It never places, arms, or transmits anything, and never writes any store.

READ-ONLY / NO-TRANSMIT guarantees (identical to app.py):
  * The ONLY broker path is connections.ibkr_live_trade.connect(readonly=True,
    launch=False, short timeout) — the live-TRADING Gateway (port 4003), read-only
    and display-only. It is used ONLY for ib.accountSummary() (account/margin
    snapshot) and is disconnected in a finally: before the fragment returns.
  * No ib.placeOrder / arm / transmit / replaceFA, no order object is ever built,
    no store is ever written.
  * The live exit-monitor makes NO Gateway contact at all — it reads the pilot's
    own recorded tick parquet.

IMPORT DISCIPLINE: module-top imports are CHEAP only (streamlit, pandas, stdlib,
theme, and the PURE data modules s8_config / s8_monitor_core / s8_report /
s8_schema / s8_store). The broker module (connections.ibkr_live_trade — the thing
that opens a socket) is imported LAZILY inside _s8_connect_readonly_short(), so
importing this page never opens a socket.

The P&L / distance-to-stop math is NEVER reimplemented here — it delegates verbatim
to livebot/s8_monitor_core (rule #1 of the desk: never curve-fit, never drift the
math). s8_distance_to_stop() below is the exact port from app.py.
"""
from __future__ import annotations

from datetime import datetime
from datetime import time as dt_time
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

# PURE / data-only modules (live in livebot/, on sys.path via desk_app.py's bootstrap).
# NONE of these opens a socket at import time. The broker import stays lazy (below).
import s8_config
import s8_monitor_core
import s8_report
import s8_schema
import s8_store
import theme as T

# --- Constants (ported verbatim from app.py) -----------------------------------
CT_ZONE = ZoneInfo("America/Chicago")     # matches s8_config.ENTRY_GRID_CT's convention
S8_QTY_PER_ENTRY = 1                       # fallback qty; records carry their own qty

# A tick older than this (seconds) is shown as "stale" rather than a live price.
S8_TICK_STALE_SECS = 90
# Bound the recorded-tick scan to the recent tail of part files (see app.py rationale).
S8_TICK_SCAN_WINDOW_SECS = 240


# ============================ STORE READS (read-only) ==========================
@st.cache_data(ttl=20)
def _s8_store_records() -> list:
    """All S8 capture-store trade records, read-only, latest-wins by trade_id.

    Delegates to livebot/s8_store.read_trade_records() — the SAME read s8_report.py
    uses — and never writes. Any read failure degrades to an empty list rather than
    taking the page down. Cached 20s (short — feeds the fast-refresh fragments)."""
    try:
        return s8_store.read_trade_records()
    except Exception:
        return []


def _s8_available_dates(records: list) -> list[str]:
    """Distinct captured session dates (bare YYYYMMDD), most-recent first. Pure."""
    return sorted({r.date for r in records if r.date}, reverse=True)


def _s8_records_for_date(records: list, date: str | None) -> list:
    """Records for one session date, reusing s8_report.select_records (pure). A None
    date means 'all captured records'."""
    if date is None:
        return list(records)
    return s8_report.select_records(records, date=date)


# ============ PURE distance-to-stop math (offline-testable; no st/IBKR) =========
def _s8_monitor_position(rec):
    """Build a livebot/s8_monitor_core.MonitorPosition from a stored TradeRecord.

    PURE (no I/O). realized_credit and stop_price are taken VERBATIM from the frozen
    entry (never recomputed here) — the monitor core only compares live prices against
    that already-frozen level (rule #1 stays clean)."""
    e = rec.entry
    return s8_monitor_core.MonitorPosition(
        trade_id=rec.trade_id,
        side=rec.side,
        short_strike=(e.short_strike if e else None),
        long_strike=(e.long_strike if e else None),
        qty=(rec.qty if rec.qty else S8_QTY_PER_ENTRY),
        realized_credit=(e.realized_credit if e and e.realized_credit is not None else 0.0),
        stop_price=(e.stop_price if e and e.stop_price is not None else 0.0),
    )


def s8_distance_to_stop(rec, short_ask, long_bid) -> dict:
    """PURE. Given an open TradeRecord + the CURRENT short-leg ask and long-leg bid,
    return the live exit-monitor row values using the CANONICAL frozen semantics from
    livebot/s8_monitor_core:

        spread_cost      = short_ask - long_bid                 (cost to close now)
        distance_to_stop = stop_price - spread_cost             (points; <=0 == stopped)
        running_pnl      = (realized_credit - spread_cost) * 100 * qty   (dollars)

    Computed by delegating to s8_monitor_core.spread_close_value / pnl_at so this page
    can never drift from the monitor's own stop/P&L math. None-safe: a missing quote
    yields None for whatever needs it. NO Streamlit or IBKR dependency."""
    pos = _s8_monitor_position(rec)
    sample = s8_monitor_core.Sample(short_ask=short_ask, long_bid=long_bid)
    spread_cost = s8_monitor_core.spread_close_value(sample)
    running_pnl = s8_monitor_core.pnl_at(pos, sample)
    stop_price = rec.entry.stop_price if rec.entry else None
    distance = None
    if spread_cost is not None and stop_price is not None:
        distance = float(stop_price) - spread_cost
    stopped = distance is not None and distance <= 0
    return {
        "spread_cost": spread_cost,
        "stop_price": stop_price,
        "distance_to_stop": distance,
        "running_pnl": running_pnl,
        "stopped": stopped,
    }


# ==================== RECORDED-TICK OVERLAY (zero Gateway contact) ==============
def _s8_num(value):
    """None for None/NaN/blank; float otherwise. Local mirror of s8_chain._num so the
    tick reader carries no IBKR dependency. PURE."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _s8_ticks_dataframe(session_date: str, trade_ids=None):
    """Read the LATEST recorded tick per (trade_id, leg) for one session date into a
    DataFrame, via an EPHEMERAL in-memory DuckDB view — the SAME read-only pattern
    livebot/s8_report.tick_counts uses: the on-disk catalog.duckdb is NEVER opened, and
    the running collector's parquet parts are only READ.

    ZERO Gateway contact. Any failure degrades to an empty frame, so the overlay
    fail-softs to '—'. Bounds the scan to the recent TAIL of part files and pushes the
    latest-per-leg reduction into SQL (verbatim port from app.py)."""
    import os

    ids = None if trade_ids is None else list(trade_ids)
    if ids is not None and not ids:
        return pd.DataFrame(columns=s8_schema.TICK_COLUMNS)
    ticks_dir = s8_store.get_root() / "ticks" / f"date={session_date}"
    try:
        parts = [(e.path, e.stat().st_mtime) for e in os.scandir(ticks_dir)
                 if e.name.endswith(".parquet")]
    except (FileNotFoundError, NotADirectoryError, OSError):
        return pd.DataFrame(columns=s8_schema.TICK_COLUMNS)
    if not parts:
        return pd.DataFrame(columns=s8_schema.TICK_COLUMNS)
    cutoff = max(m for _, m in parts) - S8_TICK_SCAN_WINDOW_SECS
    recent = [p for p, m in parts if m >= cutoff]
    try:
        import duckdb

        con = duckdb.connect(":memory:")
        try:
            files_sql = ",".join(
                "'" + p.replace("\\", "/").replace("'", "''") + "'" for p in recent
            )
            con.execute(
                "CREATE VIEW mon_ticks AS "
                f"SELECT * FROM read_parquet([{files_sql}], union_by_name=true)"
            )
            where = ""
            params: list = []
            if ids is not None:
                where = " WHERE trade_id IN (" + ",".join(["?"] * len(ids)) + ")"
                params = ids
            df = con.execute(
                "SELECT trade_id, ts, leg, bid, ask, last, delta, iv FROM mon_ticks"
                + where
                + " QUALIFY row_number() OVER "
                "(PARTITION BY trade_id, leg ORDER BY ts DESC) = 1",
                params,
            ).fetchdf()
        finally:
            con.close()
    except Exception:
        return pd.DataFrame(columns=s8_schema.TICK_COLUMNS)
    return df


def _s8_latest_tick_per_leg(df) -> dict:
    """PURE. Collapse a tick DataFrame to the LATEST tick per (trade_id, leg) by ts,
    returning {trade_id: {"short": {...}, "long": {...}}} with bid/ask/last/delta/iv/ts
    (numbers coerced, NaN -> None). Empty / None input -> {}. No I/O."""
    out: dict = {}
    if df is None or len(df) == 0:
        return out
    ordered = df.sort_values("ts")
    for (tid, leg), grp in ordered.groupby(["trade_id", "leg"], sort=False):
        row = grp.iloc[-1]
        out.setdefault(str(tid), {})[str(leg)] = {
            "bid": _s8_num(row.get("bid")),
            "ask": _s8_num(row.get("ask")),
            "last": _s8_num(row.get("last")),
            "delta": _s8_num(row.get("delta")),
            "iv": _s8_num(row.get("iv")),
            "ts": row.get("ts"),
        }
    return out


def _s8_tick_age_secs(ts_iso, now=None):
    """Seconds between ``now`` and an ISO-8601 tick timestamp; None if unparseable/blank.
    PURE (now defaults to the tick's own tz so age is well-defined offline)."""
    if not ts_iso:
        return None
    try:
        t = datetime.fromisoformat(str(ts_iso))
    except (TypeError, ValueError):
        return None
    ref = now or datetime.now(tz=t.tzinfo)
    if t.tzinfo is not None and ref.tzinfo is None:
        ref = ref.replace(tzinfo=t.tzinfo)
    return (ref - t).total_seconds()


def _s8_fmt_age(secs) -> str:
    """Compact '4s ago' / '2m03s ago' / '1h05m ago' freshness label; '—' if unknown."""
    if secs is None:
        return "—"
    s = int(round(secs))
    if s < 0:
        s = 0
    if s < 60:
        return f"{s}s ago"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m{s:02d}s ago"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m ago"


# ---- Single centralized recorded-tick monitor computation (shared everywhere) ----
# Column headers below are FULL plain-English phrases (the #1 label rule). The
# meaning is identical to app.py's cryptic headers; only the wording changed.
def _s8_monitor_rows(open_recs: list, session_date: str | None, is_today: bool):
    """Compute the live exit-monitor rows for the given open positions from the
    RECORDED ticks (zero Gateway contact). Returns (display_rows, summary) where summary
    = {"total_pnl", "any_live", "any_unpriced"}. This is the single path reused by the
    P&L headline, the monitor table, AND s8_today_pnl() — so they can never disagree."""
    fmt_n = s8_report._fmt_num
    fmt_k = s8_report._fmt_strike
    fmt_s = s8_report._fmt_signed

    latest: dict = {}
    if is_today and session_date and open_recs:
        ids = [r.trade_id for r in open_recs]
        latest = _s8_latest_tick_per_leg(_s8_ticks_dataframe(session_date, ids))

    rows = []
    total_pnl = 0.0
    any_live = False
    any_unpriced = False
    for r in open_recs:
        e = r.entry
        legs = latest.get(r.trade_id, {})
        sq = legs.get("short")
        lq = legs.get("long")
        # Freshness: as live as the most recent leg sample; take the newer of the two.
        age = None
        for q in (sq, lq):
            if q and q.get("ts"):
                a = _s8_tick_age_secs(q["ts"])
                if a is not None and (age is None or a < age):
                    age = a
        stale = age is not None and age > S8_TICK_STALE_SECS
        short_ask = (sq.get("ask") if sq else None) if not stale else None
        long_bid = (lq.get("bid") if lq else None) if not stale else None
        d = s8_distance_to_stop(r, short_ask, long_bid)
        if d["running_pnl"] is not None:
            any_live = True
            total_pnl += d["running_pnl"]
        else:
            any_unpriced = True
        if stale:
            state = "Quote is stale (capture paused)"
        elif d["stopped"]:
            state = "Stop-loss level reached"
        elif d["running_pnl"] is not None:
            state = "Live (fresh quote)"
        else:
            state = "No recorded quote yet"
        rows.append({
            "Template name": r.template or "—",
            "Time slot": r.slot or "—",
            "Side (call or put spread)": r.side or "—",
            "Short strike / long strike":
                (f"{fmt_k(e.short_strike)}/{fmt_k(e.long_strike)}" if e else "—"),
            "Stop-loss price": fmt_n(d["stop_price"]),
            "Cost to close the spread right now": fmt_n(d["spread_cost"]),
            "Points until the stop-loss triggers": fmt_s(d["distance_to_stop"]),
            "Running profit/loss on this position (dollars)":
                (f"{d['running_pnl']:,.0f}" if d["running_pnl"] is not None else "—"),
            "Current short-leg delta (option price change per $1 move in SPX)":
                fmt_n((sq.get("delta") if sq else None) if not stale else None, 3),
            "Current short-leg implied volatility":
                fmt_n((sq.get("iv") if sq else None) if not stale else None, 3),
            "Age of the latest recorded quote": _s8_fmt_age(age),
            "Live status": state,
        })
    return rows, {"total_pnl": total_pnl, "any_live": any_live,
                  "any_unpriced": any_unpriced}


# ============================ EXPORT: home-page P&L tile =======================
def s8_today_pnl() -> dict:
    """TODAY's live running profit/loss across OPEN S8 positions, from the pilot's OWN
    RECORDED TICKS (zero Gateway contact — recorded ticks only, cheap). Reuses the exact
    same recorded-tick path as the page's headline and monitor.

    Returns {"pnl": float|None, "open_count": int, "live": bool, "as_of": str}.
    pnl is None / live is False when there are no fresh recorded ticks (blank, not a
    guess). Never connects to the broker; never raises — any failure returns a blank."""
    now = datetime.now(tz=CT_ZONE)
    as_of = now.strftime("%Y-%m-%d %H:%M %Z")
    try:
        today = now.strftime("%Y%m%d")
        records = _s8_store_records()
        day_recs = _s8_records_for_date(records, today)
        open_recs = [r for r in day_recs if s8_report.is_open(r)]
        _, summary = _s8_monitor_rows(open_recs, today, is_today=True)
        live = bool(summary["any_live"])
        pnl = float(summary["total_pnl"]) if live else None
        return {"pnl": pnl, "open_count": len(open_recs), "live": live, "as_of": as_of}
    except Exception:
        return {"pnl": None, "open_count": 0, "live": False, "as_of": as_of}


# ============================ CONNECTION HELPERS ===============================
def _port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    """Cheap 'is something listening?' TCP probe — milliseconds, no asyncio, no trading
    session. A live connect just to print up/down previously crashed a scheduled job; a
    port-open check is a safe proxy for 'up' and safe on every page load."""
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _s8_connect_readonly_short(timeout: int = 5):
    """Connect to the live-TRADING Gateway (port 4003), READ-ONLY and DISPLAY-ONLY.

    readonly=True is passed explicitly — this account is transmit-capable at the broker
    level, but a monitor page only ever reads. SHORT timeout, launch=False (an
    auto-refreshing fragment must never boot a Gateway). The broker module import is
    LAZY (right here) so importing page_s8 never opens a socket."""
    from connections import ibkr_live_trade
    return ibkr_live_trade.connect("desk_s8", launch=False, readonly=True, timeout=timeout)


def _s8_account_summary(ib) -> dict:
    """AccountType/BuyingPower/ExcessLiquidity from the live-trading connection's own
    accountSummary() — the exact read s8_risk.py's margin_preflight() consumes."""
    rows = ib.accountSummary()
    m = {r.tag: r.value for r in rows}
    return {"AccountType": m.get("AccountType"), "BuyingPower": m.get("BuyingPower"),
            "ExcessLiquidity": m.get("ExcessLiquidity")}


# =============================== RENDER HELPERS ================================
def _header() -> None:
    st.markdown(
        f"<div style='display:flex;align-items:baseline;gap:.8rem;flex-wrap:wrap'>"
        f"<span style='font-size:1.9rem;font-weight:700;color:{T.TEXT}'>"
        f"Strategy 8 — British Iron Condor (0-days-to-expiry pilot)</span></div>",
        unsafe_allow_html=True,
    )
    st.markdown(
        f"<div style='font-size:12.5px;color:{T.MUTED};margin-top:.15rem'>"
        f"Pilot mode — this strategy schedules and monitors trades all day but "
        f"transmits ZERO real orders. It reports what it WOULD have done, reads the "
        f"capture store and its own recorded quotes, and never places, arms, or "
        f"transmits anything.</div>",
        unsafe_allow_html=True,
    )


def _render_headline() -> None:
    """Prominent LIVE running profit/loss headline for TODAY's open positions,
    computed from the pilot's own recorded quotes (zero Gateway contact)."""
    today = datetime.now(tz=CT_ZONE).strftime("%Y%m%d")
    records = _s8_store_records()
    day_recs = _s8_records_for_date(records, today)
    open_recs = [r for r in day_recs if s8_report.is_open(r)]
    _, summary = _s8_monitor_rows(open_recs, today, is_today=True)

    sub = ("live from the pilot's own recorded quotes; updates as ticks arrive; "
           "blank after the close")
    if not open_recs:
        tier, big = "unknown", "No open positions right now"
    elif not summary["any_live"]:
        tier, big = "unknown", "Waiting on fresh recorded quotes"
    else:
        total = summary["total_pnl"]
        tier = "good" if total >= 0 else "bad"
        big = f"{'+' if total >= 0 else '-'}${abs(total):,.0f}"
        if summary["any_unpriced"]:
            sub = ("partial — some open positions have no fresh quote this cycle; " + sub)
    st.markdown(
        T.status_card(
            "Today's running profit/loss on open positions (dollars)",
            tier, big, sub, pulse=(tier in ("good", "bad"))),
        unsafe_allow_html=True,
    )


def _s8_next_slot_today(grid: list[str] | None, now_ct: dt_time) -> str:
    if not grid:
        return "—"
    now_min = now_ct.hour * 60 + now_ct.minute
    for slot in grid:
        h, m = (int(x) for x in slot.split(":"))
        if h * 60 + m >= now_min:
            return slot
    return "done for today"


def _s8_last_fired(records: list, template: str) -> str:
    """Latest captured entry timestamp for `template` among today's records — the store
    IS the per-entry log, so a template 'fired today' iff it has a captured record
    today. Records are first-seen order, so the last match is the most recent."""
    latest = "—"
    for rec in records:
        if rec.template == template and rec.entry and rec.entry.entry_ts:
            latest = rec.entry.entry_ts
    return latest


def _s8_first_slot_minutes(grid: list[str] | None) -> int | None:
    """Minutes-since-midnight of a template's own FIRST scheduled entry — a fixed,
    day-independent sort key (NOT reordered by the clock). None sorts last."""
    if not grid:
        return None
    h, m = (int(x) for x in grid[0].split(":"))
    return h * 60 + m


def _render_schedule(records: list) -> None:
    st.markdown(T.section("Today's schedule — all 11 templates"),
                unsafe_allow_html=True)
    st.caption(
        "Times are Central (US/Central), matching the strategy's own schedule "
        "convention. Sorted by each template's own first scheduled entry time (fixed "
        "all day — not reordered by the clock); templates flagged NO SCHEDULE DATA are "
        "real gaps in the matched-fills sample (too few observations to name a grid) "
        "and are grouped at the bottom. 'Last time this fired today' is store-sourced "
        "(a captured trade record for the template today).")
    now_ct = datetime.now(tz=CT_ZONE).time()
    today = datetime.now(tz=CT_ZONE).strftime("%Y%m%d")
    records = _s8_records_for_date(records, today)
    templates_sorted = sorted(
        s8_config.TEMPLATES.items(),
        key=lambda kv: (
            (1, 0, kv[0]) if _s8_first_slot_minutes(s8_config.ENTRY_GRID_CT.get(kv[0])) is None
            else (0, _s8_first_slot_minutes(s8_config.ENTRY_GRID_CT.get(kv[0])), kv[0])
        ),
    )
    rows = []
    for name, cfg in templates_sorted:
        grid = s8_config.ENTRY_GRID_CT.get(name)
        last_fired = _s8_last_fired(records, name)
        if grid is None:
            next_slot, status = "n/a", "No schedule data (too few observations)"
        else:
            next_slot = _s8_next_slot_today(grid, now_ct)
            if last_fired != "—":
                status = "Fired today"
            elif next_slot == "done for today":
                status = "Done for today (never fired)"
            else:
                status = "Pending (waiting for its entry time)"
        rows.append({
            "Template name": name,
            "Side (call or put spread)": cfg["side"],
            "Spread width": cfg["width_label"],
            "Target credit at entry": f"${cfg['target_credit']:.0f}",
            "Next scheduled entry (Central time)": next_slot,
            "Last time this fired today": last_fired,
            "Status": status,
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


def _render_open_positions(open_recs: list) -> None:
    st.markdown(T.section("Open positions — entry snapshot"), unsafe_allow_html=True)
    st.caption(
        "Store-sourced (works offline with no Gateway). One row per still-open captured "
        "S8 credit spread for the selected session: strikes, entry credit, frozen "
        "stop-loss price, short-leg entry greeks, and entry SPX/VIX.")
    if not open_recs:
        st.info("No open positions for the selected session.")
        return
    fmt_n, fmt_k = s8_report._fmt_num, s8_report._fmt_strike
    rows = []
    for r in open_recs:
        e = r.entry
        sl = e.short_leg if e else None
        rows.append({
            "Template name": r.template or "—",
            "Time slot": r.slot or "—",
            "Side (call or put spread)": r.side or "—",
            "Short strike / long strike":
                (f"{fmt_k(e.short_strike)}/{fmt_k(e.long_strike)}" if e else "—"),
            "Credit received at entry": fmt_n(e.realized_credit if e else None),
            "Stop-loss price": fmt_n(e.stop_price if e else None),
            "Short-leg delta (option price change per $1 move in SPX)":
                fmt_n(sl.delta if sl else None, 3),
            "Short-leg gamma": fmt_n(sl.gamma if sl else None, 4),
            "Short-leg theta (daily time decay)": fmt_n(sl.theta if sl else None),
            "Short-leg implied volatility": fmt_n(sl.iv if sl else None, 3),
            "SPX price at entry": fmt_n(e.entry_spot if e else None),
            "VIX at entry": fmt_n(e.entry_vix if e else None),
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


def _render_live_monitor(open_recs: list, is_today: bool, session_date=None) -> None:
    st.markdown(T.section("Live exit-monitor — distance to stop (recorded quotes)"),
                unsafe_allow_html=True)
    st.caption(
        "The 'watch it happen' view. For each open position: current cost to close the "
        "spread (short-leg ask minus long-leg bid) versus its frozen stop-loss price -> "
        "points until the stop triggers + running profit/loss. The distance/P&L math is "
        "the canonical livebot/s8_monitor_core computation; the prices come from the "
        "pilot's OWN RECORDED QUOTES (read-only parquet, in-memory DuckDB, ZERO Gateway "
        f"contact) — the latest quote per leg. A quote older than {S8_TICK_STALE_SECS}s "
        "(e.g. after the close, when capture stops) is flagged stale and its live "
        "columns blanked rather than presenting old data as live. Past session / no "
        "quotes yet -> '—'.")
    if not open_recs:
        st.info("No open positions to monitor for the selected session.")
        return

    rows, summary = _s8_monitor_rows(open_recs, session_date, is_today)
    if summary["any_live"]:
        label = "Running profit/loss on open positions (live from recorded quotes)"
        if summary["any_unpriced"]:
            label += " — partial; some positions unpriced or stale this cycle"
        total = summary["total_pnl"]
        tier = "good" if total >= 0 else "bad"
        big = f"{'+' if total >= 0 else '-'}${abs(total):,.0f}"
        st.markdown(T.status_card(label, tier, big), unsafe_allow_html=True)
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    if not is_today:
        st.caption("Past session selected — positions shown without a live overlay "
                   "(the recorded-quote overlay applies to today's session only).")
    elif not summary["any_live"]:
        st.caption("No fresh recorded quotes for the open positions this cycle — live "
                   "columns blank; they populate as the pilot records quotes (and go "
                   "stale after the close, which is expected).")


def _render_closed(records: list, date: str | None) -> None:
    st.markdown(T.section("Closed round-trips for the session"), unsafe_allow_html=True)
    st.caption(
        "Store-sourced round-trips for the selected session (works offline): entry->exit "
        "SPX move, exit reason, profit/loss, worst unrealized loss during the trade (max "
        "adverse excursion), duration, and short-leg greeks at entry vs exit. The "
        "aggregate cards reuse livebot/s8_report.compute_aggregates (win rate / P&L / "
        "worst-loss over CLOSED trades only).")
    day_recs = _s8_records_for_date(records, date)
    closed = [r for r in day_recs if not s8_report.is_open(r)]
    if not closed:
        st.info("No closed round-trips for the selected session yet.")
        return

    agg = s8_report.compute_aggregates(day_recs)
    wr = agg["win_rate"]
    cards = [
        ("Number of closed round-trips", str(agg["closed_count"])),
        ("Win rate", f"{wr * 100:.0f}%" if wr is not None else "—"),
        ("Total profit/loss (dollars)",
         f"${agg['total_pnl']:,.0f}" if agg["total_pnl"] is not None else "—"),
        ("Average profit/loss per trade (dollars)",
         f"${agg['avg_pnl']:,.0f}" if agg["avg_pnl"] is not None else "—"),
        ("Average worst unrealized loss per trade (dollars)",
         f"${agg['avg_mae']:,.0f}" if agg["avg_mae"] is not None else "—"),
    ]
    cols = st.columns(len(cards))
    for col, (label, value) in zip(cols, cards):
        with col:
            st.markdown(T.card(label, value), unsafe_allow_html=True)

    fmt_n, fmt_k, fmt_s = s8_report._fmt_num, s8_report._fmt_strike, s8_report._fmt_signed
    rows = []
    for r in closed:
        e, x = r.entry, r.exit
        spot_move = None
        if e and x and e.entry_spot is not None and x.exit_spot is not None:
            spot_move = x.exit_spot - e.entry_spot
        sl = e.short_leg if e else None
        se = x.short_leg_exit if x else None
        rows.append({
            "Template name": r.template or "—",
            "Time slot": r.slot or "—",
            "Side (call or put spread)": r.side or "—",
            "Short strike / long strike":
                (f"{fmt_k(e.short_strike)}/{fmt_k(e.long_strike)}" if e else "—"),
            "Credit received at entry": fmt_n(e.realized_credit if e else None),
            "Why the trade closed": (x.exit_reason if x else "—"),
            "SPX move from entry to exit (points)": fmt_s(spot_move),
            "Profit/loss on the round-trip (dollars)":
                (f"{x.pnl:,.0f}" if x and x.pnl is not None else "—"),
            "Worst unrealized loss during the trade (max adverse excursion), in dollars":
                (f"{x.max_adverse_excursion:,.0f}"
                 if x and x.max_adverse_excursion is not None else "—"),
            "How long the trade was open":
                s8_report._fmt_duration(x.duration_secs if x else None),
            "Short-leg delta, entry then exit":
                (f"{fmt_n(sl.delta if sl else None, 3)} -> "
                 f"{fmt_n(se.delta if se else None, 3)}"),
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True,
                 height=400)


def _render_account(ib) -> None:
    st.markdown(T.section("Account / margin snapshot"), unsafe_allow_html=True)
    st.caption(
        "The live-trading account's own numbers (live-trading Gateway, port 4003 — a "
        "funded, transmit-capable test account; S8's pilot only reads it, never trades). "
        "Informational only.")
    if ib is None:
        st.info("Live-trading Gateway unreachable this cycle.")
        return
    try:
        summary = _s8_account_summary(ib)
    except Exception as exc:
        st.error(f"accountSummary() read failed: {type(exc).__name__}: {exc}")
        return
    bp, el = summary.get("BuyingPower"), summary.get("ExcessLiquidity")
    cards = [
        ("Account type", summary.get("AccountType") or "—"),
        ("Buying power (dollars)",
         f"${float(bp):,.0f}" if bp not in (None, "") else "—"),
        ("Excess liquidity (dollars)",
         f"${float(el):,.0f}" if el not in (None, "") else "—"),
    ]
    cols = st.columns(len(cards))
    for col, (label, value) in zip(cols, cards):
        with col:
            st.markdown(T.card(label, value), unsafe_allow_html=True)


def _render_connection_health(ib, connect_error, records: list) -> None:
    st.markdown(T.section("Connection health"), unsafe_allow_html=True)
    up = _port_open("127.0.0.1", 4003)   # cheap TCP probe, no live connection opened
    right = T.pill("Up and responding" if up else "Down (not listening)",
                   "good" if up else "bad", pulse=bool(up))
    st.markdown(T.row("Live-trading Gateway (port 4003)", right), unsafe_allow_html=True)
    if ib is not None:
        st.session_state["s8_last_live_probe"] = \
            datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    st.caption("Last successful dashboard live probe (this session): "
               f"{st.session_state.get('s8_last_live_probe', '—')}")

    last_capture = "—"
    for rec in records:            # records first-seen order; last wins
        if rec.entry and rec.entry.entry_ts:
            last_capture = rec.entry.entry_ts
    st.caption(f"Last captured trade entry (from store): {last_capture}")
    if connect_error is not None and ib is None:
        st.caption(f"(dashboard connect attempt this cycle failed: "
                   f"{type(connect_error).__name__}: {connect_error})")


# =========================== LIVE FRAGMENTS (auto-refresh) =====================
@st.fragment(run_every="300s")
def _fragment_headline() -> None:
    _render_headline()


@st.fragment(run_every="300s")
def _fragment_positions_monitor() -> None:
    """Open positions + live exit-monitor for the SELECTED session (recorded quotes
    only — NO Gateway contact). Reads the date selector back via session_state."""
    date = st.session_state.get("s8_view_date")
    today = datetime.now(tz=CT_ZONE).strftime("%Y%m%d")
    is_today = (date is None) or (date == today)
    session_date = date or today
    records = _s8_store_records()
    day_recs = _s8_records_for_date(records, date)
    open_recs = [r for r in day_recs if s8_report.is_open(r)]

    _render_open_positions(open_recs)
    st.divider()
    _render_live_monitor(open_recs, is_today, session_date=session_date)


@st.fragment(run_every="300s")
def _fragment_account_health() -> None:
    """Account/margin snapshot + connection health. This is the ONLY part that connects
    to the broker — read-only, launch=False, short timeout, disconnected in finally.
    Connects only for TODAY and only when there is something open to monitor."""
    date = st.session_state.get("s8_view_date")
    today = datetime.now(tz=CT_ZONE).strftime("%Y%m%d")
    is_today = (date is None) or (date == today)
    records = _s8_store_records()
    day_recs = _s8_records_for_date(records, date)
    open_recs = [r for r in day_recs if s8_report.is_open(r)]

    ib = None
    connect_error = None
    if is_today and open_recs:
        try:
            ib = _s8_connect_readonly_short()
        except Exception as exc:
            connect_error = exc
    try:
        _render_account(ib)
        st.divider()
        _render_connection_health(ib, connect_error, records)
    finally:
        if ib is not None:
            try:
                ib.disconnect()
            except Exception:
                pass


# ================================ PAGE ENTRY ===================================
def render_s8_full() -> None:
    """Render the full Strategy 8 page, top to bottom (see module docstring)."""
    _header()

    # 2. LIVE running P&L headline for TODAY (recorded quotes; auto-refreshing).
    st.markdown("<div style='height:.5rem'></div>", unsafe_allow_html=True)
    _fragment_headline()

    records = _s8_store_records()

    # 3. Today's schedule strip (all templates).
    _render_schedule(records)
    st.divider()

    # Session-date selector (default = most recent captured session, i.e. today when
    # live). Lives OUTSIDE the fragments (so it is not reset on refresh); the fragments
    # and the closed-round-trips panel read the selection back via session_state.
    dates = _s8_available_dates(records)
    chosen: str | None = None
    if dates:
        chosen = st.selectbox(
            "Session date", dates, index=0, key="s8_view_date",
            format_func=lambda d: f"{d[:4]}-{d[4:6]}-{d[6:]}",
        )
    else:
        st.info("No S8 trades captured yet — the store (trades.jsonl) is empty. This is "
                "the expected state before the first session; panels populate as the "
                "live service captures trades.")
    st.divider()

    # 4 + 5. Open positions + live exit-monitor (auto-refreshing, recorded quotes only).
    _fragment_positions_monitor()
    st.divider()

    # 6. Closed round-trips for the day + aggregates.
    _render_closed(records, chosen)
    st.divider()

    # 7 + 8. Account/margin snapshot + connection health (the only broker-connected part).
    _fragment_account_health()
