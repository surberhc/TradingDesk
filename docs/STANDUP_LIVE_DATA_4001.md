# Stand-up runbook — IBKR Live-Data Gateway (port 4001)

**Goal.** Bring up the read-only, live-side market-data Gateway on **port 4001** so the
warehouse-safe forward collector (`datacollector/forward_daily_live.py`, writes only to
`raw/options_ibkr`) can run its A/B validation against still-live ThetaData **before
ThetaData lapses 2026-07-25**.

**Lane (authoritative — see `connections/GATEWAYS.md`):**

| Item | Value |
|------|-------|
| Install dir | `C:\IBC-Live-Data` |
| Launcher | `C:\IBC-Live-Data\StartGatewayLiveData.bat` |
| Config | `C:\IBC-Live-Data\config.ini` |
| GatewaySettings | `C:\IBC-Live-Data\GatewaySettings` |
| Port | **4001** (`clientids.LIVE_DATA_PORT`) |
| clientId | **48** (`live_data_forward`) |
| Connection module | `connections/connections/ibkr_live_data.py` (structurally read-only) |
| Watchdog | `connections/connections/gateway_watchdog_live.py` (scoped port 4001, `C:\IBC-Live-Data`) |

> **Read-only, three ways:** `ibkr_live_data.connect()` has no `readonly` parameter
> (always read-only); the account has no execution permission; `ReadOnlyLogin=yes` /
> `ReadOnlyApi=yes` in `config.ini`. Nothing in this lane can transmit an order.

---

## 0. IMPORTANT — a working install already exists under the OLD name

Per `conductor/STATUS.md` (item **#24**, closed 2026-07-14; confirmed by the
2026-07-15 `live-data-smoke-test` entry):

- **IBKR account approval is DONE.** Andrew already completed a first interactive login.
- A **working** port-4001 Gateway ran against **live account U5721712**; **both** smoke-test
  halves passed (read-only connectivity + order-rejection backstop, IBKR code 321).
- That install physically lives at **`C:\IBC-Live`** (old name) with real credentials
  already entered and a populated `GatewaySettings`.
- The **`C:\IBC-Live-Data`** name is the target of the blessed **2026-07-15 full-symmetry
  gateway rename** — Andrew is the one who must do the machine-side rename.

**Entitlement caveat (must know before A/B):** account U5721712 is **DELAYED-ONLY**
(market-data type 3). An implicit live request is rejected (error 10089). This is fine for
an **EOD** snapshot A/B — `ibkr_forward_live.py` already calls `reqMarketDataType(3)` and
the collector was designed to run on delayed data — but it is **not** OPRA/real-time. If
the A/B intends to validate live intraday quotes, that would need a separate entitlement.

### Choose ONE stand-up path

- **Path A — RENAME the existing working install (RECOMMENDED).** Preserves the approved
  login, `GatewaySettings`, and already-entered credentials — no re-entry, no re-approval.
  1. Stop any running Gateway from `C:\IBC-Live` (close its window).
  2. The scaffold created at `C:\IBC-Live-Data` (this runbook's Path B files) is a
     *placeholder* — **delete or empty `C:\IBC-Live-Data` first** so the rename can land:
     `rmdir /s /q C:\IBC-Live-Data`
  3. Rename the dir: `ren C:\IBC-Live C:\IBC-Live-Data`
  4. Rename the launcher: `ren C:\IBC-Live-Data\StartGatewayLive.bat StartGatewayLiveData.bat`
  5. Edit the renamed `StartGatewayLiveData.bat`: set `CONFIG=C:\IBC-Live-Data\config.ini`
     and `TWS_SETTINGS_PATH=C:\IBC-Live-Data\GatewaySettings` (only the two path lines).
  6. Edit `C:\IBC-Live-Data\config.ini`: confirm `OverrideTwsApiPort=4001`,
     `ReadOnlyLogin=yes`, `ReadOnlyApi=yes`, `TradingMode=live`; credentials stay as
     Andrew already entered them.
  7. Proceed to **§2** (verify listening).

- **Path B — Build from the scaffold (fallback / from-scratch).** Use the placeholder
  `C:\IBC-Live-Data\StartGatewayLiveData.bat` + `config.ini` created 2026-07-21. Fill the
  `<<FILL-LIVE-DATA-USERNAME>>` / `<<FILL-LIVE-DATA-PASSWORD>>` placeholders by hand
  (§1), then continue to §2. Only use this if the working `C:\IBC-Live` install is gone.

---

## 1. What Andrew must confirm / provide

- [ ] **IBKR live-data login approved** — DONE per STATUS #24 (account U5721712). Re-confirm
      the login still authenticates if it has been idle.
- [ ] **Options data entitlement** — currently **DELAYED (type 3)**. Confirm this is
      acceptable for the EOD A/B (it is, for close snapshots). If real-time/OPRA is
      required, that is a separate subscription decision.
- [ ] **Credentials in `C:\IBC-Live-Data\config.ini`** — `IbLoginId` + `IbPassword`.
      Under Path A these already exist; under Path B, replace the `<<FILL-…>>` placeholders.
      Claude never handles these values.
- [x] **Distinct username check** — if the live-data and live-trade (4003) logins share an
      IBKR username, they cannot run simultaneously (`ExistingSessionDetectedAction=primary`
      would boot the other). Confirm they are distinct before running both. CONFIRMED 2026-07-24: the three lanes use distinct logins — 4001 databot0001, 4002 apsvpaper, 4003 apsv1816 — so 4001 and 4003 never boot each other (see connections/GATEWAYS.md).

## 2. Launch + verify port 4001 listening

1. Launch the Gateway (from an elevated shell — the process runs elevated):
   ```
   C:\IBC-Live-Data\StartGatewayLiveData.bat
   ```
   Approve any IBKR Mobile 2FA push by hand within 180s.
2. Confirm it is listening on 4001:
   ```
   netstat -ano | findstr :4001
   ```
3. Confirm the process identity:
   ```
   tasklist /FI "IMAGENAME eq javaw.exe"
   ```
   (Expect a Java process whose command line references `C:\IBC-Live-Data`.)

## 3. Smoke test (SPY, writes to `raw/options_ibkr`)

From `C:\TradingDesk\datacollector` with the venv python:
```
cd C:\TradingDesk\datacollector
"C:\TradingDesk-Local\venv\Scripts\python.exe" ibkr_forward_live.py --test --launch
```
- `--test` writes a small (~40-line) SPY slice into `raw/options_ibkr`; `--launch` starts the
  Gateway first if it is down. The script requests delayed data (`reqMarketDataType(3)`).
- Success = a non-empty SPY slice lands under `raw/options_ibkr` and no order is transmitted
  (this lane cannot transmit, by construction).

## 4. Representative-root A/B collection run

Collect the representative multi-asset root set (writes only to `raw/options_ibkr`):
```
cd C:\TradingDesk\datacollector
"C:\TradingDesk-Local\venv\Scripts\python.exe" forward_daily_live.py SPX SPXW RUT NDX SPY QQQ XLF XLE AAPL NVDA HYG TLT
```
- Roots are optional CLI args (default = full universe); this list is the representative
  cross-section. Writes the `forward_live` jobstatus key (never collides with the retired
  `forward` key). Logs to `warehouse\forward_live.log`.

Then run the A/B diff for today's date:
```
"C:\TradingDesk-Local\venv\Scripts\python.exe" forward_ab_check.py 20260721
```
- Diffs `raw/options_ibkr` (IBKR) against the ThetaData warehouse for that date and prints a
  per-symbol + overall verdict (`ok | warn/partial | fail | stale`).

## 5. Read the A/B verdict

- Per-symbol lines `verdict=OK/WARN/FAIL` plus an overall verdict. Investigate any `FAIL`
  (coverage/strike/price divergence beyond the module's thresholds). `stale` = one side has
  no data for the date.
- This is the honest cross-check that IBKR EOD chains match ThetaData **while both are still
  live** — the whole reason for the 2026-07-25 deadline.

## 6. Register the nightly `ForwardFillLive` task (after A/B is trusted)

Staged but **not yet registered** (see `docs/SCHEDULER_PLAN.md` rows #9–#10):
- **ForwardFillLive** — nightly EOD forward-fill via port 4001, mirroring the retired
  `ThetaForwardDaily`'s old ~5:30 PM slot. Wrapper: `C:\TradingDesk-Local\warehouse\`
  (`run_forward_live.bat`) → `datacollector/forward_daily_live.py`.
- **GatewayWatchdogLive** — companion 5-min elevated watchdog
  (`connections/connections/gateway_watchdog_live.py`). Before registering, align its
  `MAINTENANCE_WINDOW_ET` placeholder to the config's `AutoRestartTime` (01:05 AM).
- Register only once the A/B verdict is trusted and the Gateway has proven stable.

---

## Hard deadline

The A/B diff (**§4–§5**) must run **before ThetaData's subscription lapses 2026-07-25** —
after that there is no live second source to validate IBKR against.
