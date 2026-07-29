"""theme.py — the rebuilt Trading Desk's design system (STYLING ONLY).

Nothing here reads data or makes any decision. Every function returns an HTML
string; the caller renders it with st.markdown(..., unsafe_allow_html=True).

IMPORTANT (the #1 rule): TIER carries COLOR only. There is no generic tier word
rendered anywhere — callers always pass a full plain-English phrase for the
label/value. "good/warn/bad/unknown/info" pick a color; they are never shown.
"""
from __future__ import annotations

import html as _html

import streamlit as st

BG = "#0d1117"; SURFACE = "#161b22"; SURFACE_2 = "#1c232c"; BORDER = "#2a323d"
TEXT = "#e6edf3"; MUTED = "#8b949e"; ACCENT = "#3b82f6"
TIER = {
    "good": {"c": "#3fb950", "bg": "rgba(63,185,80,0.14)"},
    "warn": {"c": "#d29922", "bg": "rgba(210,153,34,0.15)"},
    "bad": {"c": "#f85149", "bg": "rgba(248,81,73,0.15)"},
    "unknown": {"c": "#6e7681", "bg": "rgba(110,118,129,0.15)"},
    "info": {"c": "#58a6ff", "bg": "rgba(88,166,255,0.14)"},
}


def _tier(tier: str) -> dict:
    return TIER.get(tier, TIER["unknown"])


def _esc(s: str) -> str:
    return _html.escape(str(s), quote=True)


# --------------------------------------------------------------------------- #
# Global CSS — dark surface cards, tightened chrome, responsive, animated dot. #
# --------------------------------------------------------------------------- #
def inject_theme() -> None:
    st.markdown(
        f"""
        <style>
          :root {{
            --dk-bg: {BG}; --dk-surface: {SURFACE}; --dk-surface-2: {SURFACE_2};
            --dk-border: {BORDER}; --dk-text: {TEXT}; --dk-muted: {MUTED};
            --dk-accent: {ACCENT};
          }}
          .stApp {{ background: {BG}; }}
          html, body, [class*="css"], .stMarkdown, .stMarkdown p,
          [data-testid="stMarkdownContainer"] {{
            color: {TEXT};
            font-size: 14px; line-height: 1.4;
            -webkit-font-smoothing: antialiased;
          }}
          .block-container {{
            padding-top: 3rem !important; padding-bottom: 2rem !important;
            padding-left: 2rem !important; padding-right: 2rem !important;
            max-width: 1400px !important;
          }}
          /* Tighten Streamlit's default vertical rhythm. */
          [data-testid="stVerticalBlock"] {{ gap: .55rem !important; }}
          [data-testid="stHorizontalBlock"] {{ gap: .7rem !important; }}
          header[data-testid="stHeader"] {{ background: transparent; }}
          #MainMenu, footer {{ visibility: hidden; }}
          hr {{ margin: .6rem 0 !important; border-color: {BORDER}; }}

          /* Responsive: shrink padding on small screens (phones). */
          @media (max-width: 640px) {{
            .block-container {{
              padding-left: .8rem !important; padding-right: .8rem !important;
              padding-top: 2.2rem !important;
            }}
            .dk-row {{ flex-wrap: wrap; }}
            .dk-row__right {{ margin-left: 0 !important; margin-top: .3rem; }}
          }}

          /* Section heading — uppercase muted. */
          .dk-section {{
            text-transform: uppercase; letter-spacing: .08em;
            font-size: 11.5px; font-weight: 700; color: {MUTED};
            margin: 1.1rem 0 .5rem 0; padding-bottom: .3rem;
            border-bottom: 1px solid {BORDER};
          }}

          /* Status pill with a colored dot. */
          .dk-pill {{
            display: inline-flex; align-items: center; gap: .45rem;
            padding: .28rem .65rem; border-radius: 999px;
            font-size: 12.5px; font-weight: 600; line-height: 1.2;
            border: 1px solid var(--dk-border);
          }}
          .dk-pill__dot {{
            width: 9px; height: 9px; border-radius: 50%;
            flex: 0 0 9px; display: inline-block;
          }}
          .dk-dot--pulse {{ animation: dkpulse 1.6s ease-in-out infinite; }}
          @keyframes dkpulse {{
            0%   {{ box-shadow: 0 0 0 0 rgba(63,185,80,0.55); }}
            70%  {{ box-shadow: 0 0 0 7px rgba(63,185,80,0.0); }}
            100% {{ box-shadow: 0 0 0 0 rgba(63,185,80,0.0); }}
          }}

          /* Metric / status card. */
          .dk-card {{
            background: {SURFACE}; border: 1px solid {BORDER};
            border-radius: 12px; padding: .85rem 1rem;
            height: 100%;
          }}
          .dk-card--accent {{ border-color: {ACCENT}; }}
          .dk-card__label {{
            font-size: 11.5px; font-weight: 600; letter-spacing: .02em;
            color: {MUTED}; margin-bottom: .35rem;
          }}
          .dk-card__value {{
            font-size: 1.05rem; font-weight: 650; color: {TEXT};
            line-height: 1.35;
          }}
          .dk-card__sub {{
            font-size: 11.5px; color: {MUTED}; margin-top: .45rem;
            line-height: 1.35;
          }}

          /* A flex "row" line: name (+meta) on the left, status on the right. */
          .dk-row {{
            display: flex; align-items: center; gap: .6rem;
            background: {SURFACE}; border: 1px solid {BORDER};
            border-radius: 10px; padding: .6rem .85rem; margin-bottom: .4rem;
          }}
          .dk-row__main {{ display: flex; flex-direction: column; min-width: 0; }}
          .dk-row__name {{ font-size: 13.5px; color: {TEXT}; font-weight: 550; }}
          .dk-row__meta {{ font-size: 11.5px; color: {MUTED}; margin-top: .15rem; }}
          .dk-row__right {{ margin-left: auto; white-space: nowrap; }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def pill(label: str, tier: str, *, pulse: bool = False) -> str:
    """A rounded status pill with a colored dot. LABEL is a full plain phrase."""
    t = _tier(tier)
    dot_cls = "dk-pill__dot dk-dot--pulse" if pulse else "dk-pill__dot"
    return (
        f'<span class="dk-pill" style="color:{t["c"]};background:{t["bg"]};'
        f'border-color:{t["c"]}55">'
        f'<span class="{dot_cls}" style="background:{t["c"]}"></span>'
        f'{_esc(label)}</span>'
    )


def card(label: str, value_html: str, sub: str = "", *, accent: bool = False) -> str:
    """A surface card: small muted label, a value line (HTML allowed), optional sub."""
    cls = "dk-card dk-card--accent" if accent else "dk-card"
    sub_html = f'<div class="dk-card__sub">{_esc(sub)}</div>' if sub else ""
    return (
        f'<div class="{cls}">'
        f'<div class="dk-card__label">{_esc(label)}</div>'
        f'<div class="dk-card__value">{value_html}</div>'
        f'{sub_html}</div>'
    )


def status_card(label: str, tier: str, big_text: str, sub: str = "",
                *, pulse: bool = False) -> str:
    """A card whose value is a tier-colored dot + a full plain-English phrase."""
    t = _tier(tier)
    dot_cls = "dk-pill__dot dk-dot--pulse" if pulse else "dk-pill__dot"
    value_html = (
        f'<span style="display:inline-flex;align-items:center;gap:.45rem">'
        f'<span class="{dot_cls}" style="background:{t["c"]}"></span>'
        f'<span style="color:{t["c"]}">{_esc(big_text)}</span></span>'
    )
    return card(label, value_html, sub)


def row(name: str, right_html: str, meta: str = "") -> str:
    """A flex line: name (+ optional muted meta) on the left, right_html on the right."""
    meta_html = f'<div class="dk-row__meta">{_esc(meta)}</div>' if meta else ""
    return (
        f'<div class="dk-row">'
        f'<div class="dk-row__main">'
        f'<div class="dk-row__name">{_esc(name)}</div>{meta_html}</div>'
        f'<div class="dk-row__right">{right_html}</div>'
        f'</div>'
    )


def section(title: str) -> str:
    """An uppercase muted section heading."""
    return f'<div class="dk-section">{_esc(title)}</div>'
