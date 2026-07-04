# Expanded-universe options downloader — calibration + ETA (2026-07-04)

**Status:** BUILT + CALIBRATED. Full pull and scheduled-task registration **await Andrew's
approval** of the ticker list + ETA below. Nothing full-scale has run.

Scripts (committed, new files only; `config.py` + frozen warehouse scope untouched):
- `datacollector/universe_download.py` — standalone, resumable, K=4-shard supervisor.
- `datacollector/universe_config.py` — the expanded universe + snapshot settings (additive;
  does NOT re-scope the running IBKR forward collector).

Terminal used **READ-ONLY** (pure HTTP GETs). Collector / scheduled tasks untouched.

---

## (a) Proposed universe — 140 roots (50 already on disk = top-up, 90 NEW)

| Group | Roots |
|---|---|
| Index + vol | SPX, SPXW, VIX, NDX, RUT, XSP |
| VIX ETPs | VXX, VIXY, **UVXY, SVXY** |
| Broad equity | SPY, QQQ, IWM, DIA, RSP, **VTI, MDY, EFA, EEM** |
| Sector SPDRs | XLB XLC XLE XLF XLI XLK XLP XLRE XLU XLV XLY |
| Industry/thematic ETFs | **SMH SOXX XBI IBB KRE XOP XRT XHB ITB JETS ARKK** |
| Credit / rates | HYG LQD JNK / TLT IEF SHY |
| Real assets | GLD SLV GDX **GDXJ** USO UNG |
| Mega-cap tech | AAPL MSFT NVDA AMZN META GOOGL **GOOG AMD NFLX ADBE CRM ORCL INTC QCOM MU CSCO TXN AMAT** AVGO TSLA |
| Financials | JPM V MA BRKB **BAC WFC GS MS C AXP SCHW COIN PYPL SQ** |
| Healthcare | LLY UNH **JNJ PFE MRK ABBV BMY AMGN GILD MRNA CVS** |
| Consumer/retail | **WMT HD COST MCD NKE SBUX TGT LOW DIS BABA** |
| Energy/industrials | XOM **CVX OXY SLB COP BA CAT GE DE UPS FDX** |
| High-IV momentum/meme | **PLTR SOFI RIVN LCID NIO MARA RIOT SMCI DKNG SNAP UBER ABNB SHOP ROKU GME AMC F T** |
| Comm/media | **CMCSA VZ TMUS** |

**Bold = 90 NEW roots** (not in the frozen warehouse). The 50 existing roots have their EOD
already on disk (2018-01-01 .. 2026-07-03, ~111k files) and are only topped up for missing
days — near-zero EOD work. All 140 get the NEW snapshot layer from scratch.

---

## (b) Measured rates (clean, uncontended, June-2023, 3-day samples)

**EOD layer** (reuses `download.pull_day` + `storage.write_day` verbatim — byte-identical
to the existing warehouse product):
- Fresh single-name EOD: **1.82 s/sym-day**, ~0.25 MB/sym-day (AMD).
- On-disk EOD sizes by class (exact): SPXW 1.60 MB, SPY 0.95, QQQ 0.84, SPX 0.79, NVDA 0.54,
  TSLA 0.53, AAPL/XLK 0.25 MB per day.

**Snapshot layer** (NEW; fixed-time consistent NBBO):
- The quote endpoint accepts `expiration=*` with `interval=15m` → the WHOLE 15m chain in ONE
  call. Filtering to the 4 target minutes + near-money band + 0-60 DTE happens in memory.
  This was the key optimization (a naive per-expiration loop was 6-16x slower):

  | Name | s/sym-day | MB/sym-day (4 files) | rows/sym-day |
  |---|---|---|---|
  | AAPL (liquid large-cap) | **2.58** | 0.034 | 872 |
  | TSLA (high-IV single) | **6.23** | 0.040 | 1,250 |
  | SPXW (heavy index) | **15.82** | 0.266 | 27,074 |

- **4 parallel shards hold** (per the standing terminal knee; SPXW single-call is 17s and
  never rate-limited). Round-robin root split → each shard 35 roots, disjoint + complete.

**Why the snapshot layer exists (verified):** `greeks/eod` stamps each contract's quote at
ITS own last-activity time — e.g. 15:58:12 for one AAPL strike, 13:57:16 for another on the
same day. The snapshot gives every leg at ONE instant (10:00 / 12:00 / 14:00 / 15:45 ET).

---

## (c) Extrapolated full-universe totals

Window 2018-01-01 .. present = **2,220 business days**. ~65% of sym-days carry data
(single-name option history starts ~2020; pre-listing/holiday days return empty in ~0.5s).

| Layer | Sym-days to pull | @1 shard | @4 shards (2.85x) | Disk |
|---|---|---|---|---|
| EOD (90 new roots) | ~199,800 (~120k real) | ~72 h | **~25 h (1.0 d)** | ~42 GB |
| EOD (50 existing) | top-up only | ~0 | ~0 | ~0 |
| Snapshot (all 140) | ~310,800 (~202k real) | ~253 h | **~89 h (3.7 d)** | ~16 GB |
| **TOTAL** | | | **~114 h (4.8 days)** | **~58 GB** |

---

## (d) Proposed schedule (finishes inside the 3-week window with ~390 h margin)

Run order (resumable — kill/restart any time, skips done work):
1. **Priority 1 — EOD, new roots only** (`--layer eod --only-new`, K=4): ~25 h. Unlocks the
   full-universe EOD chains for analysis first.
2. **Priority 2 — Snapshots, all 140 roots** (`--layer snap`, K=4): ~89 h, chunked by
   root-group so a group lands complete. Heavy indices (SPX/SPXW) first so 0DTE snapshots are
   usable soonest.
3. Existing-root EOD top-up runs implicitly (near-instant skip-checks).

Total ~114 h ≈ 5 days of wall-clock at 4 shards — comfortably inside the 3-week (504 h) paid
window. Recommend running it as a **whether-logged-on scheduled task** (the supervisor is
detached + self-restarting), with the worker-watch / heartbeat rubric applied.

## (e) Disk-space check

`C:\TradingDesk-Local` free: **201 GB**. Estimated need: **~58 GB**. Ample headroom (leaves
~143 GB). No cleanup needed.

## (f) Risks / notes

- **Terminal load vs the running collector:** K=4 is the measured knee; snapshot calls are
  short (2-17 s) and the supervisor backs off on 429/error bursts. Still, running this
  concurrently with the SPXW 1-min collector would oversubscribe the terminal — schedule it
  for a window when the 1-min backfill is idle, or accept slower throughput.
- **Single-name history depth:** most single names start ~2020 on ThetaData; pre-2020 days
  are empty (fast, marked done). Real coverage per single name ≈ 2020-present.
- **Snapshot transfer volume:** the `expiration=*` call pulls the full chain then filters in
  memory — larger transfer than a targeted pull, but far fewer round-trips (net faster). Only
  the near-money band is written to disk, so on-disk size stays small (~16 GB total).
- **Weekend/holiday gaps:** handled — empty days write 0-row markers and count as done.
- **Ticker validity:** a handful of newer names (COIN 2021, RIVN/LCID 2021, SOFI 2021,
  PLTR 2020, ARKK etc.) simply have shorter histories; ABNB/SNAP/UBER/SHOP fine. No root here
  is expected to be invalid, but the first pass will surface any root the terminal doesn't map
  (logged, left un-done, harmless).
