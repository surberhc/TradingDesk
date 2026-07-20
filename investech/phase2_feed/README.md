# Phase-2 Breadth-Based "Leadership Proxy" Feed

A transparent, public/licensed-data approximation of the *behavior* of
InvesTech Research's proprietary **Negative Leadership Composite (NLC)** —
their bullish **"Selling Vacuum"** vs bearish **"Distribution"** read on market
internals.

## What this is (and is NOT)

InvesTech's NLC is **proprietary**: their exact component weights and regime
thresholds are undisclosed. We do **not** have them and we do **not** scrape
InvesTech.

What *is* public is the NLC's **inputs** — standard market **breadth**:
advance/decline, new 52-week highs vs lows, and participation (% of stocks above
moving averages). This feed rebuilds a **directionally similar, fully
transparent, tunable proxy** from those public inputs, computed over the S&P 500
universe using the desk's **licensed Tiingo EOD** prices.

> **DISCLAIMER — APPROXIMATION, NOT A CLONE.** This "Leadership Proxy" mimics the
> *direction* of the NLC (broadening participation / absent downside leadership =
> bullish "Selling-Vacuum-like"; expanding new lows / falling A/D / dropping
> %>MA = bearish "Distribution-like"). It does **not** reproduce InvesTech's
> proprietary internals, will not match their exact values, and should be read as
> an independent breadth gauge — never as a substitute for the NLC.

## Data source & caveats

- **Prices (two interchangeable bulk EOD sources):**
  - **ThetaData local Terminal (PREFERRED when up):** the desk's licensed
    ThetaData Terminal serves bulk EOD/history locally over REST at
    `http://127.0.0.1:25510` with **no free-tier hourly cap**, so it is the
    preferred source. ThetaData has **no cloud REST API** — the Java Terminal
    must be running and logged in with `THETADATA_API_KEY`; once logged in it
    serves data **without** the key in the request (we never put the key in a URL
    or print it). See **Switching price sources** and **`thetadata.py`** below.
  - **Tiingo End-of-Day (`/tiingo/daily/<ticker>/prices`), fallback:** licensed
    desk key. We use **adjusted close** (`adjClose`) so moving averages and
    52-week high/low logic are split/dividend consistent.
  - **ThetaData vs Tiingo adjustment note:** ThetaData stock EOD is
    split-adjusted but **not dividend-adjusted**, whereas the Tiingo path uses
    fully-adjusted `adjClose`. For breadth (MA crossovers, 52-week hi/lo, daily
    up/down counts) this difference is immaterial; it would matter for total-
    return work, not for this gauge.
  - The **source switch and fallback** are governed by `config.DATA_SOURCE`
    (`auto` / `thetadata` / `tiingo`). A down Terminal **always** degrades to
    Tiingo + cache, so the run never aborts. See **Switching price sources**.
- **Universe:** S&P 500 constituents.
  - **Primary (reproducible):** committed static list
    `data/sp500_constituents.csv` (column `Symbol`).
  - **Fallback (live):** the public Wikipedia *"List of S&P 500 companies"*
    table, scraped with stdlib only.
  - **Staleness risk:** the static CSV is a point-in-time snapshot. Index
    membership drifts (adds/drops, ticker changes, M&A). Refresh it periodically
    or rely on the Wikipedia fallback, which itself can lag real index changes.
    Ticker share-class symbols are normalized `.`→`-` for Tiingo (e.g. `BRK.B`→
    `BRK-B`).
- **Rate limits:** Tiingo's tier is limited (the key in use here returns
  HTTP 429 `"You have run over your hourly request allocation"` once the hourly
  budget is spent — a full 503-ticker pull can exhaust it). We **do not hammer
  it**: one EOD request per *uncached* constituent, with a small pause
  (`TIINGO_REQUEST_PAUSE`, default 0.10s) between real fetches. The **EOD cache**
  (below) is the main mitigation — repeat runs re-fetch only what is missing or
  stale. For quick scaffold/test runs you can still request a **subset**
  (`PHASE2_UNIVERSE_LIMIT`, e.g. 40) and the output is clearly labelled a subset
  sample.
- **EOD cache:** each ticker's fetched series is written to
  `data/cache/<TICKER>.csv` (`date,close,high,low`, adjusted). A run **reuses**
  a cache file when it is fresh (newest bar ≥ the most recent expected trading
  day) and only fetches missing/stale tickers; if a fetch fails (e.g. 429) but a
  cache file exists, the cached series is used so the ticker still contributes.
  Force a full re-pull with `PHASE2_FORCE_REFRESH=1`. See **EOD cache** below.
- **Breadth scope caveat:** by default the four breadth %s are computed over the
  **S&P 500 large-cap universe only** — *not* full NYSE/NASDAQ exchange breadth
  (the broader measure InvesTech-style composites traditionally use). With the
  ThetaData Terminal up you can switch to the **broad** universe
  (`PHASE2_UNIVERSE_SOURCE=thetadata`), which also lights up a real exchange-
  breadth sub-score. See **S&P-500 vs broad universe**.

## The composite formula (exact, transparent, tunable)

For each constituent with ≥ `MA_LONG` (200) daily closes we compute per-name
booleans:

- `above_50`  = last close > 50-day SMA
- `above_200` = last close > 200-day SMA
- `new_high_52w` = last close ≥ max(trailing 252-day window)
- `new_low_52w`  = last close ≤ min(trailing 252-day window)
- `advanced` = last close > previous close
- `declined` = last close < previous close

Aggregated across the `N` usable constituents:

```
pct_above_50dma    = 100 * (# above_50)  / N
pct_above_200dma   = 100 * (# above_200) / N
new_highs_52w      = # new_high_52w
new_lows_52w       = # new_low_52w
net_highs_lows     = new_highs_52w - new_lows_52w
net_highs_lows_pct = 100 * net_highs_lows / N          # in [-100, +100]
advances           = # advanced
declines           = # declined
ad_net             = advances - declines
ad_pct             = 100 * ad_net / N                   # in [-100, +100]
ad_line_cumulative = previous ad_line + ad_net          # accumulates across runs
```

Each component is normalized to a **0..100 sub-score**:

```
s_pct50   = clamp(pct_above_50dma,   0, 100)
s_pct200  = clamp(pct_above_200dma,  0, 100)
s_netHL   = clamp((net_highs_lows_pct + 100) / 2, 0, 100)
s_ad      = clamp((ad_pct           + 100) / 2, 0, 100)
```

The **Leadership Proxy (0..100)** is the weighted blend (weights in
`config.PROXY_WEIGHTS`, must sum to 1.0):

```
leadership_proxy =
      0.25 * s_pct50        # participation, short trend
    + 0.25 * s_pct200       # participation, long trend
    + 0.30 * s_netHL        # leadership: new-high vs new-low dominance
    + 0.20 * s_ad           # daily advance/decline tilt
```

(If a component is missing it is dropped and the remaining weights are
renormalized.)

**Optional 5th input — true exchange breadth (provisional).** When a real
NYSE/NASDAQ exchange-breadth reading is obtained (see below), it is blended as an
additional 0..100 sub-score with provisional weight
`config.EXCHANGE_BREADTH_WEIGHT` (default 0.25); the four S&P-500 weights are
scaled down *pro rata* to make room. When no live reading is available (the
current state in this environment), `exchange_score=None` and the proxy is
**identical** to the S&P-500-only blend above — the existing path is unchanged.
> **These weights are PROVISIONAL.** The exchange-breadth weight and the four
> S&P-500 weights will be set in the upcoming **calibration** step against
> InvesTech's published NLC values — do not treat the 0.25 as final.

**Regime label** (cut points in `config`):

```
leadership_proxy >= 60  ->  "Selling Vacuum (bullish)"      # Selling-Vacuum-like
leadership_proxy <= 40  ->  "Distribution (bearish)"        # Distribution-like
otherwise               ->  "Neutral"
```

All weights, MA windows, the 52-week window, and the regime cut points live in
`config.py` and are meant to be tuned (see **Calibration**).

## Switching price sources (ThetaData ⇄ Tiingo)

The bulk EOD/history source is chosen by **`config.DATA_SOURCE`** (env override
`PHASE2_DATA_SOURCE`):

| `DATA_SOURCE` | Behavior |
|---------------|----------|
| `auto` (default) | Use the **ThetaData Terminal if it is up** at run start, else Tiingo. |
| `thetadata` | **Prefer ThetaData**; fall back to Tiingo per-ticker when the Terminal is down or a symbol has no data. |
| `tiingo` | Tiingo only (original behavior). |

In every mode the **EOD cache** (`data/cache/<TICKER>.csv`) is used identically —
ThetaData-fetched series cache exactly like Tiingo ones (same
`date,close,high,low` columns) — and a **down Terminal never aborts the run**:
the per-ticker fetch falls through to Tiingo, then to any cache present. The run
banner prints `Price source : <thetadata|tiingo>`.

`thetadata.py` implements the integration (stdlib `urllib` only):

- `is_terminal_up()` — quick health probe of `GET /v2/system/mdds/status`
  (short timeout); returns `False` cleanly when the Terminal is down (connection
  refused), never raises.
- `get_eod_history(ticker, start, end)` → `[{date, close, high, low}, …]` via
  `GET /v2/hist/stock/eod`, reading columns **by name** from the response header
  `format` array and following v2 `next_page` pagination. Raises
  `TerminalUnavailable` if the Terminal is down so callers can fall back.
- `get_universe()` — pulls the **broad** stock-roots list from
  `GET /v2/list/roots/stock` (see below).

### ThetaData endpoints implemented

| Purpose | Method & path | Key params |
|---------|---------------|-----------|
| Health | `GET /v2/system/mdds/status` | — |
| Broad roots | `GET /v2/list/roots/stock` | — |
| Stock EOD | `GET /v2/hist/stock/eod` | `root`, `start_date=YYYYMMDD`, `end_date=YYYYMMDD` |

v2 JSON envelope: `{"header": {"format": [...], "next_page": null|url}, "response": [[...], ...]}`.
The EOD `format` order is `ms_of_day, ms_of_day2, open, high, low, close, volume,
count, bid_size, bid_exchange, bid, bid_condition, ask_size, ask_exchange, ask,
ask_condition, date` (`date` is an integer `YYYYMMDD`). We index `close/high/low/
date` **by name** so a column reorder won't break parsing. Docs:
<https://http-docs.thetadata.us/operations/get-v2-hist-stock-eod.html>,
<https://http-docs.thetadata.us/operations/get-v2-list-roots.html>.

## S&P-500 vs broad universe (and True NYSE/NASDAQ exchange breadth)

The breadth universe is chosen by **`config.UNIVERSE_SOURCE`** (env override
`PHASE2_UNIVERSE_SOURCE`):

- **`sp500` (default):** the S&P 500 constituent list (static CSV → Wikipedia
  fallback). Stable, licensed, reproducible — **but it is not what InvesTech's
  NLC uses.** It is 500 large-caps only.
- **`thetadata`:** the **broad NYSE/NASDAQ stock-roots** list from the local
  Terminal (`/v2/list/roots/stock`) — thousands of issues. **This is the
  tradeoff that matters:** InvesTech's Negative Leadership Composite is built on
  **full-exchange** breadth (every listed issue), where small-caps and secondary
  issues drive the new-low / downside-leadership signal the NLC is famous for. A
  broad universe is a far closer approximation; the cost is many more EOD calls
  (cap it with `PHASE2_THETADATA_UNIVERSE_LIMIT`) and inclusion of ETFs/ADRs/
  illiquid names unless filtered. Falls back to `sp500` if the Terminal is down.

**True exchange-breadth sub-score.** The exchange-breadth sub-score
(`exchange_breadth_score` + `exchange_breadth_status` in the CSV, toggled by
`config.EXCHANGE_BREADTH_ENABLED`) is now computed **for real** when the run uses
the broad ThetaData universe: the issue-level advance/decline and 52-week
new-high/new-low tallies across that universe **are** exchange-wide, and they are
mapped via
`(adv-dec)/(adv+dec)` and `(nh-nl)/(nh+nl)` into the 0..100 sub-score
(`breadth.exchange_breadth_score_from_tallies`). Over the S&P-500 subset this
would merely restate large-cap breadth, so the function honestly reports
`unavailable` there rather than mislabel it. It degrades gracefully (status
`"unavailable"`, proxy stays S&P-500-only) when the Terminal is down, and **never
fabricates** a value.

> **Live verification pending.** The ThetaData Terminal was **not running** when
> this integration was built (connection refused at `127.0.0.1:25510`), so the
> code was written and **structurally** validated only — the live broad-universe
> and exchange-breadth paths have **not** been verified against real ThetaData
> responses yet. See **Verifying live**.

### Verifying live (once the Terminal is running)

Start the ThetaData Terminal (logged in with `THETADATA_API_KEY`), then from
`phase2_feed/` run this single command (cmd) — it forces ThetaData as the
source over a small broad-universe slice and lights up the real exchange-breadth
sub-score:

```cmd
cd /d "C:\Users\andre\My Drive (andrew@surberhc.com)\TradingDesk\investech\phase2_feed"
set PHASE2_DATA_SOURCE=thetadata
set PHASE2_UNIVERSE_SOURCE=thetadata
set PHASE2_THETADATA_UNIVERSE_LIMIT=50
"C:\Python314\python.exe" main.py
```

Expect the banner to show `Price source : thetadata`, a `ThetaData broad stock
roots` universe, and `Exchange breadth: ok` with a numeric sub-score. Remove
`PHASE2_THETADATA_UNIVERSE_LIMIT` (or set it to `0`) for the full broad universe.

## Output

`main.py` appends **one row per day** to `data/leadership_daily.csv`, de-duped by
`date` (re-running on the same day overwrites that day's row). Columns:

```
date, run_timestamp, status, subset, universe_source,
universe_count, pct_above_50dma, pct_above_200dma,
new_highs_52w, new_lows_52w, net_highs_lows, net_highs_lows_pct,
advances, declines, ad_net, ad_line_cumulative,
exchange_breadth_score, exchange_breadth_status,
leadership_proxy, regime
```

`status` is `ok`, `needs_api_key`, or `error: ...`. `subset=yes` flags a
scaffold/subset run.

## How to run

```bash
cd phase2_feed
python main.py            # stdlib only — no pip install required
```

The **operational default is the FULL S&P 500** (`UNIVERSE_LIMIT = None`,
~503 EOD requests on a cold cache; near-instant once cached). To run a fast
subset instead (scaffold/test), set a positive limit — the output is clearly
labelled a subset:

```bash
PHASE2_UNIVERSE_LIMIT=40 python main.py        # bash; 0/empty => full universe
```

cmd:

```cmd
set PHASE2_UNIVERSE_LIMIT=40
python main.py
```

### EOD cache

Fetched series are cached at `data/cache/<TICKER>.csv` (`date,close,high,low`,
adjusted). On each run, fresh cache files are reused and only missing/stale
tickers are fetched — so the first full run pays ~503 requests and subsequent
runs the same trading day are essentially free (and re-runnable for the
calibration step without re-pulling 503 names). The run banner prints
`EOD cache: N reused, M fetched`.

Force a complete re-pull (ignore + overwrite the cache):

```bash
PHASE2_FORCE_REFRESH=1 python main.py
```
```cmd
set PHASE2_FORCE_REFRESH=1
python main.py
```

To clear the cache entirely, delete `data/cache/`.

### Environment variables

| Var | Purpose | Default |
|-----|---------|---------|
| `TIINGO_API_KEY` | Tiingo token. Read from the desk `.env` if not already in the environment; an existing env var **wins**. **Never printed.** | from `.env` |
| `PHASE2_ENV_PATH` | Override path to the desk `.env`. | `C:\TradingDesk-Local\secrets\.env` |
| `PHASE2_UNIVERSE_LIMIT` | Subset size for a run; `0` or empty => full universe. | full (`None`) |
| `PHASE2_FORCE_REFRESH` | Any truthy value re-fetches every ticker and overwrites the cache. | unset (use cache) |
| `THETADATA_API_KEY` | ThetaData token (read from `.env`, **never printed**). The local Terminal handles auth once logged in, so this is **not** placed in any request — it is read only to flag when the source is unconfigured. | from `.env` |
| `THETADATA_BASE_URL` | ThetaData local Terminal REST base. | `http://127.0.0.1:25510` |
| `PHASE2_DATA_SOURCE` | Bulk price source: `auto` (Theta if up, else Tiingo), `thetadata` (prefer Theta, Tiingo fallback), or `tiingo` (Tiingo only). | `auto` |
| `PHASE2_UNIVERSE_SOURCE` | Breadth universe: `sp500` (S&P 500 list) or `thetadata` (broad NYSE/NASDAQ roots; needs the Terminal). | `sp500` |
| `PHASE2_THETADATA_UNIVERSE_LIMIT` | Cap on the **broad** ThetaData universe; `0`/empty => all roots. | full (`None`) |

The `.env` loader (`env_loader.py`) parses simple `KEY=value` lines, stripping
whitespace, surrounding quotes, and stray `\r`; it never overwrites an existing
env var and **never echoes any value**.

## Calibration (do later — not run here)

We hold InvesTech's actual published **monthly** NLC values/regimes in
`C:\Users\andre\My Drive (andrew@surberhc.com)\TradingDesk\investech\_dataset\InvesTech_Signals.csv`
(columns `NLC Value`, `NLC Regime`, `Issue Date`). To tune this proxy:

1. Run the feed daily (full universe) to accumulate `data/leadership_daily.csv`,
   or backfill historically by pulling EOD windows ending on each InvesTech
   issue date.
2. For each `Issue Date`, take the proxy's reading on/near that date and pair it
   with that row's `NLC Value` / `NLC Regime`.
3. Compare **turning points and regime flips**, not absolute levels (different
   scales). Check rank correlation of `leadership_proxy` vs `NLC Value`, and a
   confusion matrix of proxy regime vs `NLC Regime`
   (Selling Vacuum / Distribution / transitional).
4. Tune `PROXY_WEIGHTS`, `REGIME_BULL_MIN`, `REGIME_BEAR_MAX`, and the MA windows
   in `config.py` to best align the proxy's flips with InvesTech's known turns.
   Treat the `_dataset` file as **read-only reference** — it is never scraped or
   modified by this feed.

## TODO

- ~~**Scale to the full universe**~~ — **done**: `UNIVERSE_LIMIT = None` is now
  the operational default; the EOD cache keeps repeat runs cheap.
- ~~**Real exchange breadth (light up the stub):**~~ — **wired**: with the
  ThetaData Terminal up + `PHASE2_UNIVERSE_SOURCE=thetadata`, exchange breadth is
  computed bottom-up from broad-universe issue-level A/D + NH/NL. **Live
  verification is still pending** the Terminal being up (see **Verifying live**).
- **Calibration:** run the calibration below against `InvesTech_Signals.csv` to
  set `PROXY_WEIGHTS`, the **provisional** `EXCHANGE_BREADTH_WEIGHT`, and the
  regime thresholds.
- **Refresh the constituent list** on a schedule to limit staleness.

## Files

```
phase2_feed/
  README.md                       this file
  config.py                       universe source, UNIVERSE_LIMIT, MA windows,
                                  secrets path, weights, thresholds
  env_loader.py                   .env loader (never prints the key)
  thetadata.py                    ThetaData local-Terminal client (health,
                                  stock EOD history, broad roots universe)
  breadth.py                      universe + price pull (Theta/Tiingo) + metrics
  main.py                         orchestrate, print table, write CSV
  requirements.txt                stdlib-only; requests optional
  data/
    sp500_constituents.csv        static universe snapshot (Symbol column)
    leadership_daily.csv          appended daily output
    cache/<TICKER>.csv            per-ticker EOD cache (date,close,high,low)
  fetchers/breadth.py             deprecated shim -> top-level breadth.py
```
