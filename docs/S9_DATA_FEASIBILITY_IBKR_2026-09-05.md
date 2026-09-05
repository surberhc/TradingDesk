# S9 Data Feasibility — Can IBKR Replace ThetaData for Expired Single-Name LEAP History?

**Date:** 2026-09-05
**Probe:** `top15/probe_expired_options.py` (READ-ONLY, port 4001 live-data Gateway, clientId 67)
**Raw results:** `C:\TradingDesk-Local\top15\` (`probe_expired_options_results.json` plus per-run archives `run1_*`–`run5_*`)
**Question:** can IBKR's API serve usable historical price data for EXPIRED single-name US equity option contracts — long-dated LEAP calls — back to September 2021, as required by `docs/S9_SPEC.md` §11/§12.1?

---

## 1. Verdict

> **NO.** IBKR cannot supply this data. Two independent walls each block the project on their own: **expired single-name option contracts stop resolving within about a week of expiry** (error 200, contract definition gone), and **this login has no option market-data entitlement, with the historical EOD-chart service returning nothing for options even on live, unexpired, liquid contracts.**

The second wall could be bought. The first cannot be bought away by any subscription, because you cannot request history for a contract IBKR's own contract database will not return.

---

## 2. What actually works, so the negative result is trustworthy

The plumbing is fine. Confirming this first matters, because otherwise every failure below could just be a broken connection.

| Check | Result |
|---|---|
| Connect to port 4001 read-only, clientId 67 | OK — server version 178 |
| AAPL **stock** daily bars, `whatToShow=TRADES` | **5 bars, 2026-08-31 → 2026-09-04** |
| `reqSecDefOptParams` for AAPL | OK — full listed expiry and strike set returned |
| `reqContractDetails` on a **live** AAPL LEAP | OK — resolved, conId 844250748 |
| `reqContractDetails` on a **live** near-term AAPL call | OK — resolved, conId 908713494 |

So: connectivity, the historical-data service, and the contract database all respond correctly. Equity history works. Option *contract resolution* works. Only option *history* and *expired* option resolution fail.

No pacing violation occurred at any point. 35 historical requests total across six runs (5 + 12 + 16 + 0 + 0 + 2), each separated by 11 seconds, against IBKR's ~60-per-10-minutes budget. The two resolution-only runs cost zero historical requests by design.

---

## 3. Test matrix, with verbatim IBKR errors

### Step 0 — Control: a LIVE, UNEXPIRED long-dated AAPL call

Contract discovered from IBKR's own listed expiries and strikes (not guessed): **AAPL 2028-12-15 $240 call**, conId 844250748, 832 DTE, chosen deep-ITM against an AAPL close of 319.97.

| `whatToShow` | Bars | Verbatim IBKR error |
|---|---|---|
| MIDPOINT | 0 | `162 :: Historical Market Data Service error message:No data of type EODChart is available for the exchange 'BEST' and the security type 'Option' and '1 y' and '1 day'` |
| BID | 0 | same 162, verbatim as above |
| ASK | 0 | same 162, verbatim as above |
| TRADES | 0 | same 162, verbatim as above |

**The control failed.** Per the probe's own design it stopped and diagnosed rather than proceeding to expired contracts, because an expired-contract result could not have been interpreted against a broken control.

### Step 0b — Why did the control fail? (liquidity vs routing vs entitlement)

The Step 0 error names an *exchange*, not a subscription, so blaming entitlement immediately would have been a guess. Three competing explanations were tested.

**(i) Was it contract liquidity?** A Dec-2028 deep-ITM LEAP might simply never have traded. Tested a liquid near-term at-the-money contract instead — **AAPL 2026-09-25 $320 call**, conId 908713494:

| `whatToShow` | Bars | Verbatim error |
|---|---|---|
| TRADES | 0 | `162 :: ...No data of type EODChart is available for the exchange 'BEST' and the security type 'Option' and '1 m' and '1 day'` |
| MIDPOINT | 0 | same 162 |

Not liquidity.

**(ii) Was it SMART→BEST routing?** IBKR maps SMART to 'BEST' for options, and the error names 'BEST'. Re-requested the same LEAP against real options exchanges drawn from the contract's own `validExchanges`. Each venue gave a *different* message, all verbatim:

| Exchange | Bars | Verbatim IBKR error |
|---|---|---|
| CBOE | 0 | `162 :: Historical Market Data Service error message:No data of type EODChart is available for the exchange 'CBOE' and the security type 'Option' and '1 m' and '1 day'` |
| AMEX | 0 | `162 :: Historical Market Data Service error message:No market data permissions for AMEX OPT` |
| PHLX | 0 | `162 :: Historical Market Data Service error message:No data of type EODChart is available for the exchange 'PHLX' and the security type 'Option' and '1 m' and '1 day'` |
| ISE | 0 | `162 :: Historical Market Data Service error message:No historical market data for AAPL/OPT@ISE Last 1d` |
| CBOE, near-term contract | 0 | `162 :: ...No data of type EODChart is available for the exchange 'CBOE'...` |

Not routing. Naming a real exchange explicitly changes the wording but never the outcome. Note AMEX answers with an explicit **permissions** message.

**(iii) Was it entitlement?** Asked directly with a market-data snapshot, which is the clean discriminator — a missing subscription announces itself with 354 / 10091 / 10167 rather than with a message about an exchange:

| Market data type | Result | Verbatim errors |
|---|---|---|
| 1 (live) | bid/ask/last all NaN | `354 :: Requested market data is not subscribed. Check API status by selecting the Account menu then under Management choose Market Data Subscription Manager and/or availability of delayed data.Delayed market data is available.AAPL SEP 25 '26 320 Call (AAPL  260925C00320000) /TOP/ALL` · `10091 :: Part of requested market data requires additional subscription for API. See link in 'Market Data Connections' dialog for more details.AAPL NASDAQ.NMS/TOP/ALL` |
| 2 (frozen) | bid/ask/last all NaN | `354` and `10091`, as above |
| 3 (delayed) | **last 8.05, close 13.26, bid −1, ask −1** | `10167 :: Requested market data is not subscribed. Displaying delayed market data.` · `10091 :: ...requires additional subscription for API...AAPL NASDAQ.NMS/TOP/BID_ASK` |

**Entitlement confirmed.** The port-4001 login (`databot0001`) has **no live US options (OPRA) subscription**. Delayed option data is available and returns a last and a close, but **no quotes** — bid and ask come back as `−1`, which is IBKR's documented absent-quote sentinel, not a price.

### Step 0c — Does delayed mode unlock historical option bars?

Worth asking, because the desk's existing nightly IBKR option EOD feed (`datacollector/ibkr_forward.py`) runs on exactly this trick: `reqMarketDataType(3)` followed by `reqMktData` snapshots. If historical honoured the delayed mode too, the entitlement gap would not be fatal.

| Market data type | Contract | Bars | Verbatim error |
|---|---|---|---|
| 3 (delayed) | near-term 320C | 0 | `162 :: ...No data of type EODChart is available for the exchange 'BEST'...` |
| 3 (delayed) | LEAP 240C | 0 | same 162 |
| 4 (delayed-frozen) | near-term 320C | 0 | same 162 |
| 4 (delayed-frozen) | LEAP 240C | 0 | same 162 |

**No.** `reqMarketDataType` has no effect on `reqHistoricalData` for options. The forward-snapshot trick does not extend to history.

### Step 1 — Do expired LEAP contract definitions survive?

`reqContractDetails` needs no market-data subscription — it reads the contract database, not the quote feed — so this half of the question is answerable despite the entitlement wall. Wildcard strike, `includeExpired=True`, three issuers × five January (LEAP-cycle) expiries:

| Expiry | AAPL | MSFT | NVDA |
|---|---|---|---|
| 2026-01-16 (~8 months expired) | 200 | 200 | 200 |
| 2025-01-17 (~20 months) | 200 | 200 | 200 |
| 2024-01-19 (~20 months) | 200 | 200 | 200 |
| 2023-01-20 (~32 months) | 200 | 200 | 200 |
| 2022-01-21 (~44 months) | 200 | 200 | 200 |

All 15: `200 :: No security definition has been found for the request`. Zero contracts returned.

### Step 1b — Is that real, or is `includeExpired` just broken here?

A wildcard query is the shape most likely to be rejected for its own reasons, so Step 1 alone was suggestive but not conclusive. Re-asked with **fully-specified** contracts (exact expiry, strike, right), real strikes that certainly existed, both `exchange="SMART"` and blank, including a contract that expired **the previous trading day**:

| Expiry | Age at probe | Resolved? | Result |
|---|---|---|---|
| **2026-09-04** | **1 day** | **YES** | conId 904299451 — `includeExpired=True` demonstrably works on this login |
| 2026-08-21 | ~2 weeks | no | `200 :: No security definition has been found for the request` (4 attempts: strikes 300/280 × SMART/blank) |
| 2026-06-19 | ~2.5 months | no | 200 × 4 attempts |
| 2026-01-16 | ~8 months | no | 200 × 4 attempts |
| 2024-01-19 | ~20 months | no | 200 × 4 attempts |
| 2023-01-20 | ~32 months | no | 200 × 4 attempts |
| 2022-01-21 | ~44 months | no | 200 × 4 attempts |

This is the load-bearing control. `includeExpired` **is** functioning — yesterday's expiry resolves with a real conId. Every older expiry is genuinely gone.

### Step 1c — Where exactly is the boundary?

Walked the weekly expiries between the two known points, using strikes near current spot so "the strike was never listed" cannot be mistaken for "the definition is gone":

| Expiry | Days expired | Resolved? |
|---|---|---|
| 2026-09-04 | 1 | **YES** (conId 904299451) |
| 2026-08-28 | 8 | no — 200 |
| 2026-08-21 | 15 | no — 200 |
| 2026-08-14 | 22 | no — 200 |
| 2026-08-07 | 29 | no — 200 |
| 2026-07-31 | 36 | no — 200 |
| 2026-07-17 | 50 | no — 200 |
| 2026-07-03 | 64 | no — 200 |

### Step 5 — Greeks and implied volatility

Run standalone (`--step5-only`), because the main flow never reaches Step 5. Tested on a **live, resolvable** contract (AAPL 2026-09-25 $320C) so the answer is about the data, not about expiry:

| Request | Bars | Verbatim error |
|---|---|---|
| `whatToShow=OPTION_IMPLIED_VOLATILITY` | 0 | `162 :: ...No data of type EODChart is available for the exchange 'BEST' and the security type 'Option' and '1 m' and '1 day'` |
| `whatToShow=BID_ASK` | 0 | same 162 |
| Live snapshot with genericTick 106 (greeks) | none | `321 :: Error validating request.-'bM' : cause - Snapshot market data subscription is not applicable to generic ticks` |

**API-surface fact, independent of this account:** `reqHistoricalData` is the only historical bar source in the TWS API. For an option its `whatToShow` accepts TRADES, MIDPOINT, BID, ASK, BID_ASK and OPTION_IMPLIED_VOLATILITY — **all of which are OHLC bars**. There is no historical-greeks endpoint anywhere in the API. Delta, gamma, theta, vega and rho arrive only on the live streaming `tickOptionComputation` path (`reqMktData` genericTick 106 / `modelGreeks`), which cannot be requested for a past date and cannot be requested at all for a contract that no longer resolves.

---

## 4. The three failure modes, kept separate

These lead to different decisions, so they must not be conflated.

**(a) NOT SUBSCRIBED — entitlement.** Confirmed, verbatim: `354 Requested market data is not subscribed`, `10091 Part of requested market data requires additional subscription for API`, `10167 Requested market data is not subscribed. Displaying delayed market data`, and `162 No market data permissions for AMEX OPT`. This login has no OPRA option entitlement. **This one is purchasable.**

**(b) NOT RETAINED — IBKR discards expired-contract history.** **NOT MEASURED, and not measurable from here.** It cannot be tested, because failure mode (c) removes the contract before any history request can be made, and failure mode (a) blocks history even on live contracts that *do* resolve. Do not let anyone report this document as having measured historical retention. It did not. What was measured is *definition* retention, which is a different thing.

**(c) NOT RESOLVABLE — the contract definition itself is gone.** Confirmed, verbatim: `200 No security definition has been found for the request`, on 15 wildcard queries and 24 fully-specified queries, with `includeExpired=True` proven working by a contract that expired one day earlier. **This one is not purchasable.**

---

## 5. Answers to the specific questions asked

**Retention boundary (contract definitions):** between **1 and 8 days after expiry**. AAPL 2026-09-04 resolves; AAPL 2026-08-28 does not. Everything older — every expiry tested back to January 2022, across AAPL, MSFT and NVDA — returns error 200. The S9 study needs contracts expiring from roughly September 2022 through September 2026; **none of them exist in IBKR's contract database today.**

**Is BID/ASK quote history available?** **No.** BID, ASK and BID_ASK each returned zero bars with error 162, on a live, liquid, unexpired, fully-resolvable contract. Separately, the delayed live snapshot returned bid = −1 and ask = −1 (absent-quote sentinel). The S9 spec cannot run on TRADES alone — §12.1 requires `bid`, `ask`, `mid`, `spread_abs`, `spread_pct_mid`, `bid_size`, `ask_size`, and §14's execution models price off the spread — and TRADES returned zero bars here too, so the question is moot on this lane regardless.

**Greeks and implied volatility:** **bars only, and here not even bars.** No historical-greeks endpoint exists in the TWS API at all; that is an API-surface fact, not an account limitation. `OPTION_IMPLIED_VOLATILITY` exists as a historical *bar* series but returned zero bars on this connection.

**Consequence for a delta-based study.** S9 §15 selects contracts **by delta** (target grid 0.75–0.95, candidate band 0.70–0.98) and §16.2/§18.1 roll and bucket by delta. If greeks are not supplied, every delta would have to be **computed** from bar mid + underlying price + a rate curve via Black-Scholes. My honest read: **that is not acceptable as the study's primary selection rule**, for three compounding reasons:

1. **Dividend assumptions.** These are American calls on dividend-paying mega-caps over 365–730 day horizons. Delta is materially sensitive to the assumed dividend stream, and the correct input is the *expected* dividend path as of each historical date — not today's known-in-hindsight actuals, which would leak future information into a selection rule.
2. **American early-exercise premium.** A deep-ITM American call on a dividend payer carries early-exercise value that Black-Scholes does not price. The error is largest precisely in the 0.75–0.95 delta region the study selects from, so the bias is concentrated exactly where it does the most damage.
3. **Bar close is not a simultaneous snapshot.** A daily bar close for the option and a daily close for the underlying are not struck at the same instant, and the option's last print may be stale by minutes or hours on a thin LEAP. Implying a delta from two non-simultaneous prices injects noise into the very quantity that decides which contract gets bought.

Each of those alone would put a computed delta off by enough to change which strike the selection rule picks. Together, a "0.80 delta" contract chosen this way is not reliably the 0.80 delta contract, and §27's anti-overfitting discipline is meaningless if the selection variable itself is mismeasured. A vendor-supplied delta, or at minimum a vendor-supplied IV surface with dividend and borrow inputs, is a genuine requirement rather than a nicety.

---

## 6. Where the decision goes now

The answer is NO, so the decision passes to a **paid options-history vendor**. What is missing and must be supplied:

- Expired single-name US equity option contracts, September 2021 through September 2026, for the Top-15 mega-cap issuer manifest.
- **NBBO bid and ask** (with sizes) — not trades-only. §13's spread and quote-quality filters and §14's execution models are unbuildable without quotes.
- Per-contract **delta and implied volatility**, ideally with the dividend and rate assumptions used, for the reasons in §5 above.
- Open interest and volume (§12.1, and the §15 tie-break hierarchy).
- Daily held-contract series for §12.2, not just decision-date snapshots.

**No vendor pricing is included here, deliberately.** Vendor selection and cost are a separate step and Andrew's call.

One narrow, honest alternative worth naming without recommending it: IBKR *can* accumulate this data **going forward** from today via the existing `datacollector/ibkr_forward.py` snapshot mechanism, since delayed option snapshots do return a last and a close. That builds a forward archive; it does nothing for September 2021 – September 2026, and it does not supply quotes. It is not a substitute for the historical vendor.

---

## 7. What I did NOT test, and what remains unknown

Stated plainly, without papering over gaps.

1. **Historical *price* retention for expired options (failure mode b) was never measured.** It could not be reached: definitions vanish within ~8 days, and history fails even on live contracts. Whether IBKR would serve expired-option bars to a properly entitled account is **unknown from this probe**.
2. **Whether buying an OPRA subscription would make option history work at all.** Never tested — no subscription was purchased. The 162 messages are consistent with an entitlement gap, but "EODChart is not available for security type Option" is *also* consistent with IBKR simply not offering an option EOD-chart service on this route. **These two were not separated.** If the vendor route is ever reconsidered in favour of IBKR, this is the first thing to settle.
3. **Only port 4001 was tested.** Port 4003 (live-trade, FA master, 354 real client accounts) was deliberately never touched, per the task's hard constraint. Whether the 4003 login carries different option entitlements is unknown and was not investigated. Port 4002 (paper) is retired and was not used.
4. **Only three issuers** (AAPL, MSFT, NVDA) and only January LEAP-cycle expiries plus a short run of 2026 weeklies. The manifest is 15 issuers. Given that all three behaved identically and the failure is structural, broader coverage was judged unnecessary — but it was not run.
5. **The live-greeks snapshot in Step 5 is inconclusive on its own terms.** It returned error 321 because the request combined `snapshot=True` with a generic tick list, which IBKR rejects — **that is a flaw in my request shape, not an IBKR finding.** The greeks conclusion above rests on the API-surface fact (no historical-greeks endpoint exists) and on the separate Step 0b snapshots, which returned `modelGreeks = None` on an unsubscribed contract, not on this 321.
6. **No intraday bar sizes were tried** — only `1 day`. S9 §11 Layer 1 wants 10:00/12:00/14:00/15:45 ET samples. Since daily bars return nothing, finer granularity was not attempted, but it was not proven absent either.
7. **`reqHistoricalTicks` and `reqHeadTimeStamp` were not tried.** Both are plausible alternative routes to the same data. Neither would help with expired contracts that do not resolve, but neither was tested on live contracts.
8. **Corporate actions were not examined.** NVDA's 10:1 split (2024) and other manifest-relevant actions mean historical strikes are pre-split values; §9.4's alias handling was not exercised because no historical contract ever resolved.

---

## 8. Safety note

This probe was read-only throughout. `top15/probe_expired_options.py` calls only `reqContractDetails`, `reqSecDefOptParams`, `reqHistoricalData`, `reqMktData`/`cancelMktData` and `reqMarketDataType`. It contains no order path of any kind — no `placeOrder`, no `whatIfOrder`, no arming helper. It connects via `connections.ibkr_live_data.connect()`, whose `connect()` has no `readonly` parameter and is structurally read-only. Port 4003 was never contacted. The only Gateway action taken was the idempotent `datacollector/run_live_data_gateway_open.cmd`, which launched port 4001 (down at session start, up in ~70 seconds, no 2FA prompt) and never kills or restarts anything.
