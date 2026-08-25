"""desk_app.py — the rebuilt, isolated Trading Desk dashboard (port 8502). READ-ONLY.

A ground-up rebuild of the desk monitor, deliberately separate from the live
dashboard/app.py (port 8501) so it can grow without touching what's already
running. Same read-only guarantees: it never places, arms, or transmits an order,
never writes to any store/warehouse/config. Gateway reads are cheap TCP port
probes only (deskdata.py) — no ib_async connection is opened on the Pulse page.

Multipage app via st.navigation / st.Page (function-page callables). A persistent,
guarded emergency control strip (Halt + inert Flatten) renders at the TOP of every
page, before the page body runs, wrapped so a bar error can never take the app down:
  1. Desk Pulse (home)                        — pulse.render_pulse
  2. Action Center (propose-and-arm inbox)     — page_action_center.render_action_center
  2. Feeds & Connections                      — page_feeds.render_feeds
  3. History & Event Log                       — page_history.render_history
  4. Strategy 0 — Adaptive All-Weather Core    — page_s0.render_s0_full
  5. Strategy 8 — British Iron Condor (0DTE)   — page_s8.render_s8_full
  6. Research shelf                            — page_research.render_research_full

Every page module's top-level imports are CHEAP (streamlit, theme, and pure data
modules); every heavy/broker import inside them is lazy, so importing the page
modules here opens no socket and runs no backtest.
"""
from __future__ import annotations

import sys
from pathlib import Path

# --- Bootstrap sys.path exactly like app.py (reuse the existing packages) ------
REPO = Path(__file__).resolve().parents[2]
for sub in ("paperbot", "backtester", "connections", "strategies",
            "dailyreport", "livebot"):
    p = REPO / sub
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
# connections is a namespace package one level deeper (mirror app.py).
_conn = REPO / "connections"
if str(_conn) not in sys.path:
    sys.path.insert(0, str(_conn))

import streamlit as st

st.set_page_config(page_title="Trading Desk — Pulse", page_icon="🩺",
                   layout="wide")

import theme as T

T.inject_theme()

# Page modules (cheap at import — heavy/broker imports inside them are lazy) and
# the persistent emergency control strip.
import emergency
import page_action_center
import page_control_plane
import page_custom_alloc
import page_feeds
import page_history
import page_models
import page_research
import page_s0
import page_s0_model
import page_s8
import pulse

# --- Persistent emergency bar on EVERY page --------------------------------- #
# Rendered after theme injection and before the selected page runs, so it always
# sits at the top. Wrapped so a bar error can never take the whole app down —
# a small plain-English notice is shown instead.
try:
    emergency.render_emergency_bar()
except Exception as exc:  # noqa: BLE001 — the bar must never crash the app
    st.warning(
        "The emergency control strip could not be shown right now "
        f"({type(exc).__name__}). The rest of the dashboard is unaffected, and "
        "nothing about trading has changed — this bar only stops software, it "
        "never places or transmits any order."
    )

# Action Center unread badge — a cheap SQLite count; wrapped so it can never break nav.
try:
    import action_center
    _ac_unread = action_center.unread_count()
except Exception:  # noqa: BLE001 — the badge must never take the app down
    _ac_unread = 0
_ac_title = "Action Center" + (f" ({_ac_unread})" if _ac_unread else "")

pages = [
    st.Page(pulse.render_pulse, title="Desk Pulse", icon="🩺", default=True),
    st.Page(page_action_center.render_action_center, title=_ac_title, icon="🔔"),
    st.Page(page_feeds.render_feeds, title="Feeds & Connections", icon="📡"),
    st.Page(page_history.render_history, title="History & Event Log", icon="📜"),
    st.Page(page_s0.render_s0_full,
            title="Strategy 0 — Adaptive All-Weather Core", icon="📈"),
    st.Page(page_s0_model.render_s0_model,
            title="Strategy 0 — Model & Parameters", icon="📋"),
    st.Page(page_models.render_models,
            title="Strategy Models — all models", icon="🗂️"),
    st.Page(page_custom_alloc.render_custom_alloc,
            title="Custom allocation — models Andrew writes himself", icon="✍️"),
    st.Page(page_control_plane.render_control_plane,
            title="Control Plane — S0 rebalance", icon="🎛️"),
    st.Page(page_s8.render_s8_full,
            title="Strategy 8 — British Iron Condor (0DTE)", icon="🎯"),
    st.Page(page_research.render_research_full, title="Research shelf", icon="🔬"),
]

st.navigation(pages).run()
