"""page_feeds.py — the "Feeds & Connections" page. READ-ONLY.

Answers the owner's plain question: is my data correct, coming in, and being
logged? Every value comes from deskfeeds.py (real reads, no mock data). The live
TRADING gateway (port 4003 — the future real-trading platform) is the headline;
then the market-data gateway (4001); then the live option-quote recording for the
Strategy 8 pilot; then the nightly end-of-day feeds. If a read fails, deskfeeds
returns a safe value and this page shows a plain message instead of crashing.

PLAIN-ENGLISH RULE (#1): every status is a full sentence; color only sorts it.
"""
from __future__ import annotations

import streamlit as st

import deskdata as dd
import deskfeeds as F
import theme as T


def render_feeds() -> None:
    ctx = dd.session_context()

    # ---- Header ----
    st.markdown(
        f"<div style='display:flex;align-items:baseline;gap:.8rem;flex-wrap:wrap'>"
        f"<span style='font-size:1.9rem;font-weight:700;color:{T.TEXT}'>"
        f"Feeds &amp; Connections</span>"
        f"<span style='font-size:12.5px;color:{T.MUTED}'>{ctx['central_time']}</span>"
        f"</div>",
        unsafe_allow_html=True,
    )
    st.markdown(
        f"<div style='font-size:12.5px;color:{T.MUTED};margin-top:.15rem'>"
        f"Is the data correct, coming in, and being recorded? Read-only — this "
        f"page never places, arms, or transmits any order.</div>",
        unsafe_allow_html=True,
    )

    gw = F.gateway_feed_focus()
    live = gw["live_trade"]
    data = gw["market_data"]
    ticks = F.s8_tick_feed_status()

    # ---- Headline: the LIVE trading gateway (4003) ----
    st.markdown(T.section("Live trading gateway — the future real-trading platform"),
                unsafe_allow_html=True)

    live_pulse = bool(live["up"] and gw["market_hours"])
    # Three plain sub-answers built from what we actually know.
    if live["up"]:
        connected_phrase = "Yes — connected and responding"
        connected_tier = "good"
    else:
        connected_phrase = "No — not connected right now"
        connected_tier = live["tier"]

    # "Receiving / recording data" is evidenced by the pilot's recorded quotes.
    if ticks["logging_now"]:
        recv_phrase = "Yes — live option quotes are arriving and being saved"
        recv_tier = "good"
        rec_phrase = "Yes — writing quotes to disk right now"
        rec_tier = "good"
    elif ticks["has_data"]:
        recv_phrase = ("Not at this moment — quotes were arriving earlier today "
                       "(see the last recorded time below)")
        recv_tier = "warn" if gw["market_hours"] else "unknown"
        rec_phrase = "Not at this moment — recording is paused"
        rec_tier = recv_tier
    else:
        recv_phrase = "No quotes recorded yet today (normal before the session opens)"
        recv_tier = "unknown"
        rec_phrase = "Not yet today"
        rec_tier = "unknown"

    last_tick = (f"{ticks['newest_phrase']}"
                 + (f" ({ticks['age_phrase']})" if ticks["age_phrase"] else "")) \
        if ticks["has_data"] else "no quote recorded yet today"

    c1, c2, c3 = st.columns(3)
    with c1:
        st.markdown(
            T.status_card("Is the live trading gateway connected?",
                          connected_tier,
                          "Connected" if live["up"] else "Not connected",
                          live["phrase"], pulse=live_pulse),
            unsafe_allow_html=True)
    with c2:
        st.markdown(
            T.status_card("Is it receiving market data?", recv_tier,
                          "Receiving data" if recv_tier == "good"
                          else "No data right now",
                          recv_phrase, pulse=bool(recv_tier == "good")),
            unsafe_allow_html=True)
    with c3:
        st.markdown(
            T.status_card("Is it recording to disk?", rec_tier,
                          "Recording now" if rec_tier == "good"
                          else "Not recording now",
                          f"Last quote recorded: {last_tick}. "
                          f"Quotes recorded today (in batches): "
                          f"{ticks['files_today']:,}.",
                          pulse=bool(rec_tier == "good")),
            unsafe_allow_html=True)

    st.markdown(
        T.row("Live trading gateway (port 4003)",
              T.pill(connected_phrase, connected_tier, pulse=live_pulse)),
        unsafe_allow_html=True)

    # ---- Market-data gateway (4001) ----
    st.markdown(T.section("Market-data gateway — feeds the evening data pulls"),
                unsafe_allow_html=True)
    st.markdown(
        T.row("Market-data gateway (port 4001)",
              T.pill("Connected and responding" if data["up"]
                     else "Not connected right now", data["tier"])),
        unsafe_allow_html=True)
    st.markdown(
        f"<div style='font-size:12px;color:{T.MUTED};margin:-.15rem 0 .4rem .2rem'>"
        f"{data['phrase']}</div>", unsafe_allow_html=True)

    # ---- Live option-quote recording (Strategy 8) ----
    st.markdown(T.section("Live option-quote recording (Strategy 8 pilot)"),
                unsafe_allow_html=True)

    if not ticks["has_data"]:
        st.markdown(
            T.row("Live option quotes recorded to disk",
                  T.pill("No quotes recorded yet today — normal before the "
                         "pilot session opens", "unknown")),
            unsafe_allow_html=True)
    else:
        st.markdown(
            T.row("Last option quote recorded",
                  T.pill(f"{ticks['newest_phrase']}"
                         + (f" — {ticks['age_phrase']}" if ticks["age_phrase"]
                            else ""),
                         "good" if ticks["logging_now"] else "unknown")),
            unsafe_allow_html=True)
        st.markdown(
            T.row("Quote batches recorded today",
                  T.pill(f"{ticks['files_today']:,} saved to disk today "
                         f"(about {ticks['records_estimate']:,} individual "
                         f"quote snapshots)", "info")),
            unsafe_allow_html=True)
        st.markdown(
            T.row("Currently recording to disk",
                  T.pill(ticks["logging_phrase"],
                         "good" if ticks["logging_now"] else
                         ("warn" if gw["market_hours"] else "unknown"),
                         pulse=bool(ticks["logging_now"]))),
            unsafe_allow_html=True)

    # ---- End-of-day data feeds ----
    st.markdown(T.section("End-of-day data feeds (the nightly pulls)"),
                unsafe_allow_html=True)
    eod = F.eod_feed_status()
    if not eod:
        st.markdown(
            f"<div style='color:{T.MUTED};font-size:12.5px'>The end-of-day feed "
            f"status files could not be read right now.</div>",
            unsafe_allow_html=True)
    else:
        for f in eod:
            st.markdown(T.row(f["label"], T.pill(f["phrase"], f["tier"])),
                        unsafe_allow_html=True)

    # ---- Footer legend ----
    st.markdown("<div style='height:.6rem'></div>", unsafe_allow_html=True)
    legend = (
        f"<div style='display:flex;gap:1.1rem;flex-wrap:wrap;font-size:11.5px;"
        f"color:{T.MUTED};border-top:1px solid {T.BORDER};padding-top:.6rem'>"
        f"<span><b style='color:{T.TIER['good']['c']}'>Green</b> = working as "
        f"expected</span>"
        f"<span><b style='color:{T.TIER['warn']['c']}'>Amber</b> = needs a look "
        f"soon</span>"
        f"<span><b style='color:{T.TIER['bad']['c']}'>Red</b> = needs attention "
        f"now</span>"
        f"<span><b style='color:{T.TIER['unknown']['c']}'>Grey</b> = not running "
        f"now / no data (often expected)</span>"
        f"</div>"
    )
    st.markdown(legend, unsafe_allow_html=True)
