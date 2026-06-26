"""
weekly_comparison.py — Weekly side-by-side comparison report (last N years).

Takes the strategy AS CURRENTLY BUILT and lays it next to SPY, a 60/40 blend, and
T-bills over the trailing N years, sampled weekly. Produces one standalone HTML
file: a cumulative-return chart on top, and a supporting data table underneath
listing — for every weekly date and every series — the ongoing (cumulative)
return, that week's return, and that day's daily return. A CSV of the same table
is written alongside for spreadsheet use.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import plotly.io as pio
from plotly.offline import get_plotlyjs

from strategies import config
from src import backtest, metrics  # noqa: F401 (metrics kept for parity/imports)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = PROJECT_ROOT / config.OUTPUT_DIR
_SERIES = ["strategy", "SPY", "60/40", "T-bills"]
_LABEL = {"strategy": "Strategy", "SPY": "S&P 500 (SPY)", "60/40": "60/40", "T-bills": "T-bills"}
_COLOR = {"strategy": "#1f77b4", "SPY": "#d62728", "60/40": "#7f7f7f", "T-bills": "#2ca02c"}


def _weekly_last_trading_days(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Last actual trading day of each ISO week (handles holidays)."""
    s = index.to_series()
    iso = index.isocalendar()
    return pd.DatetimeIndex(s.groupby([iso["year"], iso["week"]]).last().sort_values().values)


def build_weekly_comparison(
    version: str = config.ACTIVE_VERSION,
    years: int = 5,
    output_path: str | Path | None = None,
) -> Path:
    """Render the weekly comparison HTML (+ CSV) and return the HTML path."""
    result = backtest.run_backtest(version=version)
    navs = result["benchmark_navs"][[c for c in _SERIES if c in result["benchmark_navs"]]].dropna()

    # Trailing N-year window, rebased so cumulative return starts at 0%.
    end = navs.index.max()
    start = end - pd.DateOffset(years=years)
    window = navs.loc[navs.index >= start]
    rebased = window / window.iloc[0]                      # growth of $1 from window start
    daily_ret = window.pct_change()                        # true daily returns

    weekly_dates = _weekly_last_trading_days(window.index)
    cum = rebased.loc[weekly_dates] - 1.0                  # ongoing (cumulative) return
    wk_ret = rebased.loc[weekly_dates].pct_change()        # week-over-week return
    day_ret = daily_ret.loc[weekly_dates]                  # that day's daily return

    # ---- Chart: cumulative return, weekly ----
    fig = go.Figure()
    for c in _SERIES:
        if c in cum:
            fig.add_trace(go.Scatter(
                x=cum.index, y=cum[c], name=_LABEL[c], mode="lines",
                line=dict(color=_COLOR[c], width=2.4 if c == "strategy" else 1.6),
                hovertemplate=_LABEL[c] + ": %{y:.1%}<extra></extra>",
            ))
    fig.update_layout(
        title=f"Cumulative return — last {years} years (weekly), {version} strategy vs benchmarks",
        yaxis_tickformat=".0%", yaxis_title="Cumulative return", hovermode="x unified",
        template="plotly_white", legend=dict(orientation="h", y=-0.15), margin=dict(t=55, b=40),
    )
    chart_div = pio.to_html(fig, full_html=False, include_plotlyjs=False)

    # ---- Supporting data table + CSV ----
    flat = pd.DataFrame(index=weekly_dates)
    flat.index.name = "week_ending"
    for c in _SERIES:
        if c in cum:
            flat[(c, "cumulative")] = cum[c]
            flat[(c, "weekly")] = wk_ret[c]
            flat[(c, "daily")] = day_ret[c]

    OUTPUT_PATH.mkdir(parents=True, exist_ok=True)
    if output_path is None:
        output_path = OUTPUT_PATH / f"weekly_comparison_{version}.html"
    output_path = Path(output_path)
    csv_path = output_path.with_suffix(".csv")
    csv_out = flat.copy()
    csv_out.columns = [f"{_LABEL[c]} {k}" for c, k in csv_out.columns]
    csv_out.round(5).to_csv(csv_path)

    table_html = _table_html(weekly_dates, cum, wk_ret, day_ret)
    final = cum.iloc[-1]
    summary = " · ".join(f"{_LABEL[c]} {final[c]:+.1%}" for c in _SERIES if c in cum)

    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Weekly comparison — last {years} years ({version})</title>
<script>{get_plotlyjs()}</script>
<style>
  body {{ font-family: system-ui, Arial, sans-serif; margin: 24px; color: #222; max-width: 1100px; }}
  h1 {{ margin-bottom: 2px; }} .sub {{ color: #666; margin-top: 2px; }}
  .summary {{ background:#f3f7ff; border-left:4px solid #1f77b4; padding:10px 14px; margin:14px 0; font-size:14px; }}
  .tbl-wrap {{ max-height: 560px; overflow: auto; border: 1px solid #e0e0e0; border-radius: 4px; }}
  table.cmp {{ border-collapse: collapse; font-size: 12px; width: 100%; }}
  table.cmp th, table.cmp td {{ padding: 4px 8px; border-bottom: 1px solid #f0f0f0; text-align: right; white-space: nowrap; }}
  table.cmp thead th {{ position: sticky; top: 0; background:#f0f0f0; }}
  table.cmp td.date, table.cmp th.date {{ text-align: left; position: sticky; left: 0; background: #fff; }}
  table.cmp .grp {{ border-left: 2px solid #ddd; }}
  .pos {{ color:#1a7f37; }} .neg {{ color:#c0392b; }}
</style></head><body>
<h1>Weekly comparison — trailing {years} years</h1>
<p class="sub">{version} strategy vs S&amp;P 500, 60/40, and T-bills. Cumulative return rebased to 0% at the window start.
Weekly = last trading day of each week. Research tooling; no live trades.</p>
<div class="summary"><b>Final cumulative return ({cum.index[0].date()} → {cum.index[-1].date()}):</b> {summary}</div>
{chart_div}
<h2>Supporting data (weekly)</h2>
<p class="sub">For each week: <b>Cum</b> = ongoing cumulative return since the window start;
<b>Wk</b> = that week's return; <b>Day</b> = the daily return on that date. Also in
<code>{csv_path.name}</code>.</p>
{table_html}
</body></html>"""
    output_path.write_text(html, encoding="utf-8")
    return output_path


def build_versions_comparison(
    versions=("Conservative", "Balanced", "Growth"),
    years: int = 5,
    output_path: str | Path | None = None,
) -> Path:
    """All three strategy versions overlaid against SPY / 60-40 / T-bills, weekly."""
    label = {"Conservative": "Strategy — Conservative", "Balanced": "Strategy — Balanced",
             "Growth": "Strategy — Growth", "SPY": "S&P 500 (SPY)", "60/40": "60/40", "T-bills": "T-bills"}
    color = {"Conservative": "#7fc7ff", "Balanced": "#1f77b4", "Growth": "#08306b",
             "SPY": "#d62728", "60/40": "#7f7f7f", "T-bills": "#2ca02c"}

    navs = {}
    bench = None
    for v in versions:
        r = backtest.run_backtest(version=v)
        navs[v] = r["benchmark_navs"]["strategy"]
        bench = r["benchmark_navs"]
    for b in ("SPY", "60/40", "T-bills"):
        if b in bench:
            navs[b] = bench[b]
    keys = list(versions) + [b for b in ("SPY", "60/40", "T-bills") if b in bench]
    panel = pd.DataFrame(navs).dropna()

    end = panel.index.max()
    window = panel.loc[panel.index >= end - pd.DateOffset(years=years)]
    rebased = window / window.iloc[0]
    daily_ret = window.pct_change()
    wk = _weekly_last_trading_days(window.index)
    cum = rebased.loc[wk] - 1.0
    wk_ret = rebased.loc[wk].pct_change()
    day_ret = daily_ret.loc[wk]

    fig = go.Figure()
    for k in keys:
        fig.add_trace(go.Scatter(
            x=cum.index, y=cum[k], name=label[k], mode="lines",
            line=dict(color=color[k], width=2.4 if k in versions else 1.5,
                      dash="dot" if k in ("SPY", "60/40", "T-bills") else "solid"),
            hovertemplate=label[k] + ": %{y:.1%}<extra></extra>",
        ))
    fig.update_layout(
        title=f"Cumulative return — last {years} years (weekly): all strategy versions vs benchmarks",
        yaxis_tickformat=".0%", yaxis_title="Cumulative return", hovermode="x unified",
        template="plotly_white", legend=dict(orientation="h", y=-0.18), margin=dict(t=55, b=50))
    chart_div = pio.to_html(fig, full_html=False, include_plotlyjs=False)

    # Table: cumulative + daily per series (grouped); CSV carries weekly too.
    top = "<tr><th class='date' rowspan='2'>Week ending</th>" + "".join(
        f"<th colspan='2' class='grp'>{label[k]}</th>" for k in keys) + "</tr><tr>" + "".join(
        "<th class='grp'>Cum</th><th>Day</th>" for _ in keys) + "</tr>"
    body = []
    for d in reversed(cum.index):
        cells = [f"<td class='date'>{pd.Timestamp(d).date()}</td>"]
        for k in keys:
            cv = cum.loc[d, k]
            cells.append(f"<td class='grp {'pos' if cv >= 0 else 'neg'}'>{cv:+.1%}</td>")
            cells.append(_pct(day_ret.loc[d, k]))
        body.append("<tr>" + "".join(cells) + "</tr>")
    table_html = f"<div class='tbl-wrap'><table class='cmp'><thead>{top}</thead><tbody>{''.join(body)}</tbody></table></div>"

    OUTPUT_PATH.mkdir(parents=True, exist_ok=True)
    if output_path is None:
        output_path = OUTPUT_PATH / "weekly_comparison_all_versions.html"
    output_path = Path(output_path)
    flat = pd.DataFrame(index=cum.index)
    for k in keys:
        flat[f"{label[k]} cumulative"] = cum[k]
        flat[f"{label[k]} weekly"] = wk_ret[k]
        flat[f"{label[k]} daily"] = day_ret[k]
    flat.round(5).to_csv(output_path.with_suffix(".csv"))

    final = cum.iloc[-1]
    summary = " · ".join(f"{label[k]} {final[k]:+.1%}" for k in keys)
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Weekly comparison — all versions</title>
<script>{get_plotlyjs()}</script>
<style>
  body {{ font-family: system-ui, Arial, sans-serif; margin: 24px; color: #222; max-width: 1150px; }}
  h1 {{ margin-bottom: 2px; }} .sub {{ color: #666; margin-top: 2px; }}
  .summary {{ background:#f3f7ff; border-left:4px solid #1f77b4; padding:10px 14px; margin:14px 0; font-size:13px; }}
  .tbl-wrap {{ max-height: 560px; overflow: auto; border: 1px solid #e0e0e0; border-radius: 4px; }}
  table.cmp {{ border-collapse: collapse; font-size: 12px; width: 100%; }}
  table.cmp th, table.cmp td {{ padding: 4px 8px; border-bottom: 1px solid #f0f0f0; text-align: right; white-space: nowrap; }}
  table.cmp thead th {{ position: sticky; top: 0; background:#f0f0f0; }}
  table.cmp td.date, table.cmp th.date {{ text-align: left; position: sticky; left: 0; background: #fff; }}
  table.cmp .grp {{ border-left: 2px solid #ddd; }}
  .pos {{ color:#1a7f37; }} .neg {{ color:#c0392b; }}
</style></head><body>
<h1>Weekly comparison — trailing {years} years, all strategy versions</h1>
<p class="sub">Conservative / Balanced / Growth vs S&amp;P 500, 60/40, T-bills (dotted). Cumulative return rebased to 0% at window start.</p>
<div class="summary"><b>Final cumulative ({cum.index[0].date()} → {cum.index[-1].date()}):</b> {summary}</div>
{chart_div}
<h2>Supporting data (weekly)</h2>
<p class="sub"><b>Cum</b> = cumulative return; <b>Day</b> = daily return on that date. Weekly return also in the CSV.</p>
{table_html}
</body></html>"""
    output_path.write_text(html, encoding="utf-8")
    return output_path


def _pct(v: float) -> str:
    if pd.isna(v):
        return "<td>—</td>"
    cls = "pos" if v >= 0 else "neg"
    return f"<td class='{cls}'>{v:+.2%}</td>"


def _table_html(dates, cum, wk_ret, day_ret) -> str:
    """Grouped-header table: one row per week, 3 columns (Cum/Wk/Day) per series."""
    top = "<tr><th class='date' rowspan='2'>Week ending</th>"
    for c in _SERIES:
        if c in cum:
            top += f"<th colspan='3' class='grp'>{_LABEL[c]}</th>"
    top += "</tr><tr>"
    for c in _SERIES:
        if c in cum:
            top += "<th class='grp'>Cum</th><th>Wk</th><th>Day</th>"
    top += "</tr>"

    body = []
    for d in reversed(dates):  # most recent first
        cells = [f"<td class='date'>{pd.Timestamp(d).date()}</td>"]
        for c in _SERIES:
            if c in cum:
                cv = cum.loc[d, c]
                cells.append(f"<td class='grp {'pos' if cv >= 0 else 'neg'}'>{cv:+.1%}</td>")
                cells.append(_pct(wk_ret.loc[d, c]))
                cells.append(_pct(day_ret.loc[d, c]))
        body.append("<tr>" + "".join(cells) + "</tr>")
    return f"<div class='tbl-wrap'><table class='cmp'><thead>{top}</thead><tbody>{''.join(body)}</tbody></table></div>"
