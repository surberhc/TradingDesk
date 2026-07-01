# Sharadar one-month bulk-pull plan (CAN SLIM historical backtest)

**Date:** 2026-07-01
**Goal:** One-TIME historical download for a CAN SLIM growth-stock backtest on obscure US
small/mid-caps + delisted names, 1998–2026. Needs: point-in-time/as-reported fundamentals
(restatement-safe), survivorship-free universe, daily prices, corporate actions. Live system
runs off IBKR, so this data is needed ONCE. Data lands in a local flat-file warehouse.

---

## BOTTOM LINE (read this first)

- **Package to buy:** Sharadar **Core US Equities Bundle — SKU `SFA`** on Nasdaq Data Link.
  It bundles the tables we need (SF1 fundamentals, SEP prices, TICKERS, ACTIONS, SP500, EVENTS,
  SF2 insiders, SF3 institutions) at preferential bundle pricing vs. buying tables individually.
- **Price:** **LOGIN-GATED. No honest public number exists** — Sharadar has not published pricing
  anywhere (confirmed by Datarade, QuantRocket, and Nasdaq Data Link, all of which show
  "Log in / create account to see pricing" or "Select license to see pricing"). I did **not**
  fabricate a figure. Exact 2-minute click-path to see it yourself is below (Section 2).
- **LICENSE VERDICT — THE KEEP-AFTER-CANCEL PLAN IS DEAD.** Sharadar's own license requires you
  to **delete all copies of the data — AND all datasets derived from it — within 30 days of
  termination**, with an affidavit-on-demand clause. The one-month-then-cancel-and-keep plan
  **violates the license.** See Section 3 for the verbatim quote. This is the make-or-break item
  and it breaks.
- **Professional gotcha:** "professional activities of any sort" forces the **Professional tier**.
  Because this research is bound for a real-money strategy (currently paper), we do **not** cleanly
  qualify as Non-Professional. Professional pricing is materially higher (gap login-gated).
- **Fallback if we still want to keep data:** **EODHD All-In-One — $99.99/mo, published, cancel
  after 1 month, bulk download, retention allowed** — with one honest caveat: its point-in-time /
  restatement handling is unconfirmed (look-ahead risk for as-reported fundamentals).

---

## 1. EXACT PACKAGE

**Recommendation: buy the bundle, SKU `SFA` (Sharadar "Core US Equities Bundle").** The bundle
exists specifically to give preferential pricing when you need multiple tables, which we do. Buying
SF1 + SEP + the metadata tables à la carte is the same data at a worse price.

Tables we actually need for CAN SLIM (all inside SFA):

| Table | What it is | CAN SLIM role | Need? |
|-------|-----------|---------------|-------|
| **SF1** | Core US Fundamentals, with as-reported/point-in-time dimensions | **C, A** (earnings growth) | **YES — the core.** Use dimension **ARQ** (As-Reported Quarterly) / **ART** (As-Reported TTM) for restatement-safe, point-in-time values. MR* dims are most-recent-restated → look-ahead; avoid. |
| **SEP** | Sharadar Equity Prices — daily EOD, survivorship-free, since 1998 | **N, L** (price/new-highs, RS) | **YES.** |
| **TICKERS** | Ticker metadata + `permaticker` (survivorship-safe ID across renames/relistings) | universe construction, join key | **YES.** |
| **ACTIONS** | Splits, dividends, corporate actions | price/return adjustment | **YES.** |
| **SP500** | Historical S&P 500 constituents (adds/removals) | benchmark / universe screens | Nice-to-have (in bundle). |
| **EVENTS** | SEC Form 8-K corporate events | context | Bundled; optional. |
| **SF2** | Insider holdings/transactions | tangential to "I" | Bundled; optional. |
| **SF3** | 13F institutional ownership (since 2013) | **"I"** (institutional sponsorship) | **Marginal.** SF3 only starts **2013**, so it does NOT cover the 1998–2012 half of the backtest — you cannot build a consistent point-in-time "I" factor across the full window from it. Take it because it's in the bundle, but don't design the core signal around it, and don't buy it standalone. |

**Verdict:** one SKU — **SFA**. Don't assemble individual tables.

---

## 2. REAL PRICE, MONTH-TO-MONTH

**Confirmed: pricing is login-gated on every primary source. There is no reliable published number
to quote, and I refused to invent one.**

- Nasdaq Data Link SFA page and its `/pricing` sub-page render "Log in or create account to see
  pricing" and "Select license to see pricing" (Professional / Non-Professional selector).
- QuantRocket's Sharadar pricing page (a reseller that often publishes figures) shows the same
  placeholders — "Log in or create account to see pricing."
- Datarade states outright: **"Sharadar has not published pricing information for their data services."**
- **Month-to-month IS offered.** Nasdaq Data Link premium databases (Sharadar included) show both
  **Monthly and Annual billing** options, and premium subscriptions can be **cancelled at any time**
  from the account page (see Section 3). So a 1-month subscription is structurally available.

**Exact ~2-minute click-path for Andrew to see the real number:**
1. Go to **https://data.nasdaq.com** → create a free account (or log in).
2. Open the bundle page: **https://data.nasdaq.com/databases/SFA**
3. Click **Pricing** (or go directly to **https://data.nasdaq.com/databases/SFA/pricing**).
4. Toggle the **license selector** between **Non-Professional** and **Professional**, and the
   **billing toggle** between **Monthly** and **Annual**. The four resulting numbers are the real
   month-to-month and annual prices for each tier. (Note the Professional-tier price for the
   real-money-bound gotcha in Section 4.)

*(If you paste those four numbers back to me I'll fold them into this doc.)*

---

## 3. LICENSE — THE MAKE-OR-BREAK QUESTION → **FAILS**

**Can we bulk-download during the paid month and KEEP AND USE the data after cancelling? NO.**

Sharadar's License Terms of Use, **Section 9 (Termination)**, verbatim:

> "within thirty (30) days of termination, you will delete from all computer systems owned or
> operated by you, and from all computer systems owned or operated on your behalf, **all copies of
> the Services Data, and all data sets derived from the Services Data**"

and:

> "if requested by Sharadar, you shall promptly provide Sharadar with an **affidavit certifying
> compliance** with the provisions of this clause."

This is decisive and loud: **the one-month-then-cancel-and-keep plan violates Sharadar's license.**
The delete obligation explicitly extends to **derived datasets** — so even a cleaned/derived local
warehouse or a factor panel built from the raw pull must be purged. There is no perpetual /
buy-out-the-history option in these terms; a subscription buys *access while subscribed*, not a
permanent copy.

Related terms (QuantRocket's resale wording, which mirrors Sharadar's):
- **Internal/scoped use only:** "You may not use the Services or the Services Data ... in relation
  to activities conducted outside of the [licensed] service."
- **No redistribution:** "you may not transfer or make available access to either the Services or
  the Service Data to others. You may not publish, disseminate, re-distribute or share the
  Services Data."
- Nasdaq Data Link's general terms echo the same: on termination/expiration, all rights to use the
  data terminate and the client must cease use and delete/purge the data (narrow exceptions only
  for legally-compelled retention or audit).

**Month-to-month availability:** YES — premium subscriptions are monthly-billable and cancellable
at any time. So the *mechanics* of a one-month buy work; it's the *retention right* that fails.

**Consequence for our plan:** A one-month Sharadar buy is fine **only if** we finish the backtest
and delete the raw + derived data within the paid window (or within 30 days of cancel). It cannot
seed a permanent local warehouse. If the plan requires keeping the data, **Sharadar is out** — go
to the fallback.

---

## 4. PROFESSIONAL vs NON-PROFESSIONAL for OUR use

Sharadar's terms, **Section 4 (Appropriate License)**, verbatim:

> "If your use of the Services or the Services Data is **in relation to professional activities of
> any sort**, you agree to subscribe to the appropriate professional or institutional license."

Our use is research feeding a real-money-bound trading strategy (currently paper, but the whole
project's stated purpose is a live trading system). That is "professional activity," so we do
**not** cleanly qualify as Non-Professional. The honest read: **buy the Professional tier.**
The Professional-vs-Non-Professional price gap is **login-gated** (see the toggle in Section 2) —
typically several-fold higher for financial-data vendors, but I won't quote a number I can't verify.

---

## 5. BULK DOWNLOAD + SIZE

**Mechanism (confirmed, official Nasdaq "Large Table Download" docs):** append
**`qopts.export=true`** to the API call for a table. This queues an async job that returns a link to
a **zipped CSV of the entire table**; poll until status is `Fresh`, then download (link valid ~30
min). Subscriber-only, login required. Sharadar also publishes a `bulk_fetch.py` helper. So the full
pull is: SF1, SEP, TICKERS, ACTIONS (+ optional SP500/EVENTS/SF2/SF3), each `qopts.export=true`,
land the zips in the local warehouse.

**Size:** exact GB total is login-gated (Nasdaq doesn't publish per-table byte sizes publicly). Order
of magnitude: the whole Core US Equities pull is comfortably in the **low-single-digit to ~10s of GB
compressed** range (SEP daily prices 1998→now across the full survivorship-free universe is the
largest table; SF1 is wide but far fewer rows). **This fits comfortably in well under a day** — the
bottleneck is the async export job queue, not bandwidth or disk. (Confirm true size from the table
pages once logged in.)

---

## 6. ONE FALLBACK — EODHD All-In-One

If Sharadar's license (keep-after-cancel = no) or Professional price is a dealbreaker:

- **Package:** EODHD **All-In-One** — **$99.99/month** (or $999.90/yr ≈ $83.33/mo). **Published price.**
- **Month-to-month:** YES — "no long-term commitments, just a minimum of one month."
- **Bulk download:** YES — bulk EOD + fundamentals APIs, 120k+ global stocks/ETFs/funds.
- **Retention:** allowed under its commercial terms (no 30-day-delete-on-cancel clause like Sharadar's) —
  **verify the exact retention wording before relying on it**, but it is the standard "you keep what
  you pulled" model, which is the whole reason it's the fallback.
- **THE ONE HONEST CAVEAT:** EODHD's **point-in-time / as-reported (restatement-safe)** handling is
  **unconfirmed**. If its fundamentals are most-recent-restated rather than as-reported, that injects
  **look-ahead bias** into CAN SLIM's earnings-growth (C/A) signals — the exact failure mode rule #1
  forbids. Before committing, confirm EODHD serves as-reported historical fundamentals with filing
  dates; if it only serves restated values, it's unusable for an honest CAN SLIM backtest and we're
  back to eating Sharadar's delete-on-cancel term (finish-and-purge model) or sourcing point-in-time
  fundamentals elsewhere.

---

## RECOMMENDATION

1. **If we can live with finish-and-purge:** buy **Sharadar SFA, Professional, one month**, bulk-pull
   SF1(ARQ/ART) + SEP + TICKERS + ACTIONS, run the backtest, **delete raw + derived within the paid
   window**. Legal, clean, restatement-safe. Cost = one month Professional (see price via Section 2).
2. **If we need to keep the data:** Sharadar is **out** (license forbids it). Use **EODHD All-In-One
   $99.99/mo** — but first confirm it serves **as-reported point-in-time** fundamentals, or the
   backtest inherits look-ahead bias.

Andrew's call: is this a run-once-and-purge backtest (Sharadar wins on data quality) or do we need a
retained warehouse (EODHD, pending the point-in-time check)?

---

## Sources

- Sharadar Core US Equities Bundle (SFA): https://data.nasdaq.com/databases/SFA
- SFA pricing (login-gated): https://data.nasdaq.com/databases/SFA/pricing
- Sharadar Equity Prices (SEP): https://data.nasdaq.com/databases/SEP
- Sharadar Fundamentals (SF1): https://data.nasdaq.com/databases/SF1
- Sharadar publisher page: https://data.nasdaq.com/publishers/SHARADAR
- Sharadar License Terms of Use (delete-on-termination, internal-use, redistribution, professional):
  https://www.quantrocket.com/terms/sharadar/
- QuantRocket Sharadar data pricing (login-gated placeholders; Pro vs Non-Pro definitions):
  https://www.quantrocket.com/pricing/data/sharadar/
- QuantRocket Sharadar overview (tables, history dates, survivorship-free): https://www.quantrocket.com/sharadar/
- QuantRocket Sharadar docs: https://www.quantrocket.com/docs/data/fundamental/sharadar/
- Nasdaq Data Link general Data License Terms: https://data.nasdaq.com/terms
- Nasdaq Data Link cancel-anytime help: https://help.data.nasdaq.com/article/473-can-i-cancel-my-premium-data-subscriptions-at-any-time-how-do-i-cancel-my-subscription
- Nasdaq Data Link Large Table Download (qopts.export=true bulk CSV): https://docs.data.nasdaq.com/docs/large-table-download
- Sharadar bulk_fetch.py helper: http://www.sharadar.com/meta/bulk_fetch.py
- Datarade (confirms "Sharadar has not published pricing"): https://datarade.ai/data-providers/sharadar/profile
- EODHD pricing (All-In-One $99.99/mo, 1-month min): https://eodhd.com/pricing
- EODHD commercial pricing: https://eodhd.com/commercial-pricing
