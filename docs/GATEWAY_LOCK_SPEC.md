# Gateway Lock — design spec (single-process gateway mutex)

**Status:** DESIGN ONLY. No code written. This is the prerequisite to ever putting the
account monitor on a schedule (closes scheduler-plan **F2** and gates **D5**).
**Scope:** one machine, one paper Gateway (`127.0.0.1:4002`). PAPER only, as always.

---

## TL;DR — the recommended design in three sentences
A single **PID lockfile in local `STATE_DIR`** (`C:\TradingDesk-Local\state\paperbot\`,
NEVER on Drive) makes only one of our processes operate the Gateway at a time. We **reuse
the proven atomic acquire + stale-reclaim pattern** already shipping in
`datacollector\spxw_1m_supervisor.py` (`O_CREAT|O_EXCL` create, `tasklist` liveness check,
reclaim a dead holder's lock) — we do not reinvent it. Callers wrap their connect/work in a
**`with gateway_lock(...)` context manager** that always releases (normal exit *and*
exception), with a **heartbeat lease** so a legitimately long laddered rebalance can hold
the Gateway for many minutes without being mistaken for hung.

---

## OPEN DECISIONS FOR THE OWNER (rule on these — everything else is mechanical)

1. **Monitor: skip vs. wait when the Gateway is busy?**
   *Recommendation:* **wait briefly (≈10 s), then SKIP this cycle** — log it, surface "held
   by rebalance pid X since HH:MM" in the next verdict/dashboard, try again next cycle. The
   monitor is automated and read-only; it should never queue behind a multi-minute human
   rebalance. **Your call:** accept 10 s, or a different brief wait.

2. **Max-lease / heartbeat horizon — how long may a holder hold before we presume it hung?**
   *Recommendation:* **heartbeat-based, not a fixed short timeout.** The holder refreshes
   the lock's timestamp every ~30 s; a blocked process only reclaims after **no heartbeat for
   ≈5 min** (`STALE_HEARTBEAT_SECS`). A laddered rebalance legitimately holds for many
   minutes, so a short fixed lease would wrongly reclaim it mid-flight. **Your call:** accept
   the 5-min no-heartbeat horizon, or set it higher for safety.

3. **Rebalance: how long does the human-driven path wait before refusing?**
   *Recommendation:* **wait a short bounded time (≈30 s), then REFUSE with a named holder**
   ("Gateway held by paperbot_monitor pid X clientId 40 since HH:MM — that read-only cycle
   should finish in seconds; re-run"). A human rebalance has precedence, but a read-only
   monitor cycle is short, so a brief wait usually avoids a needless refusal without ever
   transmitting blind into a contended Gateway. **Your call:** accept 30 s, or refuse
   immediately and name the holder.

These three numbers (`MONITOR_WAIT_SECS≈10`, `STALE_HEARTBEAT_SECS≈300`,
`REBALANCE_WAIT_SECS≈30`) are the only judgment calls. The rest follows from the proven
supervisor pattern.

---

## 1. The problem this solves (why clientIds are not enough)
clientIds (`connections\clientids.py`) stop two of our sessions from grabbing the **same
IBKR API client slot** — an IBKR-side session collision. They do **nothing** to stop two of
**our** processes from operating the Gateway **concurrently on different clientIds**:

- `account_monitor_run.py` — clientId **40**, read-only — must NOT read account state in the
  middle of a rebalance (it would snapshot positions/cash that are changing under it, and
  compete for Gateway attention).
- `rebalance_run.py` (clientId **37**, read-only build) / `rebalance_execute.py` (clientId
  **38**, transmit-capable) — the path that moves the book.

Scheduler-plan **F2** states the rule directly: *"monitor and rebalance are mutually
exclusive on the Gateway"* — and that this is *"the central reason the monitor is not yet
scheduled."* This lock is the mechanism that enforces it.

---

## 2. Mechanism (RECOMMENDED) — a PID lockfile in STATE_DIR, supervisor pattern

**Reuse, cite, do not reinvent:** `datacollector\spxw_1m_supervisor.py::acquire_lock()` (and
`release_lock` / `_pid_alive`) already implements exactly the hard part we need — and it is
battle-tested in production:

- **Atomic acquire** — `os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)`. Only one of N
  racing starts can create the file; the rest get `FileExistsError`. (supervisor lines
  ~134–160.)
- **Liveness check** — on `FileExistsError`, read the holder PID and test it with
  `tasklist /FI "PID eq <pid>"` on Windows (`_pid_alive`, lines ~95–117). If we can't tell,
  assume alive (refuse) — safer than racing.
- **Stale reclaim** — holder PID not running ⇒ `unlink()` the lock and retry the atomic
  create (lines ~152–159).
- **Release-iff-owner** — only delete the lock if its recorded PID is still ours
  (`release_lock`, lines ~163–169) — never stomp a successor's lock.

**Where the file lives — LOCAL ONLY.** `gateway.lock` lives in
`config.STATE_DIR` = `C:\TradingDesk-Local\state\paperbot\` (same dir/discipline as
`ledger.py`, the monitor baselines, and the FA backups). **It must NEVER be on Google
Drive**: Drive's background sync renames/rewrites files non-atomically and replicates them
across machines, which would **break `O_CREAT|O_EXCL` atomicity and the liveness check** —
exactly the property the lock depends on. The whole point of `STATE_DIR` being off-Drive
(per `config.py` and the CLAUDE.md contract) applies here with extra force.

**Lock file contents (richer than the supervisor's bare PID).** The supervisor stores only a
PID; we store a small JSON record so a blocked process can name the holder in its log/UI:

```json
{
  "pid": 12345,
  "client_id": 38,
  "purpose": "rebalance_execute",
  "acquired_at": "2026-06-30T14:07:32",
  "heartbeat_at": "2026-06-30T14:11:02",
  "host": "DESKTOP-...",
  "armed": true
}
```

- `pid` + `host` — liveness (PID is only meaningful on this host; we assert single-machine).
- `client_id`, `purpose`, `armed` — so a refusal reads *"held by rebalance_execute pid 12345
  clientId 38 (armed) since 14:07."*
- `acquired_at` / `heartbeat_at` — staleness math (Decision 2).

Atomicity caveat: the atomic primitive is the **exclusive create of the path**, not the JSON
write. We create the file empty-exclusive first (wins the race), then write the JSON record
into the held fd — identical ordering to the supervisor, just a richer payload.

---

## 3. API (RECOMMENDED) — a context manager that wraps the single connect chokepoint

There is exactly **one place every component connects**: `connections\connections\ibkr_paper.py::
connect()` (and the one bespoke `IB().connect()` in `rebalance_execute.execute_armed`, which
must be wrapped at its call site since it bypasses `ibkr_paper.connect`). The lock is a context
manager the caller wraps its connect+work in:

```python
from connections.gateway_lock import gateway_lock, GatewayBusy

# MONITOR (automated, read-only) — fail-fast / skip this cycle
try:
    with gateway_lock(purpose="monitor", client_id=clientids.get("paperbot_monitor"),
                      wait_secs=MONITOR_WAIT_SECS):          # ≈10 s
        ib = ibkr_paper.connect("paperbot_monitor", readonly=True, launch=True, timeout=15)
        try:
            ...read-only cycle...
        finally:
            ib.disconnect()
except GatewayBusy as busy:
    print(f"SKIP this cycle — {busy}")     # names holder: pid/clientId/purpose/since
    return 0                               # not an error; try again next cycle
```

```python
# REBALANCE (human-driven) — wait a short bounded time, then REFUSE naming the holder
try:
    with gateway_lock(purpose="rebalance_execute", client_id=clientids.get("paperbot_rebalance_exec"),
                      wait_secs=REBALANCE_WAIT_SECS, armed=armed):   # ≈30 s
        ib = IB(); ib.connect(ibkr_paper.HOST, ibkr_paper.PAPER_PORT, clientId=..., readonly=False, ...)
        try:
            ...build / write FA config / place blocks...
        finally:
            ib.disconnect()
except GatewayBusy as busy:
    print(f"REFUSING to start — {busy}")   # "held by paperbot_monitor pid X since HH:MM"
    return 2
```

Contract:
- **Acquire on enter** using the supervisor pattern; **block up to `wait_secs`** polling on a
  short cadence; raise **`GatewayBusy`** (carrying the holder record) if still held.
- **Release on exit — always.** `__exit__` releases on normal return AND on exception
  (`try/finally` semantics) so a crashed/Ctrl-C'd rebalance never leaves a poisoned lock —
  and even if it dies without releasing, the next caller reclaims it as stale (Decision 2).
- **Heartbeat while held** — a daemon thread (or an explicit `lock.beat()` the rebalance
  loop calls between blocks) refreshes `heartbeat_at` every ~30 s, mirroring the supervisor's
  heartbeat write.
- The manager **only guards the mutex** — it never connects, never transmits, never reads
  config. Connection stays the caller's job; the gate's review→arm→transmit flow is
  untouched.

---

## 4. Acquire policy per client (RECOMMENDED — not just enumerated)

**Human-driven rebalance takes precedence over the automated monitor.** Justification: the
rebalance is the value-bearing, time-sensitive, human-supervised operation; the monitor is a
cheap, repeatable, read-only background cycle that loses nothing by deferring one tick.

| Caller | clientId | Policy | Why |
|---|---|---|---|
| **monitor** (`account_monitor_run`) | 40 | **wait ≈10 s, then SKIP** the cycle (log + surface in next verdict) | Automated + read-only; never queue behind a multi-minute human rebalance. A skipped read-only cycle is a non-event — the next cycle catches up. |
| **rebalance run/execute** (`rebalance_run` 37 / `rebalance_execute` 38) | 37 / 38 | **wait ≈30 s, then REFUSE** with a named holder | Has precedence, but a monitor cycle is *seconds*; a short wait usually clears it. Never transmit blind into a contended Gateway — refuse loudly and name who holds it. |
| read-only probes (`fa_probe` 33, `accounts` 31, `recon` 32) | various | acquire with a short wait; SKIP-or-print on busy (same family as the monitor) | Read-only diagnostics; same "don't read mid-rebalance" rule, low stakes. |

The asymmetry **is** the precedence: monitor yields (skip), rebalance insists (wait longer,
then refuse rather than silently proceed). Neither ever forcibly evicts a live holder — only
a **dead/stale** holder is reclaimed (§5).

---

## 5. Stale / hung-holder reclaim (RECOMMENDED)

Two distinct failure shapes, two handled mechanisms:

**(a) Dead holder PID — reclaim, exactly as the supervisor does.** If the recorded PID is not
alive (`tasklist` liveness), the holder crashed/was killed without releasing. `unlink` the
lock and retry the atomic create (supervisor lines ~152–159). This covers Ctrl-C, a hard
kill, a process crash, and a power-cycle that left a stale file.

**(b) Hung-but-ALIVE holder — heartbeat lease, NOT a short fixed timeout.** A laddered
rebalance can *legitimately* hold the Gateway for many minutes (watch windows per rung, GTC
rest, one block at a time). A short fixed lease would wrongly reclaim a healthy long run
mid-flight — unacceptable for a transmit operation. So:

- The holder **refreshes `heartbeat_at` every ~30 s** while it holds (daemon thread or an
  explicit `beat()` between blocks).
- A blocked caller treats the lock as stale **only if the PID is alive but `heartbeat_at` is
  older than `STALE_HEARTBEAT_SECS` (≈5 min)** — i.e. the process is wedged, not working.
- **Conservative default:** if liveness can't be determined (`tasklist` failed), assume alive
  and do **not** reclaim — refuse instead (the supervisor's "safer to refuse than to risk
  two" stance). For a transmit path, never reclaim on ambiguity.

This is the heartbeat-or-generous-lease the brief calls for: long legitimate holds survive;
a genuinely wedged holder is reclaimed after a generous, heartbeat-gated horizon, not a
hair-trigger timer.

---

## 6. clientIds vs. the lock (they are different things, both needed)

**clientIds prevent an IBKR-side session collision; the lock prevents an our-side process
collision.** A clientId is the identity the IBKR API uses to keep each connection's order/
data streams separate — two sessions on the *same* id collide at the API boundary, so the
registry hands every consumer a unique number. But two sessions on *different* ids are
perfectly legal to IBKR and can operate the Gateway at the same instant — which is exactly
the monitor-reads-mid-rebalance hazard. The lock is an **inter-process mutex on the Gateway
as a shared resource**, orthogonal to identity: it guarantees that at any moment **at most
one of our processes is operating the Gateway at all**, regardless of which clientId each
holds. You need both: the registry so connections don't collide *when more than one is
allowed*, and the lock so that *only one is allowed at a time*.

---

## 7. Failure modes & visibility (ties to scheduler-plan F2 / D5)

| Situation | Monitor (automated) | Rebalance (human) |
|---|---|---|
| Gateway free | acquire, run, release | acquire, run, release |
| Held by the other path | wait ≈10 s → **SKIP** + log + show "held by … since HH:MM" in next verdict/dashboard | wait ≈30 s → **REFUSE** + print "held by `<purpose>` pid X clientId Y since HH:MM" |
| Holder dead (stale PID) | reclaim (supervisor pattern), proceed | reclaim, proceed |
| Holder alive but no heartbeat ≥5 min | reclaim, proceed | reclaim, proceed |
| Liveness undeterminable | refuse/skip (assume alive) | refuse (assume alive) — never transmit on ambiguity |
| Holder crashes mid-hold | n/a | `__exit__`/stale-reclaim frees it for the next run |

**Visibility is a first-class requirement, not a log line.** Every busy outcome names the
holder (purpose + pid + clientId + since-HH:MM) from the lock's JSON record. The monitor's
SKIP must appear in its propose-only verdict output and the eventual dashboard so a skipped
cycle is *seen*, not silent. This is precisely the **F2** interlock the scheduler plan asks
for and the soft-interlock option in **D5(b)** ("the monitor checks for an active rebalance/
lock before connecting and skips if busy") — this lock is that interlock.

---

## 8. Test plan (all offline / unit-level — no Gateway, no network)

The supervisor's lock has no dedicated tests; we write proper ones for this shared mutex.

1. **Atomic acquire** — first `gateway_lock(...)` acquires; a second concurrent acquire on
   the same path raises `GatewayBusy`; releasing the first lets the second succeed.
2. **Lock record** — after acquire, the file parses to JSON with the right
   pid/client_id/purpose/acquired_at; a refusal message contains purpose+pid+clientId+time.
3. **Stale reclaim — dead PID** — write a lock with a PID known dead (monkeypatch
   `_pid_alive`→False); next acquire reclaims and succeeds; assert the file's PID is now ours.
4. **Heartbeat staleness** — alive PID + `heartbeat_at` older than `STALE_HEARTBEAT_SECS`
   ⇒ reclaimable; fresh heartbeat ⇒ NOT reclaimable (raises `GatewayBusy`). Use injected
   `now`/monkeypatched liveness — no real clock waits.
5. **Timeout/skip path** — held lock + `wait_secs` small ⇒ `GatewayBusy` after ~wait;
   monitor-style caller turns it into a clean SKIP (return 0), rebalance-style into REFUSE
   (return 2).
6. **Release on exception** — raising *inside* the `with` body still releases the lock
   (next acquire succeeds); assert via a follow-on acquire.
7. **Release-iff-owner** — a process must not release a lock whose recorded PID isn't its own
   (mirror `spxw_1m_supervisor.release_lock`); assert a foreign-PID lock is left intact.
8. **Simulated two-process contention** — spawn two short Python subprocesses both calling
   `gateway_lock`; assert exactly one wins and the other reports busy (the realistic race,
   still offline — no Gateway involved).

No live-Gateway test is required for the lock itself; the existing read-only connect paths
already exercise the Gateway, and the lock is provably correct from the unit level up.

---

## 9. Build slices (smallest blast radius first)

| Slice | What | Blast radius | Order-affecting? |
|---|---|---|---|
| **1** | `connections\gateway_lock.py` — the module (atomic acquire + stale/heartbeat reclaim + `gateway_lock` CM + `GatewayBusy`) **+ its tests** (§8). Lift the supervisor pattern; cite it. Add lock-timing constants to a config. | New file only; nothing imports it yet → **zero** runtime change | No |
| **2** | Wrap the **monitor's** connect (`account_monitor_run.main`) in `gateway_lock(purpose="monitor", …)` with the SKIP-on-busy policy; surface the skip in the verdict output. | One read-only consumer; read-only, propose-only — cannot transmit | No (read-only; but bump `version.py` for the audit trail of a connect-path change) |
| **3** | Wrap the **rebalance** connects: `rebalance_run.main` (clientId 37) and the bespoke `IB().connect` in `rebalance_execute.execute_armed` (clientId 38), with the WAIT-then-REFUSE policy + heartbeat between blocks. | Touches the transmit-capable path → highest care; gate logic unchanged, lock only wraps the connection | **YES — bump `version.py` + CHANGELOG** (it wraps an order-affecting path) |
| **4** *(optional)* | Wrap the read-only probes (`fa_probe`, `accounts`, `recon`) for completeness. | Read-only diagnostics | No |
| **5** *(enables the goal)* | With Slices 2–3 proven, **schedule the monitor** (resolves D5) — now safe because the lock guarantees it can never read mid-rebalance. | Scheduler config only | No |

Ship 1→2→3 in order; never wrap the transmit path (Slice 3) before the module and the
read-only monitor (Slices 1–2) are green. Slice 1 alone is a no-op safety net you can land
and sit on.

---

## Non-negotiables this spec honors
- **PAPER only**, port 4002 — the lock changes nothing about the account or the
  review→arm→transmit gate; it only serializes *who operates the Gateway*.
- **Lockfile is LOCAL** (`C:\TradingDesk-Local\state\paperbot\gateway.lock`), never Drive —
  Drive sync would break the atomicity the lock depends on.
- **Reuse over reinvention** — the acquire/reclaim machinery is the proven
  `spxw_1m_supervisor.py` pattern, cited, not rebuilt.
- **No new heavy dependencies** — stdlib `os`/`subprocess`/`json`/`threading` only.
