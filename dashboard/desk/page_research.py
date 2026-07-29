"""page_research.py — the rebuilt desk's "research shelf". READ-ONLY, static.

A compact shelf of the strategies that are RESEARCHED but NOT running live. Nothing
here reads live data, connects to a broker, or makes any decision — these strategies
are not deployed, so there is nothing live to show. Each is a plain-English themed
status card describing what it is and exactly where it stands.

All imports are cheap (streamlit, theme). Importing this module opens no socket and
runs nothing.
"""
from __future__ import annotations

import streamlit as st

import theme


# Each entry: (title, colour-tier, one-line status phrase, longer plain description).
_SHELF = [
    (
        "Strategy 4 — Volatility-Control Fund",
        "info",
        "Built and validated — not deployed",
        "A single-asset volatility-targeting fund that dials one stock exposure up and "
        "down against cash to hold a steady level of risk — a replica of a fixed-index / "
        "registered-index-linked annuity (FIA/RILA). Status: BUILT and validated against "
        "the actual SEC filings (2026-06-28). Not deployed yet — the paper-deploy account "
        "for it is still undecided.",
    ),
    (
        "Strategy 5 — Financed Convexity Overlay",
        "info",
        "Defensive half validated — offensive half blocked",
        "A permanent, self-financed tail hedge sitting on top of a synthetic S&P 500 core "
        "— the hedge is meant to pay for itself over time. Status: the DEFENSIVE half is "
        "validated on end-of-day data; the OFFENSIVE / harvest half is blocked because it "
        "needs intraday option data that is no longer available.",
    ),
    (
        "Strategy 7 — 45-Day Managed Iron Condor",
        "info",
        "Research in progress — protocol preregistered",
        "A monthly-style, defined-risk options income strategy (an iron condor) opened "
        "roughly 45 days out and laddered in weekly slices to spread out timing. Status: "
        "research in progress, with a preregistered test protocol already committed so "
        "the results can't be curve-fit after the fact.",
    ),
]


def render_research_full() -> None:
    """Render the research shelf — the strategies that are researched but NOT live."""
    st.subheader("Research shelf")
    st.caption("Strategies we have researched but are NOT running live. Nothing here "
               "trades, connects, or reads live data — these are status notes only.")

    for title, tier, status_phrase, description in _SHELF:
        st.markdown(
            theme.status_card(title, tier, status_phrase, description),
            unsafe_allow_html=True,
        )
