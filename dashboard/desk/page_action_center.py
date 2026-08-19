"""page_action_center.py — the desk's Action Center (propose-and-arm inbox) page.

Shows the plain-English notices in action_center.py, newest first, each with a Dismiss button
and an "Ignore for N days" (snooze) control; dismissed history and currently-snoozed items sit
in their own expanders. A notice may carry structured detail (detail_json) — e.g. the list of
out-of-spec accounts — rendered in an expandable table. READ-ONLY with respect to trading: a
notice only POINTS at another page (e.g. the Control Plane) — nothing here places, arms, or
transmits an order. The only writes are marking a notice dismissed / snoozed in the store.
"""
from __future__ import annotations

import json

import streamlit as st

import action_center
import theme

# Friendly small-header label per notice kind (the status_card's `label`).
_KIND_LABEL = {
    "cash_deploy": "Idle cash — consider deploying",
    "outofspec": "Accounts out of spec — rebalance proposed",
}

# The snooze / "ignore for N days" options the owner chose.
_SNOOZE_DAYS = [5, 10, 30]

# detail_json field -> plain-English column header for the expandable detail table.
_DETAIL_COLS = {
    "account": "Account",
    "model": "Model",
    "advisor": "Advisor",
    "net_liq": "Account value",
    "managed_net_liq": "Value the model manages",
    "n_legs": "Trades to conform",
    "n_held_aside": "Holdings we never trade",
    "held_aside_value": "Value we never trade",
    "n_unclassified": "Holdings we could not identify",
    "held_back": "All trades held back",
}


def _tier(severity: str) -> str:
    s = (severity or "").lower()
    return s if s in ("good", "warn", "bad", "info") else "info"


def _render_detail(n: dict) -> None:
    """If a notice carries detail_json, render it as an expandable plain-English table."""
    raw = n.get("detail_json")
    if not raw:
        return
    try:
        rows = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return
    if not isinstance(rows, list) or not rows:
        return
    with st.expander(f"Show the {len(rows)} accounts"):
        display = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            nav = r.get("net_liq")
            managed = r.get("managed_net_liq")
            held_val = r.get("held_aside_value")
            display.append({
                "Account": r.get("account", ""),
                "Model": r.get("model", "") or "",
                "Advisor": r.get("advisor", "") or "",
                "Account value": (f"${float(nav):,.0f}"
                                  if isinstance(nav, (int, float)) else ""),
                # What the model's 100% actually applies to. Holdings the desk never
                # trades (individual bonds) sit outside the allocation, so this is the
                # number the "Trades to conform" column rebalances.
                "Value the model manages": (f"${float(managed):,.0f}"
                                            if isinstance(managed, (int, float)) else ""),
                "Trades to conform": r.get("n_legs", ""),
                "Holdings we never trade": r.get("n_held_aside", ""),
                "Value we never trade": (f"${float(held_val):,.0f}"
                                         if isinstance(held_val, (int, float)) else ""),
                "Holdings we could not identify": r.get("n_unclassified", ""),
                "All trades held back": "Yes" if r.get("held_back") else "",
            })
        st.dataframe(display, use_container_width=True, hide_index=True)


def _render_controls(n: dict) -> None:
    """Dismiss + Ignore-for-N-days controls for one open notice. Snooze keys on the notice's
    dedup_key (the family key the poster de-dups on), so it silences that daily re-nag."""
    dedup = n.get("dedup_key") or ""
    nk = n.get("notice_key")
    # Keys carry the dedup_key so each notice's controls are individually addressable.
    tag = dedup or nk
    c1, c2, c3 = st.columns([2, 2, 3])
    with c1:
        if st.button("Dismiss — I've handled this", key=f"ac_dismiss_{tag}"):
            action_center.dismiss(nk)
            st.rerun()
    if dedup:
        with c2:
            days = st.selectbox(
                "Ignore this for", _SNOOZE_DAYS, index=0,
                format_func=lambda d: f"{d} days", key=f"ac_snooze_days_{tag}",
                label_visibility="collapsed",
            )
        with c3:
            if st.button(f"Ignore for {days} days", key=f"ac_snooze_{tag}"):
                action_center.snooze(dedup, int(days))
                st.rerun()


def render_action_center() -> None:
    st.subheader("Action Center")
    st.caption(
        "Things that want your attention — proposals and heads-ups the desk surfaces for you "
        "to review. Nothing here trades on its own: each item just points you at the page "
        "where you can act (for a rebalance, the Control Plane). Dismiss an item once you've "
        "handled it, or Ignore it for a set number of days to stop the daily reminder."
    )

    notices = action_center.read_notices(include_dismissed=False)

    if not notices:
        st.markdown(
            theme.status_card(
                "Action Center",
                "good",
                "You're all caught up",
                "There are no open action items right now. New proposals (for example, idle "
                "cash worth deploying, or accounts drifting out of spec) will appear here when "
                "the desk finds them.",
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
            _render_detail(n)
            _render_controls(n)

    # Currently snoozed / ignored items — visible so the operator can un-snooze early.
    snoozed = action_center.read_snoozed()
    if snoozed:
        with st.expander(f"Ignored / snoozed items ({len(snoozed)})"):
            for s in snoozed:
                st.markdown(
                    f"**{theme._esc(str(s.get('title', '')))}** — ignored until "
                    f"**{theme._esc(str(s.get('snoozed_until') or '—'))}**"
                )
                dedup = s.get("dedup_key") or ""
                if dedup and st.button("Un-ignore — show this again now",
                                       key=f"ac_unsnooze_{s.get('notice_key')}"):
                    action_center.unsnooze(dedup)
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
