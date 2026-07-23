# INCIDENT — 2026-07-23: `arming.py arm` killed the S8 LIVE-pilot Gateway (port 4003)

**Severity:** High — an unrelated PAPER-lane operation destroyed a **live, funded-account**
pilot's Gateway, and reported success while doing it.
**Status:** Recovered (2m51s outage). Root cause identified and confirmed in code. **Fix NOT
implemented** — proposed below. One orphan process still running (see Follow-ups).
**All times CDT (UTC-05:00), the box's local time.**

---

## 1. Summary

The session's goal was PAPER-lane work: bring up the paper Gateway (port 4002) with API
writes enabled so an FA allocation-group membership change could be made. Andrew hand-edited
`C:\IBC\config.ini` to set `ReadOnlyApi=no`, then ran `paperbot\arming.py arm`. That path
restarts the paper Gateway through the elevated `GatewayArmRestart` scheduled task
(`paperbot\gateway_arm_restart_elevated.py`), which calls
`connections.gateway_watchdog._kill_gateway_processes()` with **no arguments**.

That kill also destroyed the **S8 live-pilot Gateway on port 4003** — the instance
`CLAUDE.md` and `docs/HANDOFF_2026-07-23_FA_gateway_test.md` both mark **NEVER touch**.

The cause is the kill routine's *secondary* discriminator: it keeps (i.e. kills) any candidate
process whose command line contains the substring `C:\IBC`. The paper install dir `C:\IBC` is a
**string prefix** of both sibling installs, `C:\IBC-Live-Data` and `C:\IBC-Live-Trade`. The
live-trade processes therefore matched and were killed.

**The `PILOT_MODE` zero-transmit wall was never breached.** No order was placed, modified, or
transmitted at any point. The damage was availability, not execution.

---

## 2. Impact

| | |
|---|---|
| **Outage window** | 09:55:17 → 09:58:08 = **2 minutes 51 seconds** with no listener on port 4003 |
| **What was lost** | Live 0DTE band-collector data for the window; S8's exit-monitoring was **blind** on a REAL funded account's pilot for the same window |
| **What was NOT affected** | Zero-transmit guarantee held. No order placed/modified/transmitted. S8 alerting behaved correctly (one email, duplicate suppressed) |
| **Residual** | One orphan `java.exe` (pid 29236) from a losing self-heal race is still running, bound to no port |
| **Unresolved** | The PAPER gateway (pid 33576) that the arm *did* successfully bring up was gone by ~10:04 with nothing on 4002 — cause NOT established (see §7) |

---

## 3. Verified timeline (2026-07-23, CDT)

| Time | Event | Evidence |
|---|---|---|
| 09:55:17 | 4003 Gateway (`java` pid 30784) killed | `s8_gateway_alert` event id `s8_collector:1784818517.2826092` in `C:\TradingDesk-Local\s8_pilot\logs\s8_service_20260723.log`. Epoch 1784818517 → `2026-07-23 09:55:17 -05:00` (converted at write time) |
| 09:55:25 | An S8 process self-healed by relaunching its own gateway: `cmd /c C:\IBC-Live-Trade\StartGatewayLiveTrade.bat` (pid 30728) | `Win32_Process` CreationDate `7/23/2026 9:55:25 AM` — **still running at write time** |
| 09:55:27 | That relaunch produced `java` pid 29236 | `Win32_Process` CreationDate `7/23/2026 9:55:27 AM`, parent pid 30728, `-DjtsConfigDir="C:\IBC-Live-Trade\GatewaySettings" … "C:\IBC-Live-Trade\config.ini"`. It **never bound port 4003** (lost the race / login state) and is **still running bound to nothing** |
| 09:55:27 | The PAPER Gateway came up as `java` pid 33576 on port 4002 | observed in session; pid no longer exists at write time |
| 09:55:55 | `C:\TradingDesk-Local\state\paperbot\gateway_arm_restart_state.json` written: `{"ok": true, "detail": "restarted and serving data"}` | file read at write time: `ts` 1784818555.94 → `2026-07-23 09:55:55 -05:00`. **This success record is about the PAPER gateway only — it is silent about the live one the same call had just destroyed** |
| 09:55:xx – 09:58:08 | Port 4003 had **no listener**. S8's collector/monitor/service (pids 17164 `s8_service.py`, 17328 `s8_collector.py`) sat in `SYN_SENT` retrying | `s8_service_20260723.log`: repeated `API connection failed: ConnectionRefusedError(22, 'The remote computer refused the network connection', None, 1225, None)` and `s8_service.run: reconnect failed ([WinError 1225] …); retrying...`. Both pids still alive at write time |
| — | S8 emitted **one** alert email and correctly suppressed duplicates | `s8_gateway_alert: another process already alerted {0,14,26,38}s ago (event='s8_collector:1784818517.2826092') — suppressing a duplicate email for the same outage.` |
| 09:58:06 | Andrew manually ran scheduled task `LiveTradeGatewayOpen_0815CT` → `cmd` pid 7004 | `Win32_Process` CreationDate `7/23/2026 9:58:06 AM` |
| 09:58:08 | `java` pid 3496 came up and **bound 4003** | `Win32_Process` CreationDate `7/23/2026 9:58:08 AM`; `Get-NetTCPConnection -State Listen` at write time shows **4003 → OwningProcess 3496** |
| post-recovery | Both S8 clients returned to `ESTABLISHED`; collector resumed; monitor re-subscribed its legs | `s8_collector_20260723.log`: `s8_collector: harvested 48 band rows (spot=7407.83, vix=19.44)` |
| 10:00 | Runner logged a normal decision — the zero-transmit wall intact | `[Calls-80-$4@10:00] WOULD HAVE TRANSMITTED: S8 entry Calls-80-$4 short=7425/long=7440 qty=1 stop=7.30 + B2 child` |
| ~10:04 | PAPER gateway pid 33576 gone; nothing listening on 4002 | **cause NOT established — see §7** |

---

## 4. Root cause — verified in code

**File:** `C:\TradingDesk\connections\connections\gateway_watchdog.py`

**Entry point (~line 279):**

```python
def _kill_gateway_processes(port: int = ibkr_paper.PAPER_PORT,
                            dir_substring: str = r"C:\IBC") -> list[int]:
```

**The filter (`_KILL_PS_TEMPLATE`, ~lines 240–271)** selects `java.exe` matching `IbcGateway`
or `cmd.exe` matching `StartGateway`, then keeps a process if **either** discriminator hits
(~lines 257–260):

```powershell
    (
        ($_.ProcessId -in $gwPid) -or
        ($cl -match [regex]::Escape($dirSubstring))
    ) -and
```

- `$gwPid` (~line 248) is the PID owning `-LocalPort {port}`. **This discriminator is correct**
  — on its own it would have spared 4003.
- `$dirSubstring` defaults to `C:\IBC`. **This is the bug.** `C:\IBC` is a **string prefix** of
  `C:\IBC-Live-Data` and `C:\IBC-Live-Trade`. A live-trade process whose command line contains
  `C:\IBC-Live-Trade\StartGatewayLiveTrade.bat` therefore matches `C:\IBC` and is killed.

**Mechanically confirmed at write time** against the real running processes:

```
  PID Name     MatchPrefix('C:\IBC')  MatchLiveTradeDir
30728 cmd.exe               True                 True
29236 java.exe              True                 True
 7004 cmd.exe               True                 True
 3496 java.exe              True                 True
```

Every live-trade process — including pid 3496, the one currently serving the live pilot on
4003 — matches the paper default `dir_substring`. A zero-argument
`_kill_gateway_processes()` call today would kill the live pilot again.

**The docstring's claim is false.** Lines 281–283 state the function spares "any OTHER Gateway
instance running on a different port/install dir." That is **not true for any install dir that
shares the `C:\IBC` prefix** — which is both of the other two lanes. The module comment at
lines 221–228 makes the same claim about the "SECONDARY discriminator." Both need correcting
along with the code.

### 4.1 Why the secondary discriminator exists at all

Per the module comment (lines 226–228) it is deliberate: it catches a Gateway process that has
been launched but has **not yet bound its port**, during a wedge/launch race window. Removing
it outright would reintroduce that gap; it needs to be made *exact*, not deleted.

---

## 5. Why it went undetected until now

1. **Port 4003 is new.** The live-trade lane was introduced 2026-07-15 —
   `connections\connections\clientids.py` line 23: *"NOTE (2026-07-15): port 4003
   (LIVE_TRADE_PORT) now also exists…"*. The kill routine's instance-scoping predates a real
   third instance existing to collide with.
2. **The 5-minute watchdog is off.** `GatewayWatchdog` scheduled task state is **Disabled**
   (verified at write time). Its `main()` (line 391) passes `kill_fn=_kill_gateway_processes`
   with defaults, so had it been enabled it would have been killing the live pilot every time
   it restarted the paper gateway. `GatewayArmRestart` is `Ready`;
   `LiveTradeGatewayOpen_0815CT` is `Running`.
3. **The exposed call sites use the defaults.** `paperbot\gateway_arm_restart_elevated.py`
   line 77 (`killed = _kill_gateway_processes()`) and `gateway_watchdog.main()` line 391 both
   pass **no arguments**. The arm/disarm path is therefore the live exposure.
   *(Correction to an earlier read: not literally every call site uses defaults —
   `connections\connections\gateway_watchdog_live.py` line 299 correctly passes
   `port=4001, dir_substring=r"C:\IBC-Live-Data"`. That call is safe: `C:\IBC-Live-Data` is not
   a prefix of the other installs. The paper-lane defaults are the sole problem.)*
4. **The tests could not catch it.** `connections\test_gateway_watchdog.py` lines 268–306
   assert only on the **generated PowerShell text** — that the script *contains*
   `$dirSubstring = 'C:\IBC'` — with `subprocess.run` monkeypatched out. Nothing exercises the
   matching **semantics** against realistic command lines, so a prefix collision is invisible
   to the suite.
5. **The failure was silent.** `gateway_arm_restart_elevated.py` verifies only that the paper
   gateway came back (`ibkr_paper.ensure_gateway()`, lines 92–94) and writes
   `{"ok": true, "detail": "restarted and serving data"}`. It never checks what else it killed.
   Detection came from S8's own alert email, not from the tool that caused the damage.

---

## 6. The fix — **PROPOSED, not implemented**

### 6.1 Primary: make the directory discriminator exact, not a prefix

Match the install root exactly — e.g. append a trailing separator (`C:\IBC\`) or compare the
resolved install root rather than doing a substring test.

**Verified by execution — and this reveals the trailing-separator form is NOT sufficient on its
own.** Tested against the real running command lines:

```
  PID Name      Match 'C:\IBC'   Match 'C:\IBC\'   contains 'C:\IBC\IBC.jar'
30728 cmd.exe            True             False                       False
29236 java.exe           True              True                        True
 7004 cmd.exe            True             False                       False
 3496 java.exe           True              True                        True
```

- For the **`cmd.exe` launchers**, `C:\IBC\` works: `C:\IBC\` is **not** a substring of
  `C:\IBC-Live-Trade\StartGatewayLiveTrade.bat` or `C:\IBC-Live-Data\StartGatewayLiveData.bat`,
  but **is** a substring of the paper `C:\IBC\StartGateway.bat`.
- For the **`java.exe` gateway JVMs it does NOT work** — and those are the processes that
  matter. All three lanes launch IBC from the **shared** classpath entry `C:\IBC\IBC.jar`, so
  the live-trade JVM's command line contains the literal `C:\IBC\` regardless of which install
  it belongs to. A naive trailing-separator fix would still kill the live pilot.

**Therefore the discriminator must key on the per-instance config/settings arguments, not on any
bare install-root substring.** The live-trade JVM carries
`-DjtsConfigDir="C:\IBC-Live-Trade\GatewaySettings"` and the final argument
`"C:\IBC-Live-Trade\config.ini"`; the paper launcher sets `CONFIG=%SYSTEMDRIVE%\IBC\config.ini`
(verified in `C:\IBC\StartGateway.bat`), so the paper JVM's config argument is
`C:\IBC\config.ini`. `C:\IBC\config.ini` is **not** a substring of
`C:\IBC-Live-Trade\config.ini`, so matching on the **config.ini path** discriminates correctly
for the JVMs, while the `C:\IBC\` launcher-path form discriminates correctly for the `cmd.exe`
shells. Whatever form is chosen, **it must be validated against real captured command lines,
not against the launcher path alone.**

### 6.2 Defence in depth — recommendations

1. **Callers pass port + install dir explicitly.** Remove reliance on the defaults in
   `gateway_arm_restart_elevated.py` (line 77) and `gateway_watchdog.main()` (line 391) so the
   paper lane's scope is stated at every call site, as `gateway_watchdog_live.py` already does.
2. **Explicit never-kill list.** Hard-exclude any process whose command line references
   `C:\IBC-Live-Trade` or `C:\IBC-Live-Data` from the paper kill filter — a positive
   allow/deny check that does not depend on getting a substring right.
3. **Post-condition assertion in `gateway_arm_restart_elevated.py`.** After the kill+relaunch,
   assert port **4003 is still LISTENING** (and 4001, when that lane is live) and **fail loudly**
   if not. The arm reported `{"ok": true}` while the live pilot lay dead; the success record was
   actively misleading and must not be possible again.
4. **Regression test.** Assert the discriminator does **not** match the sibling install paths —
   exercising the real match semantics against captured `Win32_Process` command lines
   (including the shared `C:\IBC\IBC.jar` classpath entry, which is exactly what defeats the
   obvious fix), not just the presence of a string in the generated script.
5. **Re-examine the disabled `GatewayWatchdog` before re-enabling it** (conductor #80 covers
   gateway unattended-resilience hardening). Re-enabling it *before* this fix lands would put
   the live pilot at risk on every automatic paper restart, every 5 minutes.

---

## 7. Unresolved

**The paper Gateway (pid 33576) disappeared by ~10:04 and nothing was listening on 4002.
The cause is NOT established.** Two candidates, neither confirmed:

- The **shared paper login** being taken over by another user — the IBKR paper login is shared
  by multiple people (see memory `paper-account-after-3pm-ct` and
  `docs/HANDOFF_2026-07-23_FA_gateway_test.md` §"BIG operational discovery"); a login takeover
  kicks whoever holds the session.
- An **IBC session-conflict exit** (`ExistingSessionDetectedAction=manual`).

Do not assert a cause without evidence. This is logged as open.

---

## 8. Follow-ups

1. **Implement the §6.1 fix** — conductor item opened (see below). Until it lands, see §9.
2. **Clean up the orphan.** `java` pid 29236 (parent `cmd` pid 30728) is a second
   `C:\IBC-Live-Trade` instance from the 09:55:25 self-heal race. It is **still running and
   bound to no port**. This is the same shape as the **2026-07-05 gateway-pileup incident**
   referenced in `paperbot\gateway_arm_restart_elevated.py`'s docstring. It needs cleanup — not
   done here, deliberately, because killing Gateway processes by hand is how this incident
   started. Do it with the live pilot's schedule in mind, and confirm pid 3496 keeps 4003.
3. **Correct the false docstring/comment** in `gateway_watchdog.py` (lines 221–228 and 281–283)
   as part of the fix.
4. **Root-cause the 4002 disappearance** (§7).

---

## 9. Operational lesson — read this before running `arming.py`

**`paperbot\arming.py arm` is NOT safe to run while any other Gateway instance is up, until the
fix in §6.1 lands.** It will kill the live-trade Gateway on 4003 (and would kill a live-data
Gateway on 4001) and then report success.

**This applies to `disarm` too.** Both `arm()` (line 270) and `disarm()` (line 288) in
`paperbot\arming.py` go through the same `restart_gateway()` → `GatewayArmRestart` →
`_kill_gateway_processes()` path. Verified.

If you must arm/disarm before the fix: bring the live lanes down deliberately first, or expect
to re-run `LiveTradeGatewayOpen_0815CT` immediately afterwards and verify 4003 is listening.

### 9.1 Triage note — a correction worth recording

During this incident I initially reported that `java` pids **29236 and 33576 were "both paper"**
gateways. **That was wrong** — 29236 is a **live-trade** instance
(`-DjtsConfigDir="C:\IBC-Live-Trade\GatewaySettings"`, config `C:\IBC-Live-Trade\config.ini`).
The error came from grouping the two by their near-identical start times.

**The correct discriminator when triaging is the process's CommandLine install directory** —
`Get-CimInstance Win32_Process` and read `-DjtsConfigDir` / the trailing `config.ini` argument —
**never the start time**, and never the bare `C:\IBC` substring (see §6.1: every lane's JVM
carries `C:\IBC\IBC.jar` on its classpath, so that substring identifies nothing).

---

## Appendix — verification performed while writing this report

Re-verified directly, not from memory:

- `gateway_watchdog.py` lines 240–320 (template + function signature + docstring) — read.
- `gateway_arm_restart_elevated.py` — read in full; line 77 confirms the zero-argument call.
- `arming.py` — `arm()` line 270 and `disarm()` line 288 both call `restart_gateway()`.
- `gateway_watchdog_live.py` line 299 — correctly scoped call with explicit args.
- `test_gateway_watchdog.py` lines 268–306 — text-only assertions, no match semantics.
- `clientids.py` line 23 — the 2026-07-15 port-4003 note.
- Scheduled task states: `GatewayArmRestart` = Ready, `GatewayWatchdog` = **Disabled**,
  `LiveTradeGatewayOpen_0815CT` = Running.
- Live process inventory + `Get-NetTCPConnection` listeners: 4003 → pid 3496; **no listener on
  4002**; orphan pid 29236 alive and unbound; pids 17164/17328 (`s8_service.py`,
  `s8_collector.py`) alive; pids 33576 and 30784 gone.
- Regex match matrix for `C:\IBC` vs `C:\IBC\` vs `C:\IBC\IBC.jar` against the real command
  lines (§4, §6.1) — executed.
- `gateway_arm_restart_state.json` contents and its `ts` → 09:55:55 conversion.
- All quoted log lines located in
  `C:\TradingDesk-Local\s8_pilot\logs\s8_service_20260723.log` and
  `s8_collector_20260723.log`.

Not independently re-verifiable at write time (processes/state already gone; taken from
in-session observation): the 09:55:17 kill of pid 30784 beyond its log-recorded alert epoch, and
the paper gateway pid 33576's 09:55:27 appearance on 4002.
