"""placeholders.py — themed "Coming next" pages for the not-yet-built tabs.

Each renders a clean themed card describing, in plain English, what the page will
show. No data reads here — these are deliberately static.
"""
from __future__ import annotations

import streamlit as st

import theme as T


def _coming_next(title: str, blurb: str, bullets: list[str]) -> None:
    items = "".join(
        f"<li style='margin:.3rem 0;color:{T.TEXT}'>{b}</li>" for b in bullets
    )
    st.markdown(
        f"""
        <div class="dk-card" style="max-width:820px">
          <div style="display:inline-flex;align-items:center;gap:.5rem;
                      color:{T.ACCENT};font-size:11.5px;font-weight:700;
                      letter-spacing:.06em;text-transform:uppercase">
            <span class="dk-pill__dot" style="background:{T.ACCENT}"></span>
            Coming next
          </div>
          <div style="font-size:1.5rem;font-weight:700;color:{T.TEXT};
                      margin:.5rem 0 .4rem 0">{title}</div>
          <div style="font-size:13.5px;color:{T.MUTED};margin-bottom:.7rem">
            {blurb}
          </div>
          <div style="font-size:12px;color:{T.MUTED};font-weight:600;
                      text-transform:uppercase;letter-spacing:.05em;
                      margin-bottom:.2rem">This page will show</div>
          <ul style="margin:.2rem 0 0 1.1rem;padding:0;font-size:13.5px">{items}</ul>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_s0() -> None:
    _coming_next(
        "Strategy 0 — Adaptive All-Weather Core",
        "The full end-of-day view of the desk's core strategy, now in a gated, "
        "carefully-staged move toward real-money deployment.",
        [
            "Deploy state — where the staged, cash-gated real-money rollout stands "
            "right now, and what the next gate is",
            "Paper accounts — each paper sub-account's balance and holdings, read-only",
            "Drift versus target — how far each account has wandered from the model's "
            "intended mix, and the review-only rebalance plan",
            "Real-money gate status — a clear, honest statement that live transmission "
            "is still OFF and what it would take to arm it",
            "Backtest-versus-live performance — the validated model curve against the "
            "actual live paper results, side by side",
        ],
    )


def render_s8() -> None:
    _coming_next(
        "Strategy 8 — British Iron Condor (0-days-to-expiry pilot)",
        "The intraday monitor for the S8 pilot — a zero-real-order 'watch it happen' "
        "view of today's scheduled trades.",
        [
            "Today's trade schedule — the day's planned entries and which have fired",
            "Open positions — each live spread with its running profit or loss, "
            "computed from the pilot's own recorded quotes",
            "Closed trades — completed round-trips with their exit reason and result",
            "The pilot 'no real orders' wall — a plain confirmation that the strategy "
            "reports what it WOULD have done and transmits nothing",
            "Margin — the live account's own margin snapshot, read-only",
        ],
    )


def render_research() -> None:
    _coming_next(
        "Research shelf",
        "A compact, read-only status board for the strategies still in research — "
        "what's validated, what's in progress, and when each was last checked.",
        [
            "Strategy 4 — volatility-control overlay: validation status and last-checked date",
            "Strategy 5 — convexity (tail-risk) hedge: validation status and last-checked date",
            "Strategy 7 — iron condor research: validation status and last-checked date",
            "Each shelf item marked clearly as validated or still in progress, with its "
            "most recent review date",
        ],
    )
