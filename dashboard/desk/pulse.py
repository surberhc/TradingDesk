"""pulse.py — the "is my whole desk alive right now?" home page. READ-ONLY.

Every value on this page comes from deskdata.py (real reads, no mock data). If a
read fails, deskdata returns an honest safe value and this page shows a plain
message rather than crashing.
"""
from __future__ import annotations

import streamlit as st

import deskdata as dd
import theme as T


def _overall_desk_tier(gateways, freshness, s8, ctx) -> tuple[str, str]:
    """Green only if the things EXPECTED to be up are up. Returns (tier, phrase)."""
    problems: list[str] = []

    # Paper gateway should be up during weekday market hours.
    if ctx["market_hours"]:
        paper = next((g for g in gateways if g["port"] == 4002), None)
        if paper and not paper["up"]:
            problems.append("the paper trading gateway is down during market hours")

    # Any feed flagged red (missing / very stale) is a real problem.
    stale = [f["label"] for f in freshness if f["tier"] == "bad"]
    if stale:
        problems.append(f"{len(stale)} data feed(s) are stale or missing")

    if problems:
        return "warn", "Some things need a look: " + "; ".join(problems)
    return "good", "Everything expected to be running right now is running"


def render_pulse() -> None:
    ctx = dd.session_context()

    # ---- Header ----
    st.markdown(
        f"<div style='display:flex;align-items:baseline;gap:.8rem;flex-wrap:wrap'>"
        f"<span style='font-size:1.9rem;font-weight:700;color:{T.TEXT}'>Desk Pulse</span>"
        f"<span style='font-size:12.5px;color:{T.MUTED}'>{ctx['central_time']}</span>"
        f"</div>",
        unsafe_allow_html=True,
    )
    st.markdown(
        f"<div style='font-size:12.5px;color:{T.MUTED};margin-top:.15rem'>"
        f"Read-only — this dashboard never places, arms, or transmits any order.</div>",
        unsafe_allow_html=True,
    )

    # ---- Gather data once ----
    gateways = dd.gateway_status()
    freshness = dd.data_freshness()
    s8 = dd.s8_heartbeat()
    s0 = dd.s0_heartbeat()

    overall_tier, overall_phrase = _overall_desk_tier(gateways, freshness, s8, ctx)

    # ---- Top summary strip ----
    st.markdown("<div style='height:.5rem'></div>", unsafe_allow_html=True)
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.markdown(
            T.status_card("Overall desk state", overall_tier,
                          "Working as expected" if overall_tier == "good"
                          else "Needs a look",
                          overall_phrase),
            unsafe_allow_html=True)
    with c2:
        st.markdown(
            T.status_card(
                "Strategy 0 — Adaptive All-Weather Core", s0["tier"],
                "End-of-day strategy", s0["phrase"]),
            unsafe_allow_html=True)
    with c3:
        s8_live = s8.get("running") or s8.get("port_up")
        st.markdown(
            T.status_card(
                "Strategy 8 — British Iron Condor (0-days-to-expiry pilot)",
                s8["tier"],
                "Running now" if s8_live else "Pilot session closed",
                s8["phrase"], pulse=bool(s8_live)),
            unsafe_allow_html=True)
    with c4:
        st.markdown(
            T.status_card("Market session right now", ctx["tier"],
                          "Markets open" if ctx["market_hours"] else "Markets closed",
                          ctx["phrase"], pulse=bool(ctx["market_hours"])),
            unsafe_allow_html=True)

    # ---- Today's results tiles (LAZY imports keep the cost off module load) ----
    # These two tiles answer "how are we doing today?" in plain English. They wrap
    # onto their own line on a phone (st.columns stacks below ~640px).
    import page_s8 as _s8page
    import page_s0 as _s0page

    s8_pnl = _s8page.s8_today_pnl()
    s0_pnl = _s0page.s0_pnl_summary()

    st.markdown("<div style='height:.35rem'></div>", unsafe_allow_html=True)
    r1, r2 = st.columns(2)
    with r1:
        n = s8_pnl.get("open_count", 0)
        pos_word = "position" if n == 1 else "positions"
        if s8_pnl.get("live") and s8_pnl.get("pnl") is not None:
            pnl = s8_pnl["pnl"]
            tier = "good" if pnl >= 0 else "bad"
            sign = "+" if pnl >= 0 else "-"
            big = (f"Today's running profit/loss: {sign}${abs(pnl):,.0f} across "
                   f"{n} open {pos_word}")
            sub = (f"Live from the pilot's own recorded quotes (zero-transmit pilot — "
                   f"no real orders). As of {s8_pnl.get('as_of', '—')}.")
            do_pulse = True
        else:
            tier = "unknown"
            do_pulse = False
            if n:
                big = "Today's running profit/loss: waiting on fresh recorded quotes"
                sub = (f"{n} open {pos_word}, but no fresh recorded quote this cycle. "
                       f"As of {s8_pnl.get('as_of', '—')}.")
            else:
                big = "Today's running profit/loss: no open positions right now"
                sub = (f"Nothing open to value yet — this fills in as the pilot opens "
                       f"positions. As of {s8_pnl.get('as_of', '—')}.")
        st.markdown(
            T.status_card(
                "Strategy 8 — British Iron Condor (0-days-to-expiry pilot), today",
                tier, big, sub, pulse=do_pulse),
            unsafe_allow_html=True)
    with r2:
        change = s0_pnl.get("change_pct")
        if change is not None:
            tier = "good" if change >= 0 else "bad"
            sign = "+" if change >= 0 else "-"
            big = f"{sign}{abs(change):.2f}% change in total paper value"
        else:
            tier = "unknown"
            big = "Not enough history yet to show a change"
        st.markdown(
            T.status_card(
                "Strategy 0 — Adaptive All-Weather Core (slow, end-of-day)",
                tier, big, s0_pnl.get("note", "")),
            unsafe_allow_html=True)

    # ---- Trading gateways ----
    st.markdown(T.section("Trading gateways"), unsafe_allow_html=True)
    for g in gateways:
        right = T.pill(
            "Connected and responding" if g["up"] else g["phrase"],
            "good" if g["up"] else g["tier"],
            pulse=bool(g["up"] and g["port"] in (4002, 4003) and ctx["market_hours"]),
        )
        st.markdown(T.row(g["label"], right), unsafe_allow_html=True)

    # ---- Live automation ----
    st.markdown(T.section("Live automation (running as scheduled)"),
                unsafe_allow_html=True)
    groups = dd.live_tasks()
    any_task_info = any(t["state"] != "not found"
                        for grp in groups for t in grp["tasks"])
    if not any_task_info:
        st.markdown(
            f"<div style='color:{T.MUTED};font-size:12.5px'>Windows scheduled-task "
            f"states are unavailable on this machine right now.</div>",
            unsafe_allow_html=True)
    else:
        for grp in groups:
            st.markdown(
                f"<div style='font-size:12px;color:{T.MUTED};font-weight:600;"
                f"margin:.7rem 0 .35rem 0'>{grp['group']}</div>",
                unsafe_allow_html=True)
            for t in grp["tasks"]:
                right = T.pill(t["phrase"], t["tier"],
                               pulse=(t["tier"] == "good" and "Running now" in t["phrase"]))
                st.markdown(T.row(t["desc"], right), unsafe_allow_html=True)

    # ---- Data freshness ----
    st.markdown(T.section("Data freshness"), unsafe_allow_html=True)
    for f in freshness:
        st.markdown(T.row(f["label"], T.pill(f["phrase"], f["tier"])),
                    unsafe_allow_html=True)

    # ---- Retired / intentionally disabled ----
    retired = dd.retired_tasks()
    with st.expander("Intentionally disabled (retired) automation"):
        st.markdown(
            f"<div style='color:{T.MUTED};font-size:12px;margin-bottom:.5rem'>"
            f"These are switched off on purpose — nothing here is broken.</div>",
            unsafe_allow_html=True)
        for r in retired:
            st.markdown(
                T.row(r["reason"],
                      T.pill("Turned off on purpose (disabled)", "unknown"),
                      meta=r["name"]),
                unsafe_allow_html=True)

    # ---- Footer legend ----
    st.markdown("<div style='height:.6rem'></div>", unsafe_allow_html=True)
    legend = (
        f"<div style='display:flex;gap:1.1rem;flex-wrap:wrap;font-size:11.5px;"
        f"color:{T.MUTED};border-top:1px solid {T.BORDER};padding-top:.6rem'>"
        f"<span><b style='color:{T.TIER['good']['c']}'>Green</b> = working as expected</span>"
        f"<span><b style='color:{T.TIER['warn']['c']}'>Amber</b> = needs a look soon</span>"
        f"<span><b style='color:{T.TIER['bad']['c']}'>Red</b> = needs attention now</span>"
        f"<span><b style='color:{T.TIER['unknown']['c']}'>Grey</b> = unknown / no data</span>"
        f"</div>"
    )
    st.markdown(legend, unsafe_allow_html=True)
