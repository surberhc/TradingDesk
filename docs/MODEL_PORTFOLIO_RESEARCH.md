# MODEL_PORTFOLIO_RESEARCH.md — running multiple strategy sleeves in IBKR client accounts

**Date:** 2026-07-21
**Audience:** the desk (Andrew + future sessions)
**Status:** reference / compliance trail behind the Model Portfolios architecture decision
**Conductor:** #42 (Model Portfolios foundation), #43 (gateway verification)
**Sibling docs:** `docs/MODEL_PORTFOLIO_SPEC.md`, `docs/CRM_HANDOFF_model_allocation.md`,
`docs/MODEL_PORTFOLIO_GATEWAY_TEST_PLAN.md`, code at `paperbot/model_portfolio.py`

> **Disclaimer.** §7 and parts of §5/§6 summarize **IBKR platform mechanics** — what the
> broker's software and account structures permit — gathered from IBKR/library documentation.
> This is **not legal or investment advice.** Whether any of it is *appropriate* for your
> situation — RIA registration vs. exemption, the advisory agreement, POA validity, per-client
> suitability of 0DTE credit spreads, NPA eligibility — is a **regulatory determination that
> requires the user's own compliance/legal counsel.** Every such item is flagged **[COUNSEL]**
> below and is explicitly out of scope for this desk to advise on.

**Reading key:** **[CONFIRMED]** = verified against official IBKR or library documentation /
source (URL inline). **[UNVERIFIED]** = anecdotal, community-sourced, snippet-only, or awaiting
a live gateway test — do not rely on it clause-for-clause. **[COUNSEL]** = regulatory/legal
determination that is the user's own counsel's call, not the desk's.

All findings gathered 2026-07-21.

---

## 1. The core question & the recommended architecture

**Goal.** Allocate **one** client account across multiple strategy sleeves — the headline case
is **75% S0** (Adaptive All-Weather multi-asset ETFs) / **25% S8** (SPX 0DTE credit spreads) —
under our FA master **DF8922141**.

**RECOMMENDED (pending gateway Test 0):** a **hybrid**.

- **S0 → a Model Portfolio sleeve** (an IBKR `modelCode`, e.g. `S0_ALLWEATHER`).
- **S8 → its own dedicated sub-account** under the same FA master.

The driver is the multi-leg limitation in **§3**: IBKR Model Portfolios support **stocks and
single-leg positions only**, and S8 trades multi-leg (two-leg BAG/combo) credit spreads, which
likely cannot be a model holding. Under the hybrid, **"75/25"** means **75% of the client's
capital is managed under the S0 model** and **25% is funded into the S8 sub-account**.

**Trade-off to accept.** The client then holds **two accounts** (one model-managed slice + one
S8 sub-account) instead of one blended account, and the CRM contract gains a **sleeve-type
field** (model sleeve vs. sub-account sleeve) so the transport layer knows how each sleeve is
booked and read.

**Reversion condition.** If gateway **Test 0** shows models *do* accept multi-leg combos, the
cleaner **pure two-model design** (both S0 and S8 as `modelCode` sleeves in one account)
returns and the sub-account for S8 is unnecessary. Test 0 is the single decision gate.

> Note on numbering: the multi-leg showstopper is **Test 0** in the current
> `MODEL_PORTFOLIO_GATEWAY_TEST_PLAN.md` (earlier drafts / the CRM handoff called it "test #1").
> The order-account-field question is **Test 4**. This doc uses the test-plan's live numbering.

---

## 2. IBKR Model Portfolios & the API — [CONFIRMED]

Model Portfolios are a **Financial Advisor (FA) feature**: you invest a client account across
one or more **models** by **amount or percentage**, and IBKR tracks each model's slice of the
account independently.

- **Invest a client account in multiple models**, by amount/percentage — this is the native
  IBKR capability the whole design rests on.
  https://www.ibkrguides.com/traderworkstation/invest-in-multiple-models.htm
  https://www.ibkrguides.com/advisorportal/modelportfolio.htm
- **`modelCode` on the order routes it to a model**; the account is the FA master. Per-model
  reads use `reqAccountUpdatesMulti` / `reqPositionsMulti`, both of which return a `modelCode`.
  https://interactivebrokers.github.io/tws-api/model_portfolios.html
- **Model creation, rebalancing, and cash/position transfer between models are UI-ONLY** — they
  are **not** in the TWS API. The API can *route to* and *read* models, but cannot *create* or
  *rebalance* them. (Same TWS API page above.)

**FA order allocation — [CONFIRMED].** There is **no default allocation via the API**: every FA
order must specify how it is allocated. The available mechanisms:

- **`faGroup` + `faMethod`** — allocate across a predefined group of accounts by a method:
  `EqualQuantity`, `NetLiq`, `AvailableEquity`, or `PctChange`.
- **`faProfile`** — a saved allocation profile (percentages/ratios per account).
- **`faPercentage`** — percentage allocation.
- **`modelCode`** — route the order to a model (our path).
  https://interactivebrokers.github.io/tws-api/financial_advisor_methods_and_orders.html

**Order account field for a model order — [CONFIRMED intent, live test PENDING].** Research
indicates a model order's `Order.account` should be the **FA MASTER** (`DF…`) **plus**
`modelCode`, **not** the client sub-account.

> **FLAG (order-affecting).** Our current code disagrees. `paperbot/model_portfolio.py`
> `apply_model_fields` (line ~226) stamps the **client sub-account** into `order.account`
> alongside the `modelCode`. If the master+modelCode convention is confirmed by **gateway
> Test 4**, this is an **order-affecting fix** — apply it behind a `paperbot/version.py` VERSION
> bump + CHANGELOG line, and flag to Andrew before editing (per CLAUDE.md). Until Test 4 settles
> it, treat the account-field convention as **UNVERIFIED**.

---

## 3. THE multi-leg limitation — the key risk, double-sourced — [CONFIRMED as documented; resolution PENDING]

IBKR's advisor documentation states a Model Portfolio **"can invest in stocks and single leg
positions."**
https://www.ibkrguides.com/advisorportal/modelportfolio.htm

S8's **SPX 0DTE credit spreads are multi-leg** (two option legs, a BAG/combo order). By the
documented limitation, a multi-leg combo **likely cannot be a model holding**, which would mean
**S8 cannot be a `modelCode` sleeve at all**.

This was surfaced **independently** by both the architecture research and the IBKR-policy review
as **the** item to resolve before committing the architecture. It is the single biggest risk to
the pure two-model design.

- **Resolution:** gateway **Test 0** (the showstopper) — a TWS-UI check that a multi-leg holding
  can even enter the `S8_ZERODTE` model, plus a scripted `whatIf` on a modelCode-tagged combo —
  and, ideally, direct confirmation from IBKR.
- **Fallback if it fails:** the hybrid in §1 — **S8 as its own sub-account** under the FA master.

Until Test 0 returns PASS, **assume multi-leg is not allowed in a model** and build to the
hybrid.

---

## 4. ib_async library reality — [CONFIRMED at source]

We use **ib_async 2.1.0**, the maintained fork of ib_insync (ib_insync itself is **archived**).
Findings that shaped `model_portfolio.py`:

- **`Order.modelCode` works** → model-aware order routing is clean (set `order.modelCode`).
- **`reqAccountUpdatesMulti` works**, and `AccountValue.modelCode` exists (since ib_insync
  v0.9.0) → **per-model account VALUE reads** (NetLiq per sleeve) are genuinely supported.
  Changelog / API: https://github.com/ib-api-reloaded/ib_async
- **`reqPositionsMulti` is effectively broken at the high level — [CONFIRMED].** The
  `Wrapper.positionMulti` / `positionMultiEnd` callbacks are **no-op `pass` stubs** in **both**
  ib_async and ib_insync, so per-model position responses are silently dropped — the high-level
  facade exposes no accessor/event/future for them. The wire request itself
  (`ib.client.reqPositionsMulti`) is sent correctly; only the response handling is stubbed.
  Wrapper source (the `pass` stubs):
  https://github.com/ib-api-reloaded/ib_async/blob/main/ib_async/wrapper.py
  Client request path:
  https://github.com/ib-api-reloaded/ib_async/blob/main/ib_async/ib.py
- **Upstream fix is open and unmerged (~2 years).**
  https://github.com/ib-api-reloaded/ib_async/pull/25

**Our workarounds (both in `paperbot/model_portfolio.py`):**

- **`read_model_positions`** temporarily **shadows** the two no-op wrapper callbacks with its own
  collector, fires the request, pumps the event loop to the End marker or a timeout, cancels, and
  restores the originals in a `finally`. It is flagged in-code as depending on an ib_async
  internal. **Harden before live** by subclassing `Wrapper` with real `positionMulti` handling
  (or vendoring the PR #25 change) rather than instance-attribute shadowing. Confirmation that it
  harvests real per-model rows is **gateway Test 5**; the fungibility case is **Test 6**.
- **`read_model_account_values`** needed a **timeout-bounded async fix (committed)**: the bare
  *sync* `reqAccountUpdatesMulti` blocks until IBKR sends the `accountUpdateMultiEnd` marker, but
  the gateway **never sends that End** for a broad request (`account=""`) or for a `modelCode`
  that exists in the UI but has **no account allocated into it** — so the sync form **hangs
  forever**. The fix drives the async form under `asyncio.wait_for`; on timeout it keeps whatever
  streamed in (partial/empty read, never a hang). Verified against the paper gateway 2026-07-20.

---

## 5. Alternative architectures & why the hybrid

Four candidate ways to run multiple sleeves, with the trade-offs that led to the hybrid:

**(a) Model Portfolios / `modelCode` — [CONFIRMED capability, single-leg only].** Multiple
sleeves in **one** account; **IBKR is the ledger** (it tracks each model's slice). Clean, native
per-model reads. **Limit:** stocks / single-leg only (§3) — kills S8-as-model unless Test 0
overturns it. IBKR FA docs as in §2.

**(b) Sub-account per strategy under the FA master — [CONFIRMED].** Cleanest **isolation**;
supports **options combos** natively; native per-account statements. **QuantConnect officially
recommends a subaccount-per-algo** when running multiple algorithms against IBKR.
https://www.quantconnect.com/docs/v2/cloud-platform/live-trading/brokerages/interactive-brokers
**Cost:** per-account administration + per-account market-data subscriptions.

**(c) Shadow / `orderRef` ledger in one blended account — [CONFIRMED failure modes → avoid].**
Cheapest at the broker (one account, sleeves tracked only in our own ledger by tag). But the
documented failure modes are real: **long/short-the-same-symbol netting** (the broker nets a
position the ledger thinks is two separate sleeve holdings) and **reconciliation drift** between
our ledger and the broker's truth. These are exactly the problems sleeve/overlay vendors call out.
Smartleaf on sleeve accounting: https://www.smartleaf.com/  (sleeves / overlay reconciliation
guidance) — **avoid** for anything holding offsetting positions across sleeves.

**(d) SMA / TAMP overlay — overkill at this scale.** A separately-managed-account or turnkey
asset-management overlay solves sleeve accounting institutionally but is disproportionate for a
friends-and-family / small-advisor footprint.

**Conclusion:** the **hybrid** — **(a)** for S0 (ETFs, single-leg, let IBKR be the ledger) and
**(b)** for S8 (multi-leg combos need a real sub-account). It sidesteps the §3 limitation and the
(c) netting/reconciliation trap while keeping S0's accounting native to IBKR.

---

## 6. Rebalancing & reporting tooling

**Rebalancing.**

- IBKR model **rebalance is UI-only** (no API — §2). Practitioners **DIY**: read positions + NLV,
  compute drift, place plain orders — which is exactly what our `model_share_deltas` /
  `model_share_targets` do (`paperbot/model_portfolio.py` §(e)).
- **No mature OSS rebalancer exists.** `iblncr` and `ib_strategy_project` are tiny single-author
  reference projects; `pogoetic/rebalance` is drift-math only with **no IBKR integration**. There
  is nothing to adopt — **keep the rebalancer in-house**. [UNVERIFIED — survey-level; based on the
  small-project landscape as of the research date, not an exhaustive audit.]

**Reporting / attribution.**

- **`reqPnL` / `reqPnLSingle` accept `account` + `modelCode`** → **per-model P&L on an FA master**
  is available over the socket API. https://interactivebrokers.github.io/tws-api/pnl.html
  (Which P&L tags actually come back per model is **gateway Test 7**.)
- **PortfolioAnalyst** is GUI / PDF / CSV only — **no API**.
- **Flex Web Service** is the mature **automated EOD pull** (token-auth; parse with the `ibflex`
  library). Recommended pairing: **socket API for intraday** state + **Flex for EOD
  reconciliation**.
  Flex Web Service: https://www.ibkrguides.com/reportingreference/reportguide/flex3.htm
  ibflex library: https://github.com/csingley/ibflex
  - **[UNVERIFIED caveat]:** community reports that **enabling the Model field in a Flex query
    disables that query's native realized-P&L section**. Single community source, not IBKR docs —
    **test before relying on it**.

**API surface choice.** Stay on **ib_async / TWS socket API** for orders, allocation, and models.
IBKR's **Web API (REST)** reaches FA parity but adds **OAuth + session-keepalive** churn — only
worth adopting if REST/browser access is later required.

---

## 7. IBKR policy — is running this allowed? (platform matter; legal layer is the user's own)

> **[COUNSEL] framing.** Everything here is **IBKR platform mechanics** — what the broker's
> account types and software permit. Whether *you* may operate this way is a **regulatory
> question for your own compliance/legal counsel.** The desk does **not** advise on it.

**BOTTOM LINE — [CONFIRMED as platform mechanics]:** running multiple strategy sleeves across FA
client accounts is **permitted on IBKR as a platform matter, with no special program to enroll
in.** FA master + client sub-accounts, multi-model % allocation, `modelCode` routing,
discretionary order placement, credit spreads, and advisor-fee billing are **all native
features**.

**Advisor structures — [CONFIRMED as offered]:**

- **Non-Professional Advisor (NPA)** master — the friends-and-family structure, for advisors
  **exempt from registration**, generally **~15 or fewer accounts**; **OR**
- a **registered RIA** master.
- **No account minimums; commissions from $0.** A **$1,000 master-account NAV rule** applies:
  **below** $1,000 master NAV, commissions revert to being charged to the client accounts.
  https://www.ibkrguides.com/advisorportal/ (advisor portal / app guide)

**Discretionary authority — [CONFIRMED as platform mechanic]:** captured via IBKR's
**Discretionary Trading Authorization / Limited Power of Attorney**, e-signed at onboarding.
IBKR's POA **explicitly is NOT a substitute for an advisory agreement**, and **IBKR does not
judge suitability** — that stays with the advisor.
https://www.ibkrguides.com/advisorportal/ (discretionary authorization / POA pages)

**Options — [CONFIRMED as platform mechanic]:** **Level 3** options permission is required for
**credit spreads**, and it is approved **per client account** (driven by that client's stated
objectives and financials).
https://www.ibkrguides.com/traderworkstation/optionstradingpermissions.htm
- **No special IBKR gate found specifically on 0DTE / SPX** — i.e., this is the **absence of a
  restriction**, not an affirmative "0DTE is allowed" statement. [UNVERIFIED as an affirmative
  permission — treat as "no restriction located," not "explicitly permitted."]
- Defined-risk spread margin ≈ **max loss** (IBKR may add a **102% house requirement**).

**Fees — [CONFIRMED as offered]:** advisor fees can be **% of NAV, flat, or performance fees**
(with high-water-mark); **IBKR calculates, holds, and bills** them.
https://www.ibkrguides.com/advisorportal/ (fee configuration pages)

**[COUNSEL] — explicitly the user's own compliance/legal counsel (the desk does NOT advise):**
- RIA **registration vs. exemption** (and NPA eligibility / the ~15-account line).
- The **advisory agreement** itself.
- **POA validity** under applicable state law.
- **Per-client suitability** of 0DTE credit spreads.

**Sourcing caveat — [UNVERIFIED reliability]:** several `interactivebrokers.com/campus` pages
returned **HTTP 403** to automated fetch, so parts of §7 rest on **search-snippet extraction**
rather than a full-page read. **Confirm clause-for-clause in a browser** (or with IBKR directly)
before relying on any specific number or wording — especially the ~15-account NPA line, the
$1,000 NAV rule, and the 102% house margin figure.

---

## 8. Open items / next steps

1. **Gateway Test 0 (multi-leg in a model) — decides S8's home.** PASS → pure two-model design
   returns; FAIL → hybrid (S8 as sub-account) is committed. Run first; everything downstream
   hinges on it. Confirm with IBKR directly as well.
2. **Remaining gateway tests** per `docs/MODEL_PORTFOLIO_GATEWAY_TEST_PLAN.md` (post-allocation
   surfacing, sizing parity, allocation-required-or-not, fungibility, per-model P&L, readonly
   enforcement, typo behavior).
3. **Order-account-field fix (Test 4).** If master+modelCode is confirmed, fix
   `apply_model_fields` behind a version bump + CHANGELOG (order-affecting — flag to Andrew).
4. **CRM handoff** gains a **sleeve-type field** (model sleeve vs. sub-account sleeve) if the
   hybrid is confirmed — see `docs/CRM_HANDOFF_model_allocation.md`.
5. **Harden the `positionMulti` workaround** — subclass `Wrapper` (or vendor PR #25) instead of
   instance-attribute shadowing before anything goes live.
6. **Consider Flex Web Service** for EOD reconciliation/reporting (test the Model-field vs.
   realized-P&L caveat in §6 first).
7. **[COUNSEL]** NPA-vs-RIA structure, advisory agreement, POA, and 0DTE suitability go to the
   user's own counsel — not a desk deliverable.

---

### Where the findings are thin (flagged for honesty)

- **§3 multi-leg limitation** is documented by IBKR but the *operative* answer for our exact
  combo (SPXW 0DTE BAG under a model) is **not yet empirically confirmed** — Test 0 is
  load-bearing and unrun.
- **§7 policy numbers** (the ~15-account NPA line, $1,000 NAV rule, 102% house margin, and the
  0DTE "no restriction found" point) partly rest on **403-blocked pages / search snippets** —
  browser-confirm before relying on any figure, and route every regulatory judgment to counsel.
- **§4 order-account convention** is **research-suggested, code-contradicted, and untested** —
  do not treat master+modelCode as settled until Test 4.
- **§6 OSS-rebalancer survey** is landscape-level, not an exhaustive audit; the **Flex Model-field
  realized-P&L caveat** is a single community source.
