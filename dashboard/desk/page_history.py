"""page_history.py — the "History & Event Log" page. READ-ONLY to the trading
system; it only ever reads/writes the dashboard's OWN audit database (events.db).

A permanent, plain-English record of everything that happened on the desk —
morning logins, session start-ups, gateway downtime (with how long and why), and
the pilot's would-have-traded decisions. On each visit it calls eventlog.scan()
(best-effort, wrapped) to fold any new log lines into the durable store, then shows
the accumulated history grouped by day. Because the store is permanent, the record
keeps growing even after the raw logs roll off disk.

PLAIN-ENGLISH RULE (#1): every row is a full sentence; the colored dot only sorts
it by how much attention it wants.
"""
from __future__ import annotations

from datetime import datetime

import streamlit as st

import eventlog as EL
import theme as T

# Event severity -> theme tier (color only; the text always stands alone).
_SEV_TIER = {"good": "good", "warn": "warn", "bad": "bad",
             "info": "info", "unknown": "unknown"}


def _day_label(day: str) -> str:
    """'YYYYMMDD' -> 'Wednesday, July 29, 2026' (plain). Falls back to the raw
    string if it cannot be parsed."""
    try:
        d = datetime.strptime(day, "%Y%m%d")
    except (ValueError, TypeError):
        return str(day)
    today = datetime.now().strftime("%Y%m%d")
    # Build the day-of-month without a zero pad (%-d is not portable on Windows).
    base = f"{d.strftime('%A, %B')} {d.day}, {d.year}"
    if day == today:
        base += "  —  today"
    return base


def _time_label(ts: str) -> str:
    """'YYYY-MM-DD HH:MM:SS' -> '9:00 PM Central' (plain 12-hour)."""
    try:
        dt = datetime.strptime(str(ts)[:19], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return str(ts)
    hour12 = dt.hour % 12 or 12
    ampm = "AM" if dt.hour < 12 else "PM"
    return f"{hour12}:{dt.minute:02d} {ampm} Central"


def render_history() -> None:
    # ---- Fold any new log lines into the durable store (best-effort) ----
    try:
        EL.scan()
    except Exception:
        pass  # the page still shows whatever is already stored

    # ---- Header ----
    st.markdown(
        f"<div style='font-size:1.9rem;font-weight:700;color:{T.TEXT}'>"
        f"History &amp; Event Log</div>",
        unsafe_allow_html=True,
    )
    st.markdown(
        f"<div style='font-size:12.5px;color:{T.MUTED};margin-top:.15rem;"
        f"max-width:900px'>A permanent, plain-English record of everything that "
        f"happened on the desk — morning logins, session start-ups, any gateway "
        f"downtime (with how long it lasted and why), and every trade decision the "
        f"pilot logged (it never sends a real order). This record keeps growing "
        f"over time, even after the raw logs are cleared off the machine.</div>",
        unsafe_allow_html=True,
    )

    days = EL.available_days()
    if not days:
        st.markdown("<div style='height:.6rem'></div>", unsafe_allow_html=True)
        st.markdown(
            f"<div class='dk-card' style='max-width:720px'>"
            f"<div class='dk-card__value'>No events recorded yet.</div>"
            f"<div class='dk-card__sub'>As the Strategy 8 pilot runs and the "
            f"nightly tasks fire, this log will fill in on its own — check back "
            f"after the next session.</div></div>",
            unsafe_allow_html=True)
        return

    # ---- Day selector (default: most recent) ----
    st.markdown("<div style='height:.4rem'></div>", unsafe_allow_html=True)
    labels = {d: _day_label(d) for d in days}
    choice = st.selectbox(
        "Pick a day to view", options=days,
        format_func=lambda d: labels.get(d, d), index=0,
    )

    events = EL.read_events(day=choice, limit=500)
    day_title = labels.get(choice, choice)

    st.markdown(T.section(day_title), unsafe_allow_html=True)
    if not events:
        st.markdown(
            f"<div style='color:{T.MUTED};font-size:12.5px'>No events recorded on "
            f"this day.</div>", unsafe_allow_html=True)
        return

    for ev in events:
        tier = _SEV_TIER.get(ev.get("severity", "info"), "unknown")
        time_txt = _time_label(ev.get("ts", ""))
        source = ev.get("source", "")
        # The pill carries the time (+ a colored dot); the row body is the full
        # plain-English sentence, with the source as quiet meta underneath.
        st.markdown(
            T.row(ev.get("message", ""),
                  T.pill(time_txt, tier),
                  meta=source),
            unsafe_allow_html=True)

    # ---- Footer legend ----
    st.markdown("<div style='height:.6rem'></div>", unsafe_allow_html=True)
    legend = (
        f"<div style='display:flex;gap:1.1rem;flex-wrap:wrap;font-size:11.5px;"
        f"color:{T.MUTED};border-top:1px solid {T.BORDER};padding-top:.6rem'>"
        f"<span><b style='color:{T.TIER['good']['c']}'>Green</b> = went as "
        f"expected</span>"
        f"<span><b style='color:{T.TIER['warn']['c']}'>Amber</b> = a hiccup worth "
        f"knowing about</span>"
        f"<span><b style='color:{T.TIER['bad']['c']}'>Red</b> = a problem</span>"
        f"<span><b style='color:{T.TIER['info']['c']}'>Blue</b> = an ordinary "
        f"logged event (including pilot trade decisions)</span>"
        f"</div>"
    )
    st.markdown(legend, unsafe_allow_html=True)
