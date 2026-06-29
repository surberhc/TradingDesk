# Dashboard Refinement Roadmap

*Last updated: 2026-06-29 · Status: PLAN (nothing here is built). All work stays PAPER-only and the read-only guarantees in `dashboard/app.py` hold until Phase 3 is explicitly opened.*

---

## Where the dashboard is TODAY (and the gap)

`dashboard/app.py` is a single read-only Streamlit app with 4 tabs — **Health** (collector progress, EOD coverage, status JSONs, Windows task states), **Gamma (GEX)** (latest SPX/SPXW/SPY snapshot + one `st.line_chart` history), **Backtests** (live CAGR/maxDD/Calmar/Sortino/down-capture for the 3 S0 versions + HTML report download buttons), and **Accounts** (button-triggered read-only gateway read, drift vs target, build-only rebalance preview). The basic *look* is done — dark theme, colored metric tiers, status row, last-refreshed line, badge legend, compact restyle.

The gap is three-fold: (1) the **visualization is shallow** — gamma is a snapshot + a plain line chart with no by-strike grid and no GEX zero-line/flip chart; (2) the app **only knows about S0** — S4 (built, SEC-validated) and S5 (EOD-validated) have no presence at all; (3) it is **monitor-only** — there are no backtester controls (Phase 2) and no gated trading controls (Phase 3).

---

## Bucket 1 — LOOK / Visualization (the open "look-improvement roadmap")

These are the named items from STATUS.md plus the obvious adjacent wins. Nearly all are pure-frontend on data already on disk.

| # | Item | What it is (plain English) | Why useful | Effort | Dependency |
|---|------|----------------------------|-----------|--------|------------|
| L1 | **GEX zero-line / flip chart** | Replace the bare `st.line_chart` of `net_gex` with a proper plotly chart: net-GEX bars/area with a **zero line** drawn, plus a marker line for `gamma_flip` and a `spot` line so you see at a glance whether spot is above/below the flip. | The single most-requested viz. Turns "a wiggly line" into "are we in positive or negative gamma, and how far from the flip." | **S** | None — `*_gex_daily.parquet` already has `net_gex`, `spot`, `gamma_flip`, `dist_to_flip_pct`. |
| L2 | **Gamma-by-strike grid** | A per-strike dealer-gamma profile (gamma exposure across the strike ladder for the latest day) as a horizontal bar/heat strip, with spot and flip marked. | Shows *where* the gamma walls are, not just the net number — the thing you actually trade around. | **M** | **Needs per-strike GEX in the derived tables.** Current `*_gex_daily.parquet` looks like daily *aggregates* only — confirm whether a strike-level derived table exists; if not, this needs a small build in `features/gex.py` to persist the strike profile. Flag: may be **partially blocked** until that table exists. |
| L3 | **Consistent number formatting** | One shared formatter for $, %, and B/M magnitudes across all tabs (right now formatting is inline per-metric and slightly inconsistent). | Cheap polish; removes the "why is this 2 decimals here and 3 there" papercuts. | **S** | None. |
| L4 | **GEX history density / range controls** | Add a lookback selector (30 / 90 / 250 / all sessions) and optional multi-series overlay to the history chart; show min/max/last annotations. | Lets you zoom the gamma history to a regime instead of always 250 sessions. | **S** | None. |
| L5 | **Sparklines on the Gamma snapshot tiles** | Tiny inline trend under each snapshot metric (net GEX, dist-to-flip) so the snapshot shows direction, not just a point value. | Adds context to the at-a-glance row for near-zero extra screen space. | **S** | None. |
| L6 | **Backtest equity-curve preview inline** | Render a small NAV / drawdown plot per S0 version *inside* the Backtests tab (currently you must download the full plotly HTML to see a curve). | Closes the loop so the tab is useful without leaving it. | **M** | Reuses `run_backtest` output already computed in `backtest_metrics()` (it returns `benchmark_navs`). |
| L7 | **Accounts tab: show "in-band" status, not raw "drift"** | On the Accounts tab, lead with our actual band status (`in-band` / `REBALANCE`, keyed on the trade-size-vs-NetLiq 3% band) instead of the raw weight "drift" number. A correctly-invested account always reads ~1% raw drift (cash reserve + integer-share rounding), so labeling that "drift" wrongly implies a rebalance is needed. Keep the raw number available secondarily (e.g. "0.3% to band" / on hover), but the headline word should be the band verdict. | **User-flagged 2026-06-29:** "drift" makes it look like the account needs rebalancing when it doesn't. Naming should report the decision (trade-worthy?), not the raw deviation. The band logic already exists (`rebalance_engine.py:114`, `REBALANCE_BAND_PCT=0.03`); this is a labeling/display change. | **S** | None — `recon_report`/`plan_account` already compute in-band status. |

---

## Bucket 2 — Strategy coverage (S4 / S5 surfaces) — *adjacent to the look roadmap, high value*

Not in the original 3-bucket framing, but worth flagging: the desk grew S4 and S5 since the dashboard was built, and they're invisible. These are read-only display, so they fit Phase 1's guarantees.

| # | Item | What it is | Why useful | Effort | Dependency |
|---|------|-----------|-----------|--------|------------|
| V1 | **S4 vol-control panel** | A tab/section showing the S4 fund's headline TR/ER metrics, realized-vol-vs-target line, and exposure-over-time, plus the 2-D `target_vol` × `leverage_cap` sweep as a small heatmap. | S4 is BUILT + SEC-validated; the dashboard should surface the desk's newest product. Sweep heatmap is the natural decision view. | **M** | Reads `backtester/output/s4_vol_control_20260628.md` / the runner `backtester/s4_vol_control.py`. May need the runner to emit a parquet/JSON instead of just markdown. |
| V2 | **S5 convexity-ledger panel** | Show the S5 EOD-validated tail-size frontier (carry cost vs V-bottom-close) and the priority-waterfall ledger from the spec. | S5's defensive half is real-data-validated; a viz makes the "does the tail close S4's gap" story legible. | **M** | EOD results exist; **the offensive/harvest half is blocked on the 1-min SPXW pull** (see below). Display the EOD half now. |

---

## Bucket 3 — Phase 2: Backtester controls (still read-only; runs the validated engine)

Move from "show the latest backtest" to "let me *run* a backtest with parameters from the UI." This is computation, not trading — no broker, no gateway, still zero order risk.

| # | Item | What it is | Why useful | Effort | Dependency |
|---|------|-----------|-----------|--------|------------|
| P2-1 | **Version / date-range run controls** | Inputs for version + `end` date (and start), a Run button that calls the existing cached `run_backtest`, and shows metrics for that run. | Turns the static 3-card readout into an interactive what-if without leaving the dashboard. | **M** | Engine already supports `version=` and `end=`; mostly wiring + a sensible cache key. |
| P2-2 | **Regime-knob sweep panel** | Expose the safe, already-studied knobs (e.g. `REGIME_TREND_MARGIN`, the re-entry ladder params) as sliders, run the engine, and chart the metric surface. | This is where the *real* S0 research lives (per STATUS.md the structural exploration is all about these knobs). A UI makes the plateau visible. | **L** | Needs the engine to accept knob overrides per-run cleanly; **gate hard** — STATUS.md warns these knobs are a curve-fit-prone plateau. Build as explore-only, label loudly. |
| P2-3 | **S4 sweep runner in-UI** | Run the S4 `target_vol` × `leverage_cap` sweep from sliders and render the heatmap live. | Makes S4 a first-class interactive product surface, not a one-off markdown. | **M** | Depends on V1 (S4 runner emitting structured output). |
| P2-4 | **Run history / compare** | Persist the last N UI-triggered runs and let two be compared side-by-side. | "Did my knob change actually help?" in one screen. | **M** | Depends on P2-1; needs a small local results cache (off-Drive, on `C:\TradingDesk-Local`). |

---

## Bucket 4 — Phase 3: Gated trading controls (PAPER ONLY — the hard wall)

This is the only bucket that touches the order path. STATUS.md already has the full gated executor built (`paperbot/rebalance_execute.py`, the 4-condition gate, clientId 38). Phase 3 = a **review/arm surface** for it — and it must NOT weaken any existing read-only guarantee in `app.py`.

| # | Item | What it is | Why useful | Effort | Dependency |
|---|------|-----------|-----------|--------|------------|
| P3-1 | **Rebalance review view (no arm)** | Promote the existing build-only `build_preview` output into a structured, per-account review table (shares to trade, reserve, block aggregation) — still **build-only, transmits nothing**. | Makes the Monday review legible without the raw captured-stdout `st.code` block. Pure display. | **M** | Reuses `rebalance_run.build_preview`; no new order path. Safe to build before any arming. |
| P3-2 | **Pre-arm checklist surface** | Show the MONDAY_RUNBOOK pre-arm gates as a live checklist: gateway up, `fa_probe.py` XML tag casing verified, accounts funded/enrolled, drift present. | Turns the runbook into a visible go/no-go, reducing the chance of an armed run with an unmet precondition. | **M** | Read-only checks; needs the gateway up (weekday). |
| P3-3 | **Gated arm control (LAST, separate sign-off)** | A control that can *invoke* the existing 4-condition executor — and even then only assembles the exact `--arm-i-understand` invocation for explicit confirmation; ideally it never auto-arms from the web UI at all. | The end state, but the **highest-risk** item. The current app's whole design promise is "nothing here arms or transmits." | **L** | **Requires explicit Andrew sign-off to even open.** Depends on P3-1 + P3-2. Must keep the executor's 4-condition gate as the real control; the UI is at most a launcher, never a bypass. Recommend keeping the actual transmit on the CLI even after this lands. |

---

## Quick wins vs bigger builds

- **Quick wins (S, do first):** L1 (GEX zero-line/flip chart), L3 (consistent formatting), L4 (history range controls), L5 (snapshot sparklines).
- **Medium:** L2 (gamma-by-strike grid — *if* the strike table exists), L6 (inline equity curves), V1/V2 (S4/S5 panels), P2-1 (backtest run controls), P3-1/P3-2 (rebalance review + pre-arm checklist).
- **Bigger builds (L):** P2-2 (regime-knob sweep — gate hard), P3-3 (gated arm control — needs sign-off).

## Blocked on the 1-minute SPXW data (still downloading, ETA ~2026-06-30)

- **L2 gamma-by-strike** is *not* 1-min-blocked, but it MAY be blocked on a strike-level derived table that doesn't exist yet — confirm before scheduling.
- **V2 S5 — offensive/harvest half** (0DTE harvest engine, active monetization, dynamic throttle) is blocked on the Phase-1 SPXW 1-min pull. The **defensive/EOD half is buildable now.**
- Any future **intraday gamma / S2 / S3** dashboard surface is blocked on the same 1-min pull. Don't scope it yet.

---

## Recommended next 3 things to build

1. **L1 — GEX zero-line / flip chart.** The single highest-value, lowest-effort viz win and the headline item on the open look-roadmap. Pure frontend on data already on disk. (**S**)
2. **V1 — S4 vol-control panel.** Surface the desk's newest, fully-validated product (the sweep heatmap is the decision view). Highest value-per-effort of the strategy-coverage gap; sets up P2-3 later. (**M**)
3. **P3-1 — Rebalance review view (build-only).** Cleans up the Monday review *without touching the order path* — pure display over the existing `build_preview`. Delivers Phase-3 value while keeping every read-only guarantee intact, and is a safe stepping stone before any arming work.

*Deliberately deferred:* P2-2 (regime-knob sweep — curve-fit risk, gate hard) and P3-3 (gated arm control — needs explicit sign-off and depends on P3-1/P3-2). Keep the actual transmit on the CLI.
