"""pulse.py — the "is my whole desk alive right now?" home page. READ-ONLY.

Every value on this page comes from deskdata.py (real reads, no mock data). If a
read fails, deskdata returns an honest safe value and this page shows a plain
message rather than crashing.

The glance view is deliberately tiny: a two-line header, three summary tiles, four
collapsed expanders (detail on demand), and a colour legend. Each element states
the actual thing ONCE, in short plain English — no stacked status words.
"""
from __future__ import annotations

import streamlit as st

import deskdata as dd
import theme as T


def render_pulse() -> None:
    ctx = dd.session_context()

    # ---- Gather data once (cheap, cached reads) ----
    gateways = dd.gateway_status()
    freshness = dd.data_freshness()
    groups = dd.live_tasks()
    s8 = dd.s8_heartbeat()
    s0 = dd.s0_heartbeat()

    # ---- Header (compact: title + time + session tag, then the read-only line) ----
    session_tag = "markets open" if ctx["market_hours"] else "markets closed"
    st.markdown(
        f"<div style='display:flex;align-items:baseline;gap:.8rem;flex-wrap:wrap'>"
        f"<span style='font-size:1.9rem;font-weight:700;color:{T.TEXT}'>Desk Pulse</span>"
        f"<span style='font-size:12.5px;color:{T.MUTED}'>"
        f"{ctx['central_time']} &middot; {session_tag}</span>"
        f"</div>",
        unsafe_allow_html=True,
    )
    st.markdown(
        f"<div style='font-size:12.5px;color:{T.MUTED};margin-top:.15rem'>"
        f"Read-only — never places, arms, or transmits an order.</div>",
        unsafe_allow_html=True,
    )

    # ---- Today's P&L (LAZY imports keep the cost off module load) ----
    import page_s8 as _s8page
    import page_s0 as _s0page

    s8_pnl = _s8page.s8_today_pnl()
    s0_pnl = _s0page.s0_pnl_summary()

    # ================= THREE summary tiles (stack on mobile) ================= #
    st.markdown("<div style='height:.5rem'></div>", unsafe_allow_html=True)
    t1, t2, t3 = st.columns(3)

    # --- (a) Strategy 8 — heartbeat + today's P&L, merged into one --- #
    with t1:
        s8_live = bool(s8.get("running") or s8.get("port_up"))
        if s8_live and s8_pnl.get("live") and s8_pnl.get("pnl") is not None:
            pnl = s8_pnl["pnl"]
            sign = "+" if pnl >= 0 else "-"
            big = f"Running · {sign}${abs(pnl):,.0f} today"
        elif s8_live:
            big = "Running · profit/loss updating"
        else:
            big = "Session closed"

        n_open = s8.get("open_now", 0)
        if n_open:
            pos_word = "position" if n_open == 1 else "positions"
            sub = f"{n_open} {pos_word} open · 0 real orders sent (pilot)"
        else:
            sub = "No positions open right now · 0 real orders (pilot)"

        st.markdown(
            T.status_card(
                "Strategy 8 — 0-days-to-expiry pilot",
                "good" if s8_live else "unknown",
                big, sub, pulse=s8_live),
            unsafe_allow_html=True)

    # --- (b) Strategy 0 — the steady paper-only truth --- #
    with t2:
        tiingo_date = s0.get("tiingo_date")
        through = f" · data through {tiingo_date}" if tiingo_date else ""
        change = s0_pnl.get("change_pct")
        if change is not None:
            sign = "+" if change >= 0 else "-"
            sub = f"{sign}{abs(change):.1f}% month-to-date{through}"
        else:
            sub = f"Tracking since Jul 7{through}"
        st.markdown(
            T.status_card(
                "Strategy 0 — adaptive all-weather",
                "info", "Paper only — real-money OFF", sub),
            unsafe_allow_html=True)

    # --- (c) Systems — one health read across gateways, feeds, automation --- #
    with t3:
        problems: list[dict] = []
        # Any data feed flagged bad (stale / missing).
        n_stale = sum(1 for f in freshness if f["tier"] == "bad")
        if n_stale:
            fw = "feed" if n_stale == 1 else "feeds"
            problems.append(
                {"text": f"{n_stale} data {fw} stale or missing", "tier": "warn"})
        # Any live automation job reporting an error.
        n_bad_tasks = sum(1 for grp in groups for t in grp["tasks"]
                          if t["tier"] == "bad")
        if n_bad_tasks:
            jw = "job" if n_bad_tasks == 1 else "jobs"
            problems.append(
                {"text": f"{n_bad_tasks} automation {jw} reported an error",
                 "tier": "bad"})
        # The live trading gateway is a real problem only when it's down DURING
        # the pilot session (its tier goes "bad"). Paper / market-data never are.
        live_trade_gw = next((g for g in gateways if g["port"] == 4003), None)
        if live_trade_gw and live_trade_gw["tier"] == "bad":
            problems.append(
                {"text": "the live trading gateway is down during the pilot session",
                 "tier": "bad"})

        if not problems:
            live_trade_up = bool(live_trade_gw and live_trade_gw["up"])
            facts = []
            if live_trade_up:
                facts.append("live trading gateway connected")
            if n_stale == 0:
                facts.append("all data feeds current")
            if n_bad_tasks == 0:
                facts.append("automation on schedule")
            sub = " · ".join(facts)
            sub = sub[:1].upper() + sub[1:] if sub else ""
            st.markdown(
                T.status_card("Systems", "good", "All normal", sub),
                unsafe_allow_html=True)
        else:
            # Worst problem first (an automation error outranks the amber kinds).
            problems.sort(key=lambda p: 0 if p["tier"] == "bad" else 1)
            worst = problems[0]
            others = problems[1:]
            sub = ("Also: " + "; ".join(p["text"] for p in others)) if others else ""
            st.markdown(
                T.status_card("Systems", worst["tier"], worst["text"], sub),
                unsafe_allow_html=True)

    # ==================== FOUR expanders — detail on demand =================== #
    st.markdown("<div style='height:.35rem'></div>", unsafe_allow_html=True)

    # --- Trading gateways (always visible — three short statuses at a glance) --- #
    st.markdown(T.section("Trading gateways"), unsafe_allow_html=True)
    for g in gateways:
        right = T.pill(
            g["phrase"], g["tier"],
            pulse=bool(g["up"] and g["port"] in (4002, 4003)
                       and ctx["market_hours"]),
        )
        st.markdown(T.row(g["label"], right, meta=g.get("context", "")),
                    unsafe_allow_html=True)

    # --- Live automation --- #
    any_task_info = any(t["state"] != "not found"
                        for grp in groups for t in grp["tasks"])
    total_tasks = sum(len(grp["tasks"]) for grp in groups)
    running_now = sum(1 for grp in groups for t in grp["tasks"]
                      if "Running now" in t["phrase"])
    bad_tasks = sum(1 for grp in groups for t in grp["tasks"]
                    if t["tier"] == "bad")
    if not any_task_info:
        auto_header = "Live automation — scheduler status unavailable"
    elif bad_tasks:
        jw = "job" if bad_tasks == 1 else "jobs"
        auto_header = (f"Live automation — {bad_tasks} {jw} need a look, "
                       f"{running_now} running now")
    else:
        auto_header = (f"Live automation — {total_tasks} jobs, all on schedule "
                       f"({running_now} running now)")
    with st.expander(auto_header, expanded=False):
        if not any_task_info:
            st.markdown(
                f"<div style='color:{T.MUTED};font-size:12.5px'>Windows "
                f"scheduled-task states are unavailable on this machine right now.</div>",
                unsafe_allow_html=True)
        else:
            for grp in groups:
                st.markdown(
                    f"<div style='font-size:12px;color:{T.MUTED};font-weight:600;"
                    f"margin:.7rem 0 .35rem 0'>{grp['group']}</div>",
                    unsafe_allow_html=True)
                for t in grp["tasks"]:
                    right = T.pill(
                        t["phrase"], t["tier"],
                        pulse=(t["tier"] == "good" and "Running now" in t["phrase"]))
                    st.markdown(T.row(t["desc"], right), unsafe_allow_html=True)

    # --- Data freshness --- #
    n_feeds = len(freshness)
    n_bad_feeds = sum(1 for f in freshness if f["tier"] == "bad")
    _sched_note = "all pull nightly after the close (about 5:30–9:00 PM Central)"
    if n_bad_feeds == 0:
        feed_header = (f"Data freshness — all {n_feeds} feeds current · "
                       f"{_sched_note}")
    else:
        feed_header = (f"Data freshness — {n_bad_feeds} of {n_feeds} "
                       f"feeds stale or missing · {_sched_note}")
    with st.expander(feed_header, expanded=False):
        for f in freshness:
            st.markdown(
                T.row(f["label"], T.pill(f["phrase"], f["tier"]),
                      meta=f.get("schedule", "")),
                unsafe_allow_html=True)

    # --- Retired / intentionally disabled --- #
    with st.expander("Intentionally disabled (retired) automation", expanded=False):
        st.markdown(
            f"<div style='color:{T.MUTED};font-size:12px;margin-bottom:.5rem'>"
            f"These are switched off on purpose — nothing here is broken.</div>",
            unsafe_allow_html=True)
        for r in dd.retired_tasks():
            st.markdown(
                T.row(r["reason"],
                      T.pill("Turned off on purpose (disabled)", "unknown"),
                      meta=r["name"]),
                unsafe_allow_html=True)

    # ---- Footer legend (one line) ----
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
