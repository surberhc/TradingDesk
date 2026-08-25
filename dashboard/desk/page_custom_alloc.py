"""page_custom_alloc.py — "Custom allocation": the models Andrew authors himself. READ-ONLY.

STAGE 4 of the custom-allocation feature. Andrew writes his own model portfolios (tickers
and percentages) in the CRM; the desk reads them READ-ONLY out of the CRM view
``v_tradingdesk_custom_allocations`` and would rebalance client accounts to them exactly
like an S0 model. This page is the WINDOW on that: what each hand-authored model holds,
which published version it is, which accounts are assigned to it, how far those accounts
have drifted, and the trades that would bring them back — all of it displayed, none of it
sent.

THIS PAGE IS STRUCTURALLY INCAPABLE OF TRANSMITTING. Not "guarded", not "gated" —
incapable, and the suite enforces it (test_custom_alloc_page.py):
  * It spawns NO process. There is no shell-out of any kind, so it cannot invoke an
    executor with or without an arm flag. The whole preview is built IN PROCESS from the
    pure ``rebalance_engine.build_plan`` path (via ``crm_outofspec.scan_out_of_spec``,
    the same posture ``crm_execute.preview_crm`` uses: no ``ib``, ``armed=False``).
  * It opens no broker socket, holds no arm token, has no confirm phrase, no typed
    confirmation box, and no button that could stand in for one. Its only controls are a
    cache refresh and a display filter.
  * Every CRM read is a SELECT under the read-only ``tradingdesk_readonly`` role. The seam
    is one-way: this page never writes to the CRM and never publishes or edits an
    allocation.

WHAT THIS PAGE OWNS THAT NOBODY ELSE CAN (the deferred validations). The CRM can enforce
that weights sum to 100 and that a published version is immutable. It CANNOT know whether
the desk can actually price and buy the tickers, because only the desk has price history
and only the desk sizes whole shares. So two checks live here and nowhere else:
  1. TICKER PRICEABILITY. ``custom_target.build_target`` raises ``CustomAllocationError``
     naming a ticker with no usable price history (its trap 3). A silently unpriced leg
     becomes target_shares=0 — never bought, never band-breaching, invisible forever. This
     page surfaces that error VERBATIM and loudly, per model. It never swallows it.
  2. WHOLE-SHARE VIABILITY. The "(Small, ...)" models exist for accounts under $25,000,
     where a 3% sleeve of a $180 fund rounds to zero shares. This page computes, per
     ticker, how many WHOLE shares the smallest assigned account would actually buy, and
     flags every position that rounds to nothing. With no account assigned yet it runs the
     same check against a clearly-labelled EXAMPLE account size.

IMPORT DISCIPLINE (mirrors page_models.py / page_control_plane.py): module-top imports are
CHEAP only (stdlib, pandas, streamlit, theme, page_models for its table renderer). Every
heavy import — custom_target, crm_roster, crm_outofspec, the frozen config — is LAZY, so
importing this module opens no socket and runs no engine.

CACHING is a caller concern, exactly as in the sibling pages: the CRM read + target build +
whole-book engine run happen once inside ONE ``st.cache_resource`` loader (the Targets carry
pandas objects and are not cache_data-serialisable — same reason
``page_control_plane._target_for`` is a cache_resource), with a short TTL and an explicit
re-read control. ``custom_target`` itself deliberately does no caching of its own.
"""
from __future__ import annotations

import math
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

import theme
import page_models

# --- Make the existing packages importable (reuse, don't rebuild) --------------
# Same sys.path bootstrap desk_app.py / page_control_plane.py use. This module lives at
# dashboard/desk/page_custom_alloc.py, so the repo root is parents[2].
REPO = Path(__file__).resolve().parents[2]
for _sub in ("paperbot", "backtester", "connections", "strategies", "dailyreport",
             "livebot"):
    _p = REPO / _sub
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
_conn = REPO / "connections"
if str(_conn) not in sys.path:
    sys.path.insert(0, str(_conn))


# --- Display-only registry of the models Andrew authors -------------------------
# The six custom models defined in the CRM (Stage 1). This list exists so a model with
# NOTHING PUBLISHED YET can still be shown and said so plainly — a label that has no rows
# in the CRM view is otherwise invisible. It holds NO weights and steers NO decision;
# dispatch is never name-based (custom_target trap 1). Any label the CRM publishes that is
# not in this list is picked up and shown anyway (see _load_state).
CUSTOM_MODEL_LABELS: tuple[str, ...] = (
    "Growth (Custom)",
    "Balanced (Custom)",
    "Conservative (Custom)",
    "Growth (Small, Custom)",
    "Balanced (Small, Custom)",
    "Conservative (Small, Custom)",
)

# Which of them are the small-account tier. DISPLAY ONLY — it decides which EXAMPLE account
# size the whole-share check uses when no account is assigned yet, and nothing else. No
# routing, sizing, or dispatch decision anywhere reads this.
SMALL_MODEL_LABELS: frozenset[str] = frozenset({
    "Growth (Small, Custom)",
    "Balanced (Small, Custom)",
    "Conservative (Small, Custom)",
})

# Stated EXAMPLE account sizes for the whole-share check when a model has no assigned
# account to check against. $25,000 is the small-account tier boundary the desk already
# uses; $5,000 is a realistic small account well inside it.
EXAMPLE_NAV_SMALL = 5_000.0
EXAMPLE_NAV_FULL = 25_000.0

# How long the whole read (CRM + targets + engine run) is cached before it is re-read.
_CACHE_TTL_SECS = 600


# =========================================================================== #
# PURE helpers — no Streamlit, no CRM, no engine. Unit-tested directly.        #
# =========================================================================== #
def _is_small_model(label: str) -> bool:
    """True for the small-account tier of the custom models. Display-only (see
    SMALL_MODEL_LABELS): it picks an example account size for the whole-share check."""
    return str(label) in SMALL_MODEL_LABELS


def _pct_rows(rows) -> tuple[list[tuple[str, float]], float]:
    """Allocation rows -> ([(ticker, weight_as_fraction), ...], total_percent).

    Reads the CRM view's own columns (``ticker``, ``weight_pct``) and nothing else, so the
    displayed book is the published book. Percentages are converted to the FRACTION unit
    ``page_models._render_holdings_table`` renders, and the total is returned in PERCENT so
    a book that does not sum to 100 is visible on screen rather than silently normalised."""
    out: list[tuple[str, float]] = []
    total_pct = 0.0
    for r in rows:
        ticker = str(r["ticker"]).strip().upper()
        pct = float(r["weight_pct"])
        total_pct += pct
        out.append((ticker, pct / 100.0))
    out.sort(key=lambda t: t[1], reverse=True)
    return out, total_pct


def whole_share_rows(weights, prices, nav: float) -> list[dict]:
    """THE whole-share viability check (validation C-2). PURE.

    For an account worth ``nav`` on a book of ``weights`` (ticker -> fraction of NAV) at
    ``prices`` (ticker -> last close), how many WHOLE shares of each position does the
    account actually buy? A position whose dollar target is smaller than one share rounds
    to ZERO — the account simply never holds it, silently, and the miss does not breach any
    rebalance band. That is precisely the failure the small-account tier exists to avoid, so
    it is computed here and flagged.

    Returns one dict per ticker: target weight, target dollars, price, whole shares, the
    dollars actually invested, the leftover, and ``buyable`` (False iff it rounds to zero or
    has no usable price)."""
    out: list[dict] = []
    for tkr in sorted(weights):
        weight = float(weights[tkr])
        dollars = float(nav) * weight
        raw = prices.get(tkr) if hasattr(prices, "get") else None
        try:
            price = float(raw)
        except (TypeError, ValueError):
            price = float("nan")
        if not (price == price) or price <= 0:      # NaN or non-positive
            out.append({"ticker": str(tkr), "target_weight": weight,
                        "target_dollars": dollars, "price": None, "whole_shares": None,
                        "invested": 0.0, "leftover": dollars, "buyable": False,
                        "note": "no usable price — the desk cannot size this position"})
            continue
        shares = int(math.floor(dollars / price))
        out.append({"ticker": str(tkr), "target_weight": weight,
                    "target_dollars": dollars, "price": price, "whole_shares": shares,
                    "invested": shares * price, "leftover": dollars - shares * price,
                    "buyable": shares > 0,
                    "note": "" if shares > 0 else
                            "rounds to ZERO shares — this account would never hold it"})
    return out


def min_nav_for_whole_book(weights, prices) -> float | None:
    """The smallest account value at which EVERY position in the book buys at least one
    whole share = max(price / weight) over the book. PURE. None if any leg is unpriced (the
    question has no answer then). This is the plain-English "how small can an account be
    and still hold this model properly?" number."""
    worst = 0.0
    for tkr in weights:
        weight = float(weights[tkr])
        if weight <= 0:
            continue
        raw = prices.get(tkr) if hasattr(prices, "get") else None
        try:
            price = float(raw)
        except (TypeError, ValueError):
            return None
        if not (price == price) or price <= 0:
            return None
        worst = max(worst, price / weight)
    return worst or None


def scan_accounts_for_model(target, roster_rows, holdings_by_account,
                            universe=None, cash_reserve_pct=None) -> dict:
    """Drift + would-trade preview for the accounts assigned to ONE custom model. PURE-ish:
    it runs the UNCHANGED pure engine and contacts nothing.

    Delegates verbatim to ``crm_outofspec.scan_out_of_spec`` — the same read-only whole-book
    path the Control Plane's out-of-spec panel uses (``rebalance_engine.build_plan`` with no
    ``ib`` and ``armed=False``: it builds and transmits nothing by construction). The only
    difference is the target handed in: this one is Andrew's hand-authored book instead of a
    computed S0 one, keyed by ``target.version`` (which custom_target guarantees IS the model
    label, verbatim — its trap 2).

    ``universe`` is the tradeable set (custom_target trap 4). Passing it matters: a symbol
    outside the universe is classified ALIEN and can never produce a would-trade leg, so a
    preview built with the wrong universe would show "nothing to do" for an account that
    actually needs rotating.

    ``cash_reserve_pct`` is the model's standing cash reserve. Every target this page hands
    in was built from rows in the CRM custom-allocation view — that IS the source-based test
    — so it defaults to the CUSTOM reserve (1%), which is what a hand-authored book actually
    deploys against. Leaving it at the desk-wide 1.5% here would show every correctly-
    invested custom account as permanently 0.5% adrift on cash and over-state every
    would-trade BUY."""
    import crm_outofspec
    import investable

    if cash_reserve_pct is None:
        cash_reserve_pct = investable.buffer_pct_for(is_custom=True)
    return crm_outofspec.scan_out_of_spec(
        list(roster_rows), dict(holdings_by_account), {str(target.version): target},
        universe=universe,
        cash_reserve_pct_by_version={str(target.version): float(cash_reserve_pct)})


def _fmt_date(value) -> str:
    """A date/timestamp from the CRM rendered plainly; '—' when absent."""
    if value is None:
        return "—"
    try:
        return pd.Timestamp(value).strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001 — display only, never take the page down
        return str(value)


def _fmt_datetime(value) -> str:
    if value is None:
        return "—"
    try:
        return pd.Timestamp(value).strftime("%Y-%m-%d %H:%M")
    except Exception:  # noqa: BLE001
        return str(value)


# =========================================================================== #
# The single cached loader: CRM read -> Targets -> assigned accounts -> engine. #
# Read-only end to end. Never raises to the page.                              #
# =========================================================================== #
@st.cache_resource(
    ttl=_CACHE_TTL_SECS,
    show_spinner="Reading Andrew's custom allocations from the CRM (read-only, cached 10 min)…")
def _load_state() -> dict:
    """Everything this page shows, in ONE read. Returns a plain dict; on any CRM problem it
    returns ``{"error": ...}`` rather than raising, so the page degrades to a plain-English
    notice instead of a traceback (same posture as page_control_plane._scan_whole_book).

    Shape::

        {"built_at_str": str, "labels": [str, ...], "assigned_total": int,
         "models": {label: {"published": bool, "rows": [...], "target": Target|None,
                            "meta": AllocationMeta|None, "error": str|None,
                            "accounts": [roster rows], "scan": {...}|None,
                            "scan_error": str|None}}}

    READ-ONLY: two SELECTs against CRM views under the read-only role, local price history,
    and the pure rebalance engine. No broker, no process spawn, no write."""
    import crm_roster
    import custom_target

    if not crm_roster.is_configured():
        return {"error": "not_configured"}

    try:
        published = crm_roster.custom_allocation_labels()
        # Every label we know about: the six the CRM defines (so an unpublished one can be
        # SAID to be unpublished) plus anything else the CRM is publishing today.
        labels = list(CUSTOM_MODEL_LABELS) + sorted(published - set(CUSTOM_MODEL_LABELS))
        rows = crm_roster.fetch_custom_allocations(labels) if labels else []
        roster_rows = crm_roster.fetch_roster(advisor_name=None)   # whole book, read-only
    except crm_roster.CrmRosterUnavailable as exc:
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001 — the page must never show a traceback
        return {"error": f"{type(exc).__name__}: {exc}"}

    by_label: dict[str, list[dict]] = {}
    for r in rows:
        by_label.setdefault(str(r["strategy_name"]), []).append(dict(r))

    # Which roster accounts are assigned to each custom model.
    assigned: dict[str, list[dict]] = {}
    for r in roster_rows:
        model = (r.get("model") or "")
        if model in by_label or model in labels:
            assigned.setdefault(model, []).append(dict(r))

    # Latest holdings for the assigned accounts only (nothing else is needed here). A
    # failure here is NON-FATAL and deliberately so: the published allocations still
    # display in full, and only the drift read says plainly why it is blank.
    holdings: dict[str, list[dict]] = {}
    holdings_error: str | None = None
    assigned_ids = [str(a.get("account_id")) for group in assigned.values() for a in group]
    if assigned_ids:
        try:
            holdings = crm_roster.fetch_holdings_latest(assigned_ids)
        except crm_roster.CrmRosterUnavailable as exc:
            holdings_error = str(exc)
        except Exception as exc:  # noqa: BLE001
            holdings_error = f"{type(exc).__name__}: {exc}"

    # S0's ticker universe, unioned into each custom model's universe so an account
    # migrating OFF an S0 sleeve can be shown rotating out of it (custom_target trap 4 —
    # a held symbol outside the universe is ALIEN and can never produce a leg).
    try:
        from strategies import config as scfg
        base_universe = set(scfg.ALL_TICKERS)
    except Exception:  # noqa: BLE001
        base_universe = set()

    models: dict[str, dict] = {}
    for label in labels:
        model_rows = by_label.get(label, [])
        entry: dict = {
            "published": bool(model_rows),
            "rows": model_rows,
            "target": None,
            "meta": None,
            "error": None,
            "accounts": assigned.get(label, []),
            "scan": None,
            "scan_error": holdings_error,
            "small": _is_small_model(label),
        }
        if model_rows:
            # THE PRICEABILITY CHECK (validation C-1). build_target loads real price history
            # for exactly these tickers and raises CustomAllocationError naming any it cannot
            # price. Captured, never swallowed — the page shows the message verbatim.
            try:
                target, meta = custom_target.build_target(model_rows, label)
                entry["target"] = target
                entry["meta"] = meta
            except custom_target.CustomAllocationError as exc:
                entry["error"] = str(exc)
            except Exception as exc:  # noqa: BLE001
                entry["error"] = f"{type(exc).__name__}: {exc}"

        # DRIFT + WOULD-TRADE PREVIEW for the assigned accounts (pure engine, no broker).
        if entry["target"] is not None and entry["accounts"] and holdings_error is None:
            try:
                universe = custom_target.universe_for(entry["target"], base=base_universe)
                entry["scan"] = scan_accounts_for_model(
                    entry["target"], entry["accounts"], holdings, universe=universe)
            except Exception as exc:  # noqa: BLE001
                entry["scan_error"] = f"{type(exc).__name__}: {exc}"
        models[label] = entry

    return {
        "built_at_str": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "labels": labels,
        "models": models,
        "assigned_total": sum(len(v["accounts"]) for v in models.values()),
        "n_roster": len(roster_rows),
    }


# =========================================================================== #
# Renderers.                                                                   #
# =========================================================================== #
def _render_readonly_banner() -> None:
    """The page's standing statement of what it is and what it structurally cannot do."""
    st.markdown(
        theme.status_card(
            "This page only displays these allocations — it cannot send anything",
            "info",
            "Read-only — no arm, no send, nothing transmitted",
            "Andrew authors these models himself in the client system; the desk reads them "
            "one way, read-only, and would rebalance accounts to them exactly like a "
            "computed model. Everything below is displayed: the published book, which "
            "accounts are on it, how far they have drifted, and the trades that WOULD bring "
            "them back. This page starts no program, opens no broker connection, holds no "
            "arming control and has no confirmation box — there is no path from this screen "
            "to a live order. Nothing here writes back to the client system either: the "
            "connection is one-way and read-only.",
        ),
        unsafe_allow_html=True,
    )


def _render_allocation(label: str, entry: dict) -> None:
    """The published book itself: tickers, percentages, the total, and which published
    version it is (number, effective date, when it was published)."""
    rows, total_pct = _pct_rows(entry["rows"])

    # The sleeve column is OMITTED for custom models (see module docstring / report):
    # page_models._sleeve_of maps a ticker to one of Strategy 0's four sleeves and returns
    # a dash for anything else, so every hand-authored ticker would render a blank column.
    # A hand-authored book has no sleeve concept at all — Andrew picks the tickers directly
    # — so showing an always-empty column would be worse than showing no column. Rendering
    # is still page_models' own table (given a show_sleeve switch), so the two model pages
    # cannot drift apart in how a book is displayed.
    page_models._render_holdings_table(rows, show_sleeve=False)

    meta = entry.get("meta")
    version_txt = "—"
    effective_txt = "—"
    published_txt = "—"
    if meta is not None:
        version_txt = ("—" if meta.version_number is None
                       else f"version {meta.version_number}")
        effective_txt = _fmt_date(meta.effective_from)
        published_txt = _fmt_datetime(meta.published_at)
    elif entry["rows"]:
        first = entry["rows"][0]
        vnum = first.get("version_number")
        version_txt = "—" if vnum is None else f"version {vnum}"
        effective_txt = _fmt_date(first.get("effective_from"))
        published_txt = _fmt_datetime(first.get("published_at"))

    total_tier = "good" if abs(total_pct - 100.0) < 1e-6 else "bad"
    total_phrase = (f"{total_pct:.2f}% — fully invested"
                    if total_tier == "good"
                    else f"{total_pct:.2f}% — does NOT add up to 100%")
    st.markdown(
        theme.card(
            f"{label} — which published version this is",
            "".join([
                theme.row("Published version",
                          f'<span style="color:{theme.TEXT};font-weight:650">'
                          f'{theme._esc(version_txt)}</span>',
                          "a published version is frozen — a change is published as a new "
                          "version, never an edit of this one"),
                theme.row("Effective from",
                          f'<span style="color:{theme.TEXT};font-weight:650">'
                          f'{theme._esc(effective_txt)}</span>'),
                theme.row("Published on",
                          f'<span style="color:{theme.TEXT};font-weight:650">'
                          f'{theme._esc(published_txt)}</span>'),
                theme.row("Percentages add up to",
                          theme.pill(total_phrase, total_tier)),
                theme.row("Number of positions",
                          f'<span style="color:{theme.TEXT};font-weight:650">'
                          f'{len(rows)}</span>'),
            ]),
        ),
        unsafe_allow_html=True,
    )


def _render_priceability(label: str, entry: dict) -> None:
    """VALIDATION C-1 — ticker priceability. Only the desk has price history, so only the
    desk can find this. Shown LOUDLY and verbatim, never swallowed."""
    if entry.get("error"):
        st.markdown(
            theme.status_card(
                f"{label} — this allocation CANNOT be traded as published",
                "bad",
                "A ticker in this model has no price history the desk can size from",
                "Until this is fixed, an account on this model cannot be rebalanced to it. "
                "A position the desk cannot price would be sized at zero shares — never "
                "bought, and invisible to the drift check — so the desk refuses the whole "
                "book instead. Fix it either by loading price history for the ticker, or by "
                "publishing a new version of the allocation without it.",
            ),
            unsafe_allow_html=True,
        )
        st.error(entry["error"])
        return
    target = entry.get("target")
    if target is None:
        return
    tickers = ", ".join(str(t) for t in target.weights.index)
    st.markdown(
        theme.status_card(
            f"{label} — every ticker can be priced and sized",
            "good",
            f"{len(target.weights)} of {len(target.weights)} tickers have usable price "
            f"history",
            f"Checked against real local price history for exactly these tickers "
            f"({tickers}) as of {_fmt_date(target.price_date)}. This is the check the "
            f"client system cannot do — it does not hold prices.",
        ),
        unsafe_allow_html=True,
    )


def _render_whole_share(label: str, entry: dict) -> None:
    """VALIDATION C-2 — whole-share viability. Whether every position in the book is
    actually buyable in WHOLE shares at the relevant account size, and which ones round to
    nothing. Checked against the SMALLEST assigned account; with none assigned, against a
    clearly-labelled example size."""
    target = entry.get("target")
    if target is None:
        return

    accounts = entry.get("accounts") or []
    navs = [float(a.get("total_value") or 0.0) for a in accounts]
    navs = [n for n in navs if n > 0]
    if navs:
        nav = min(navs)
        smallest = min((a for a in accounts if float(a.get("total_value") or 0.0) > 0),
                       key=lambda a: float(a.get("total_value") or 0.0))
        basis = (f"the SMALLEST account assigned to this model "
                 f"({smallest.get('account_number')}, ${nav:,.0f})")
        example = False
    else:
        nav = EXAMPLE_NAV_SMALL if entry.get("small") else EXAMPLE_NAV_FULL
        basis = (f"an EXAMPLE account of ${nav:,.0f} — no account is assigned to this "
                 f"model yet, so there is no real account to check")
        example = True

    weights = {str(k): float(v) for k, v in target.weights.items()}
    prices = {str(k): float(v) for k, v in target.prices.items()}
    checks = whole_share_rows(weights, prices, nav)
    unbuyable = [c for c in checks if not c["buyable"]]
    floor_nav = min_nav_for_whole_book(weights, prices)

    if unbuyable:
        names = ", ".join(c["ticker"] for c in unbuyable)
        st.markdown(
            theme.status_card(
                f"{label} — whole-share check at ${nav:,.0f}"
                + (" (example size)" if example else ""),
                "bad",
                f"{len(unbuyable)} position(s) round to ZERO shares: {names}",
                f"Checked against {basis}. The desk buys whole shares only, so a position "
                f"whose dollar target is smaller than one share is simply never held — and "
                f"the miss never breaches a rebalance band, so nothing would ever flag it. "
                + (f"Every position in this model buys at least one whole share once the "
                   f"account is worth about ${floor_nav:,.0f}."
                   if floor_nav else ""),
            ),
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            theme.status_card(
                f"{label} — whole-share check at ${nav:,.0f}"
                + (" (example size)" if example else ""),
                "good",
                "Every position buys at least one whole share",
                f"Checked against {basis}. "
                + (f"Every position in this model stays buyable in whole shares down to an "
                   f"account of about ${floor_nav:,.0f}." if floor_nav else ""),
            ),
            unsafe_allow_html=True,
        )

    table = [{
        "Ticker": c["ticker"],
        "Target percentage": f"{c['target_weight'] * 100:.2f}%",
        "Dollars targeted": round(c["target_dollars"], 2),
        "Price used": "—" if c["price"] is None else round(c["price"], 2),
        "Whole shares bought": "—" if c["whole_shares"] is None else c["whole_shares"],
        "Dollars actually invested": round(c["invested"], 2),
        "Dollars left over": round(c["leftover"], 2),
        "Buyable in whole shares": "yes" if c["buyable"] else "NO",
        "What that means": c["note"] or "—",
    } for c in checks]
    st.dataframe(pd.DataFrame(table), hide_index=True, use_container_width=True)
    st.caption(
        f"Prices are the last close the desk holds ({_fmt_date(target.price_date)}) — the "
        f"same prices the rebalance sizing would use. Read-only arithmetic; no order of any "
        f"kind is built here.")


def _render_accounts_and_drift(label: str, entry: dict, show_in_line: bool) -> None:
    """Which accounts are assigned to this model, how far they have drifted, and the
    read-only preview of the trades that would bring them back."""
    accounts = entry.get("accounts") or []
    if not accounts:
        st.markdown(
            theme.status_card(
                f"{label} — accounts assigned",
                "unknown",
                "No account is assigned to this model yet",
                "Nothing to rebalance and nothing to drift. Assigning an account is done in "
                "the client system, not here.",
            ),
            unsafe_allow_html=True,
        )
        return

    if entry.get("scan_error"):
        st.warning(
            f"{len(accounts)} account(s) are assigned to {label}, but their drift could not "
            f"be worked out right now ({entry['scan_error']}). The published allocation "
            f"above is unaffected, and nothing has been placed or sent.")
        return
    if entry.get("target") is None:
        st.warning(
            f"{len(accounts)} account(s) are assigned to {label}, but the allocation itself "
            f"cannot be turned into a tradeable book (see the message above), so no drift "
            f"read is possible until that is fixed.")
        return

    scan = entry.get("scan") or {}
    verdicts = scan.get("verdicts", [])
    skipped = scan.get("skipped", [])
    excluded = scan.get("excluded", [])
    n_oos = scan.get("n_out_of_spec", 0)

    c1, c2, c3 = st.columns(3)
    c1.metric("Accounts on this model", len(accounts))
    c2.metric("Out of line with the allocation", n_oos)
    c3.metric("In line with the allocation", scan.get("n_in_spec", 0))
    st.caption(
        f"Checked with the same pure engine the Control Plane's read-only out-of-spec panel "
        f"uses — it sizes the account against the published book and reports what would "
        f"trade. It builds and sends nothing. "
        + (f"{len(skipped)} unfunded/no-snapshot account(s) skipped. " if skipped else "")
        + (f"{len(excluded)} account(s) held out for manual review (recorded value "
           f"disagrees with holdings). " if excluded else ""))

    shown = [v for v in verdicts if show_in_line or v.get("out_of_spec")]
    if not shown:
        st.success("Every assigned account is in line with the published allocation — "
                   "nothing would trade.")
        return

    table = [{
        "Account": v["account"],
        "Advisor": v.get("advisor_name") or "—",
        "Entity": v.get("entity") or "—",
        "Verdict": ("HELD BACK — needs a look" if v.get("blocked")
                    else "OUT OF LINE with the allocation" if v.get("out_of_spec")
                    else "in line with the allocation"),
        "Account value": round(float(v.get("net_liq") or 0.0), 2),
        "Value the model manages": round(
            float(v.get("managed_net_liq", v.get("net_liq")) or 0.0), 2),
        "Value we never trade": round(float(v.get("held_aside_value") or 0.0), 2),
        "Positions": v.get("n_positions", 0),
        "Trades it would take": v.get("n_legs", 0),
    } for v in shown]
    st.dataframe(pd.DataFrame(table), hide_index=True, use_container_width=True)

    with st.expander(f"Show the trades that WOULD bring these {len(shown)} account(s) to "
                     f"{label} (read-only preview — nothing is sent)"):
        st.caption(
            "This is a preview of what the rebalance would do, worked out in this page from "
            "the published allocation and the latest holdings. No program is started, no "
            "broker connection is opened, and no order object exists.")
        any_legs = False
        for v in shown:
            if not v.get("legs"):
                continue
            any_legs = True
            st.markdown(f"**{v['account']}** · {v.get('advisor_name') or '—'} — "
                        f"{v['n_legs']} trade(s)")
            st.dataframe(pd.DataFrame(v["legs"]), hide_index=True,
                         use_container_width=True)
        if not any_legs:
            st.caption("No account in this list would trade.")


def _render_model(label: str, entry: dict, show_in_line: bool) -> None:
    """One custom model's whole block."""
    if not entry.get("published"):
        st.markdown(theme.section(f"{theme._esc(label)} — no allocation published yet"),
                    unsafe_allow_html=True)
        st.markdown(
            theme.status_card(
                label,
                "unknown",
                "No allocation published yet",
                "This model exists in the client system but has no published allocation, so "
                "there is no book to show and nothing the desk could trade to. That is NOT "
                "the same as an empty or all-cash book: the desk refuses to build a target "
                "at all, because an empty target would sell an account down to nothing. "
                "Publish a version in the client system and it appears here.",
            ),
            unsafe_allow_html=True,
        )
        accounts = entry.get("accounts") or []
        if accounts:
            st.error(
                f"{len(accounts)} account(s) are assigned to {label}, which has NO published "
                f"allocation. Those accounts have no book to be rebalanced to.")
        return

    st.markdown(theme.section(f"{theme._esc(label)} — published allocation"),
                unsafe_allow_html=True)
    _render_allocation(label, entry)
    _render_priceability(label, entry)
    _render_whole_share(label, entry)
    _render_accounts_and_drift(label, entry, show_in_line)


# =========================================================================== #
# Page entry point.                                                           #
# =========================================================================== #
def render_custom_alloc() -> None:
    """Render the read-only Custom allocation page: for every model Andrew authors himself,
    the published book (tickers, percentages, total, version), whether the desk can price
    and buy every ticker, which accounts are assigned, how far they have drifted, and the
    trades that would bring them back. Displays only — it starts no program, opens no broker
    connection, arms nothing, sends nothing, and writes nothing back to the client system."""
    st.subheader("Custom allocation — the models Andrew writes himself")
    st.caption(
        "Andrew picks the tickers and percentages himself in the client system; the desk "
        "reads them one way and would rebalance accounts to them exactly like a computed "
        "model. This page shows each published book, checks the two things only the desk "
        "can check (can every ticker be priced, and is every position buyable in whole "
        "shares), lists the accounts assigned to it, and previews the trades that would "
        "bring them back in line. Everything here is read-only.")

    _render_readonly_banner()

    ctl1, ctl2 = st.columns([1, 2])
    with ctl1:
        if st.button("Re-read the allocations from the client system",
                     key="ca_refresh",
                     help="Clears this page's 10-minute cache and reads the published "
                          "allocations again. Reading only."):
            _load_state.clear()
    with ctl2:
        show_in_line = st.checkbox(
            "Also list accounts that are already in line with their allocation",
            value=False, key="ca_show_in_line")

    state = _load_state()

    if state.get("error") == "not_configured":
        st.warning(
            "The connection to the client system is not wired up on this machine yet, so "
            "the custom allocations cannot be read. Andrew must set the "
            "`TRADINGDESK_CRM_DSN` environment variable to the read-only role's connection "
            "string before this page can show anything. No credential is stored in code, "
            "and nothing here transmits.")
        return
    if state.get("error"):
        st.warning(
            "The custom allocations could not be read from the client system right now "
            f"({state['error']}). Nothing about the models themselves has changed, and "
            "nothing has been placed or sent. Try the re-read control above in a moment.")
        return

    models = state.get("models", {})
    labels = state.get("labels", [])
    n_published = sum(1 for m in models.values() if m.get("published"))
    n_broken = sum(1 for m in models.values() if m.get("error"))

    s1, s2, s3 = st.columns(3)
    s1.metric("Custom models", len(labels))
    s2.metric("With an allocation published", n_published)
    s3.metric("Accounts assigned to one", state.get("assigned_total", 0))
    st.caption(f"Read {state.get('built_at_str', '—')} from the client system's read-only "
               f"view, across a book of {state.get('n_roster', 0)} accounts. "
               "Cached for 10 minutes; use the re-read control to refresh.")

    if n_broken:
        st.error(
            f"{n_broken} published custom allocation(s) CANNOT be traded as published — a "
            f"ticker has no price history the desk can size from. The affected model(s) are "
            f"marked below in red with the exact reason.")

    for label in labels:
        _render_model(label, models.get(label, {"published": False}), show_in_line)
