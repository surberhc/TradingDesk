# Session Handoff — 2026-07-20 — backup system complete; open issues enumerated

Written for a reader with **no memory** of the prior session. Everything below was verified
live on 2026-07-20 (git, Task Scheduler, the status JSONs, the conductor DB, and actual test
runs) — not recalled. Where a prior claim did not survive verification, the corrected fact is
stated and the stale version is called out so it does not get re-asserted.

---

## 1. CURRENT STATE (verified 2026-07-20)

- **HEAD = `bf5343d`** ("conductor: S8 first live session -- outage, failsafe fired, recovery,
  and first live captures"). *Correction: an earlier draft of this handoff said HEAD was
  `4cd6dd8`; that commit is real but is now HEAD~1.*
- **Working tree clean apart from untracked `investech/`** (see Known Issue 1).
- **`RepoBackupDaily`** — registered, `State=Ready`, `LogonType=Password`,
  daily 20:00 (`MSFT_TaskDailyTrigger`, start boundary 2026-07-16T20:00:00-05:00) **+ AtLogon**
  (`MSFT_TaskLogonTrigger`), `ExecutionTimeLimit=PT1H`, next run 2026-07-20 20:00.
  Action: `C:\TradingDesk\datacollector\run_repo_backup.cmd`.
- **`DataBackupDaily`** — registered, `State=Ready`, `LogonType=Password`,
  daily 21:00 (start boundary 2026-07-20T21:00:00-05:00) **+ AtLogon**,
  `ExecutionTimeLimit=PT12H`, next run 2026-07-20 21:00.
  Action: `C:\TradingDesk\datacollector\run_data_backup.cmd`.
  **It has never run yet** — `LastRunTime=11/30/1999`, `LastTaskResult=267011`
  ("task has not yet run"). Tonight at 21:00 is its first-ever scheduled execution.
- **`CLOUD_VERIFY_REQUIRED = True`** in `datacollector/repo_backup.py` (line 217) — the repo
  backup fails closed if Google's cloud copy cannot be md5-confirmed.
- **Latest verified repo bundle:** `tradingdesk-repo-20260720-085258.bundle`, HEAD
  `bf5343d7e6ad523337c369cae35a279afc03ee52`, 344 commits, `state=verified_new_bundle`,
  `cloud.state=verified` (local md5 == Drive md5). See
  `C:\TradingDesk-Local\backups\repo_backup_status.json`.
  *Correction: an earlier draft named `...-20260720-083842.bundle` at `4cd6dd8`; that bundle
  exists on disk but has been superseded — the status file's own `reuse_note` records HEAD
  moving off it.*
- **Drive-sync tripwire: GREEN.** `python datacollector/drive_sync_tripwire.py` (read-only)
  reports threat1_volume clean, threat2_registry clean (0 rows overlap), proxy_artifacts clean:
  "TradingDesk-Local is NOT under Drive management."
- **Conductor: 15 open items.** `#28`, `#29`, `#31` closed this cycle (closed_date 2026-07-16 /
  07-17 / 07-20). `#32` (per-collector data-collection review) is open and is the next work.

---

## 2. WHAT LANDED (2026-07-16 .. 2026-07-20)

Both halves of the backup system now exist and **self-verify**.

**Code half — items #28 / #29 closed.**
`RepoBackupDaily` at 20:00, plus a `wrap`-triggered run (`datacollector\run_repo_backup_wrap.cmd`).
Produces a git bundle, copies it to the Drive volume, and then verifies **arrival in Google's
cloud** via the Drive API md5 — not merely that DriveFS shows a local file. Fail-closed via
`CLOUD_VERIFY_REQUIRED=True`: no cloud md5 match means no claimed backup.

**Data half — item #31 closed.**
`DataBackupDaily` at 21:00 runs `rclone copy` — **additive, never `sync`, never deletes** — from
`C:\TradingDesk-Local` to `databackup:TradingDesk-DataBackup`, excluding venv / backups / secrets,
then `rclone check --one-way --checksum` to verify.
First full backup (run by hand 2026-07-17 11:17 → 2026-07-18 15:42, ~28.5h): **499,539 files /
104.206 GiB copied, exit 0, zero collisions** despite concurrent data pulls; verification
confirmed **499,534 files byte-identical by md5 with ZERO `.parquet` mismatches**. That is the
first time the irreplaceable warehouse has existed in more than one place.

**The tier-classification fix (commit `9d80f13`).**
The first real verification exposed a defect: `data_backup.py` treated *any* check difference as
failure, and 17 benign differences showed up (live logs, `conductor.db`, files a concurrent
session created mid-run). As built it would have failed and paged **every single night** — a
false-page generator. Differences are now classified into three tiers:
- **Tier 1 — hard fail and page:** anything under `warehouse/raw/**` (write-once historical
  market data, `*.parquet` deliberately NOT forgivable), a remote-missing file whose local mtime
  predates the run start, unhashable files, and anything matching no known pattern.
- **Tier 2 — known-volatile, recorded but forgiven:** an explicit commented pattern list
  (`*.log`, `*.jsonl`, `*.db` + `-wal`/`-shm`, `*_manifest.json`, `*heartbeat*`, `*_progress*.json`,
  `*_state.json`, `*.lock`, `s8_pilot/logs/*`, two warehouse scripts by exact path).
- **Tier 3 — created during the run:** remote-missing with mtime newer than run start; copied next run.
The heartbeat refreshes only on zero tier-1 failures. Tests went 26 → 50 in
`test_data_backup.py`; datacollector package 271 passing.

**Drive-sync tripwire.** Pages if `C:\TradingDesk-Local` ever comes under Google Drive
sync/backup management — the wrong-folder risk from the 2026-07-07..16 incident. Currently green.

---

## 3. PENDING VERIFICATION — CHECK THIS FIRST NEXT SESSION

Tonight's (2026-07-20) **21:00 `DataBackupDaily` run is the FIRST live test of the
tier-classification fix (`9d80f13`)** — and the first execution of that task at all.

Read **`C:\TradingDesk-Local\backups\data_backup_status.json`**.

> **Note:** as of this writing that file **does not exist yet**, nor does `data_backup.log`.
> The first backup was driven by hand before the task existed. Tonight's run creates both.
> A missing file tomorrow morning therefore means **the task did not run** (check
> `Get-ScheduledTaskInfo -TaskName DataBackupDaily`) — it does not mean the file was deleted.

**Expect:** `ok=true`, a populated `benign_differences` section (live logs, `conductor.db`, files
created mid-run), and **ZERO tier-1 failures**.

**Decision rules — do not improvise these:**
- A **tier-1 failure is a real corruption / missing-data signal** in the immutable warehouse/raw
  data. **INVESTIGATE. Do not dismiss it, and do not "fix" it by widening the tier-2 list.**
- A **false page on benign churn** → add a tier-2 volatile pattern to `data_backup.py`
  **deliberately, with a stated reason**. Never a catch-all.

Also confirm the **20:00 `RepoBackupDaily`** run left `cloud.state=verified` in
`C:\TradingDesk-Local\backups\repo_backup_status.json`.

---

## 4. KNOWN ISSUES TO FIX (Andrew wants to dive into these)

**1. `investech/` is backed up by NOTHING.** Verified: **326 files, 153.4 MB** on disk;
**3 files tracked by git** (`investech/PROJECT_STATUS.md`, `investech/phase2_feed/config.py`,
`investech/phase2_feed/thetadata.py`). Untracked files are not in a git bundle, and the folder
lives under `C:\TradingDesk` (not `C:\TradingDesk-Local`), so the data backup does not cover it
either. **Decide: commit it, or declare it disposable.** This is the only unprotected work left
after the whole backup effort. (Context: the InvesTech project itself was shelved 2026-07-10 —
see `investech/PROJECT_STATUS.md` — which is an argument for "disposable," but that has never
been stated as a decision.)

**2. ~~`datacollector` has no working one-command test run~~ — ALREADY FIXED, do not re-open.**
*Correction:* the reported failure (bare `pytest -q` exiting 2 because
`ibkr_option_stream_test.py` / `ibkr_stream_test.py` call `int(sys.argv[1])` at import) was fixed
in commit **`ad6d40d`** by `datacollector/pytest.ini` (`python_files = test_*.py`), which narrows
collection instead of renaming or blacklisting the two hand-run diagnostic scripts. Verified live
2026-07-20: `cd datacollector` → `pytest -q` → **271 passed, exit 0**.

**3. Standing pre-approved permission that reads the secrets `.env`. CONFIRMED PRESENT.**
`.claude/settings.local.json` line 4 contains an allow-list entry: a PowerShell command that
tests for `C:\TradingDesk-Local\secrets\.env`, prints its line count, and prints each **key name**
(the text left of `=`). It prints names only, not values — but it is a standing pre-approval to
open the secrets file without a prompt. **Andrew's call whether to keep it.**
(Two related, narrower entries on lines 6–7 only print a boolean for whether `TIINGO_API_KEY` is
set in the environment; those are harmless.) No secret value was read or printed in producing
this handoff.

**4. `products\` has no documented purpose.** It contains only `S4_vol_control_fund` (which has
its own `README.md` and `DEPLOY.md`); nothing states what the *folder* is for or what else
belongs in it. **Needs one line from Andrew.**

**5. Residual backup exposure — stated so it is not forgotten.** The ~99 GB of data has exactly
**one cloud copy plus the local original**. There is no third copy and no offsite-from-Google
copy. **Accepted, not solved.** (Conductor item `#30` still carries the external-SSD / Backblaze
options, undecided.)

**6. Cosmetic.** Conductor item `#31`'s stored note contains a mojibake character (a U+FFFD
replacement char where an em-dash belonged): "Inert until then <?> mirrors RepoBackupDaily (#28)."

---

## 5. NEXT WORK — conductor item #32

**Per-collector data-collection review + restore-or-retire** (ThetaData, IBKR, Tiingo, FRED).

This is **not** a flip-it-back-on audit. For **each** collector:
1. **Re-read what the job actually does** — open its code/launcher, establish real current
   behavior, not remembered behavior.
2. **Trace the consumer** — which project / strategy / report actually consumes that data. If
   nothing does, that is a retire candidate.
3. **Keep / retire / change verdict** — decided *before* re-enabling. Retiring a collector nothing
   needs is a win, not a loss.
4. **Only survivors get re-enabled**, and on re-enable, **validate** that collection actually
   happens (heartbeat goes fresh, data lands where expected).

**Verdicts touching strategy data needs are Andrew's call** (pull-and-clarify; rule #1 frozen
config).

**TIME-BOXED:** per item `#25` the ThetaData subscription ends in weeks, and pre-cutover forward
history **can never be recreated** from IBKR once it lapses. Do not let the ThetaData-side
decisions drift past that.

**First concrete action:** build the **verified scheduled-task inventory + current on/off state
table** (Windows Task Scheduler + the `register_*.ps1` scripts + the `run_*.bat` launchers under
`C:\TradingDesk-Local\warehouse\` and `datacollector\`), from verified ground truth, not memory.
Then work purpose → consumer → verdict one collector at a time.

**Reconcile with, do not duplicate:** `#25` (ThetaData ending; IBKR crossover exists in code but
is not operational), `#10` (forward-collector depth widening built but unscheduled; greeks
side-by-side vs ThetaData still undone), `#34` (4001 forward pull + greeks validation), and
especially **`#35` — URGENT/UNFIXED**: all 20 warehouse launcher `.bat` files still point at the
deleted `My Drive` path, leaving 8 enabled tasks failing with `LastTaskResult=2`. `#35` is
awaiting Andrew's explicit go-ahead (per CLAUDE.md the collector/scheduled tasks are off-limits
without say-so) and overlaps #32 directly — the inventory pass will walk straight into it.

---

## 6. LAUNCH-MECHANICS LESSONS for `.ps1` registration scripts

Both now fixed in their own script headers, recorded here so the lesson is not relearned a third
time:

1. **Windows 11 has no right-click "Run as administrator" for `.ps1` files.** An instruction
   header that says to do that is impossible to follow (tripped `register_repo_backup_task.ps1`;
   fixed in `5914d21`).
2. **Invoking a `.ps1` path bare is refused by ExecutionPolicy** on this machine (tripped
   `register_data_backup_task.ps1`).

**The working form:**

```
Start-Process powershell -Verb RunAs -ArgumentList '-NoExit','-ExecutionPolicy','Bypass','-File','<path>'
```

The launch instructions matter as much as the script.
