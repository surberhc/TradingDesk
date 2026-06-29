# Remaining-Window Data Grab Plan

*For Andrew's review & approval. Plain-English. Nothing has been pulled yet — this is a plan to approve first.*
*Drafted 2026-06-29.*

---

## The situation, in one paragraph

We rented a firehose. The ThetaData options subscription gives us full options-market history, but it's a **rented clock** — it started ~June 25 and shuts off ~**July 25** (~26 days left). Our standing rule is **"grab everything we'll ever want NOW, because we never want to re-subscribe."** Two big grabs are already in the bag or nearly so: (1) the **end-of-day options history is DONE** (50 symbols, 8 years, ~33 GB — this is what let us validate the S5 tail hedge on real prices), and (2) an **intraday minute-by-minute SPXW pull is running right now**, finishing ~June 30. That leaves roughly **3.5 weeks of paid access after June 30** to grab anything else worth having forever. This document lists every candidate, what it unlocks, how big/slow it is, and whether it fits before the clock runs out — so you can just check boxes.

**My recommendation in one line:** after the current pull finishes, immediately grab **intraday SPY + XSP** (small, fast, unblocks the condor strategies S2/S3), do the **one-day NDX re-probe** before we ever cancel (free, you flagged "don't drop"), and **decide** on the one judgment call below — extending intraday SPXW further back in time. Everything else is nice-to-have or skip.

---

## What we already have (no action needed)

| Already grabbed | What it is | Status |
|---|---|---|
| **EOD options, 50 roots, 2018→2026** | Daily per-strike greeks/IV/open-interest/bid-ask, ~33 GB | **DONE** — unblocked S5 real-skew validation |
| **Intraday SPXW 1-min, 2022→2026** | Minute bars, all SPXW expirations incl. 0DTE, all strikes | **~55% done, finishing ~June 30 evening** |

---

## The grab plan — prioritized

### Priority key
- 🟢 **MUST-GRAB** — clear strategy need, fits the window, get it.
- 🟡 **NICE-TO-HAVE** — useful but not blocking; grab if time/space allows.
- 🔵 **PROBE-ONLY** — don't bulk-pull; just run a quick check to make a decision.

---

### 🟢 1. Intraday SPY + XSP (1-minute) — MUST-GRAB

- **What it is:** the same minute-by-minute options data we're pulling for SPXW, but for **SPY** (the big ETF) and **XSP** (Mini-SPX, 1/10th size, cash-settled, tax-advantaged).
- **Why we want it:** the **condor income strategies (S2 and S3)** are designed to trade SPX/SPXW *and* SPY/XSP side-by-side. SPXW alone can't fully test them. XSP in particular is the tax-smart vehicle for S3's covered-call mode. Without these two, S2/S3 backtests are stuck on a single instrument.
- **Size / time:** **small** relative to SPXW. SPY and XSP have far fewer strikes/expirations than SPXW's full 0DTE chain. Estimate **a few GB each, ~1–2 days total** for both over the same 2022→2026 window. **Fits the window easily** (done well before July 25).
- **Recommendation:** **GRAB.** Kick off right after the SPXW pull finishes (~June 30).

---

### 🔵 2. Extend intraday SPXW further BACK than 2022 — PROBE FIRST, then decide

- **What it is:** pulling SPXW minute data for years **before 2022** (e.g. 2018–2021).
- **The catch:** **daily 0DTE expirations didn't fully exist before 2022.** CBOE rolled out same-day SPX expirations gradually (Mon/Wed added 2022, Tue/Thu added 2022 — before that only certain days existed, and earlier still only weekly). So the deeper we reach back, the **fewer 0DTE days there actually are to pull** — we may be paying time/space for data that's mostly empty for the 0DTE strategies that need it.
- **Why we might still want it:** more history = more market regimes to test condors against (2018 vol spike, 2020 COVID). But the **EOD options grab already covers 2018→2022** for the lower-frequency views, so the gap is specifically *intraday* paths in those years.
- **Size / time:** **potentially large and slow** — full-chain intraday for extra years could be many GB and several days, and it's the most likely item to bump against the July 25 deadline if we start it late.
- **Recommendation:** **PROBE-ONLY first.** Run a quick check (a handful of dates in 2018/2019/2020/2021) to see **how many 0DTE expirations actually exist** back there. If it's rich → consider a bounded pull (0DTE + near-money only, not full chain). If it's sparse → **skip it**, we're not missing much. **← This is the one real judgment call for you (see Open Questions).**

---

### 🔵 3. NDX re-probe BEFORE any cancellation — PROBE-ONLY (free, do it)

- **What it is:** a **one-day check**, not a bulk pull. NDX (Nasdaq-100 index options) history **does not exist at our subscription tier** — only the last ~7 weeks come back; every older date returns nothing. QQQ is our historical Nasdaq proxy and we already have it in full.
- **Why we re-check:** **you explicitly said "don't drop" NDX.** Data vendors sometimes extend coverage. Before we ever cancel the subscription, we re-probe NDX one last time in case upstream coverage changed and the full history quietly became available — if so, *that* would be a must-grab.
- **Size / time:** **near-zero** — it's a few test queries, minutes of work.
- **Recommendation:** **DO IT, as the last step before any cancel.** If history appeared → escalate to a real NDX grab. If still only 7 weeks → confirmed structural, nothing to grab, QQQ remains the proxy.

---

### 🟡 4. Other condor-relevant roots (EOD already covered) — NICE-TO-HAVE / likely SKIP

- **What it is:** any *additional* underlyings the condor strategies might eventually want intraday (beyond SPX/SPXW/SPY/XSP).
- **Why probably not:** S2/S3 as specced are built on the SPX-complex (SPXW/SPY/XSP). We don't have a concrete strategy asking for a *different* intraday root right now. The **EOD grab already captured 50 roots**, so the lower-frequency need is met.
- **Recommendation:** **SKIP unless** you name a specific root you want intraday. No strategy currently demands it, so I wouldn't spend window-time here.

---

### 🟡 5. Intraday underlying-index history for S3's gap studies — NICE-TO-HAVE (likely derivable)

- **What it is:** intraday price path of the underlying index itself (overnight gap size, morning range, intraday realized vol) for S3's "wait & measure" morning-gap research.
- **Why maybe not a separate grab:** we can likely **derive this from the intraday options pull we're already getting** (the options data carries underlying spot). A dedicated separate index/stock feed would be a *different* ThetaData product we deliberately chose not to buy.
- **Recommendation:** **NICE-TO-HAVE, but try to derive first.** Don't buy a new product for it. Revisit only if the derived version proves insufficient.

---

## Quick decision table

| # | Grab | Priority | Size | Fits before 7/25? | My call |
|---|---|---|---|---|---|
| 1 | Intraday **SPY + XSP** 1-min (2022→2026) | 🟢 MUST | Small (few GB) | Yes, easily | **Grab** |
| 2 | Extend intraday **SPXW pre-2022** | 🔵 PROBE | Large if pulled | Risky if started late | **Probe, then you decide** |
| 3 | **NDX re-probe** before cancel | 🔵 PROBE | ~Zero | Yes | **Do it (last step)** |
| 4 | Other condor roots intraday | 🟡 NICE | Unknown | — | **Skip unless named** |
| 5 | Intraday underlying for S3 gap study | 🟡 NICE | Small | Yes | **Derive, don't buy** |

---

## Open questions for you to decide

1. **The pre-2022 SPXW reach-back (#2) — the only real judgment call.** After the probe shows how many 0DTE days actually exist back there: do you want to spend window-time and disk on a *bounded* (0DTE + near-money) pull for the extra regimes (2018 vol, 2020 COVID intraday)? Or is the existing 2022→2026 intraday + 2018→2026 EOD enough? My lean: **only if the probe shows meaningful 0DTE coverage AND we start it with ~2 weeks of cushion.**

2. **Cancel timing.** Once #1 (SPY+XSP) finishes and #3 (NDX re-probe) is done, we will have grabbed everything with a current justification. **Do you want to cancel the subscription immediately at that point, or ride it to the July 25 auto-lapse** as a safety margin in case a new need surfaces? (No cost difference if it's already paid through 7/25 — riding it out is free insurance.)

3. **Disk headroom.** All raw data lives **local** at `C:\TradingDesk-Local\warehouse` (never synced to Drive). Worth a quick confirm there's room for SPY+XSP (a few GB) and any pre-2022 reach-back before we start. (I can check this on request.)

---

## Suggested sequence (if approved)

1. **~June 30:** SPXW intraday pull finishes on its own. *(in flight — don't disturb)*
2. **Immediately after:** start **SPY + XSP intraday** (#1). ~1–2 days.
3. **In parallel / quick:** run the **pre-2022 0DTE probe** (#2) → bring the result back to you for the go/no-go.
4. **Before any cancel:** run the **NDX re-probe** (#3).
5. **Then:** you decide cancel-now vs ride-to-July-25 (Open Q #2).

*Nothing above runs until you approve. None of it disturbs the in-flight SPXW collector.*
