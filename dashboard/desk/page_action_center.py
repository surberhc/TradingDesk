"""page_action_center.py — the desk's Action Center (propose-and-arm inbox) page.

Shows the plain-English notices in action_center.py, newest first, each with a Dismiss
button; dismissed history sits in an expander. READ-ONLY with respect to trading: a notice
only POINTS at another page (e.g. the Control Plane) — nothing here places, arms, or
transmits an order. The only write is marking a notice dismissed in the Action Center store.
"""
from __future__ import annotations

import streamlit as st

import action_center
import theme

# Friendly small-header label per notice kind (the status_card's `label`).
_KIND_LABEL = {
    "cash_deploy": "Idle cash — consider deploying",
}


def _tier(severity: str) -> str:
    s = (severity or "").lower()
    return s if s in ("good", "warn", "bad", "info") else "info"


def render_action_center() -> None:
    st.subheader("Action Center")
    st.caption(
        "Things that want your attention — proposals and heads-ups the desk surfaces for you "
        "to review. Nothing here trades on its own: each item just points you at the page "
        "where you can act (for a rebalance, the Control Plane). Dismiss an item once you've "
        "handled it."
    )

    notices = action_center.read_notices(include_dismissed=False)

    if not notices:
        st.markdown(
            theme.status_card(
                "Action Center",
                "good",
                "You're all caught up",
                "There are no open action items right now. New proposals (for example, idle "
                "cash worth deploying) will appear here when the desk finds them.",
            ),
            unsafe_allow_html=True,
        )
    else:
        st.markdown(theme.section(f"Open items ({len(notices)})"), unsafe_allow_html=True)
        for n in notices:
            body = n.get("body", "") or ""
            hint = n.get("action_hint", "") or ""
            sub = body + (f"\n\n{hint}" if hint else "")
            label = _KIND_LABEL.get(n.get("kind", ""), "Needs your review")
            st.markdown(
                theme.status_card(label, _tier(n.get("severity", "info")),
                                  n.get("title", "Notice"), sub),
                unsafe_allow_html=True,
            )
            st.caption(f"Raised {n.get('ts', '—')}")
            if st.button("Dismiss — I've handled this",
                         key=f"ac_dismiss_{n.get('notice_key')}"):
                action_center.dismiss(n.get("notice_key"))
                st.rerun()

    dismissed = [d for d in action_center.read_notices(include_dismissed=True)
                 if d.get("status") == "dismissed"]
    if dismissed:
        with st.expander(f"Dismissed items ({len(dismissed)})"):
            for d in dismissed:
                st.markdown(
                    f"**{theme._esc(str(d.get('title', '')))}** — "
                    f"{theme._esc(str(d.get('body', '')))}  \n"
                    f"_raised {theme._esc(str(d.get('ts', '—')))}, dismissed "
                    f"{theme._esc(str(d.get('dismissed_at') or '—'))}_"
                )
