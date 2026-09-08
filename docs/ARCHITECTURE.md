# TradingDesk — Architecture Reference

**The one place that answers: "where does everything live, and what is it?"**

_Last verified: 2026-09-08_

There are **two roots**. They are kept separate on purpose:

1. **The CODE root** lives on the C: drive, **outside** Google Drive — version-tracked by git.
2. **The LOCAL root** lives on the C: drive — it is **never synced** (data, state, and the Python engine that runs everything).

The simple rule: **code and data both stay local on C:; nothing works out of Drive.** Drive sync is slow, has corrupted files in the past, and (2026-07-16) silently orphaned the repo itself.

---

## 1. The CODE root (plain local folder — NOT Drive)

`C:\TradingDesk\`

This is the git repository. It was moved out of Google Drive on 2026-07-16 after Drive silently synced the wrong folder for 9 days (2026-07-07 → 2026-07-16), moving/duplicating its own folders and orphaning the repo. Drive is now only a **backup destination for git bundles — never the working copy**. Backup is git + bundles, not Drive sync.

| Folder | What it is |
|---|---|
| `strategies\` | The shared strategy **"brain"** (a Python package named `strategies`). Both the backtester and the paperbot import it, so **what we test is what we trade** — they can't drift apart. On 2026-09-08 Strategy 8's pure definition files (`s8_config.py` and `s8_strategy.py`) were moved here out of `livebot\`, so every strategy's definition now lives in this one folder. |
| `backtester\` | The research / backtest engine. Imports `strategies`. Has its own test suite (89 tests). |
| `paperbot\` | The **PAPER** execution engine (paper accounts only — never real money). Imports `strategies` and `connections`. |
| `connections\` | Shared broker/data access layer (package `connections`): `ibkr_paper.py` / `ibkr_live_data.py` / `ibkr_live_trade.py` (the three Gateway lanes — see `connections/GATEWAYS.md`), `tiingo.py` (price data), `clientids.py` (connection ID registry). |
| `datacollector\` | The options-data collector — pulls from ThetaData and writes into the local warehouse. |
| `dailyreport\` | The end-of-day status email harness (sends the EOD digest). |
| `msr\` | Newsletter-PDF → features pipeline. **Note:** it contains a sub-folder confusingly named `Backtester Handoff` — that is **DATA**, not a session handoff. Don't look there for handoffs. |
| `dashboard\` | The **desk dashboard** — the screen Andrew actually looks at (Streamlit, `dashboard\desk\`). See the section below for what is on it. |
| `livebot\` | The Strategy 8 live-pilot runtime: the all-day service, the intraday collector, the teardown/reaper and the desk-launch link. Since 2026-09-08 it holds only the **running machinery** — S8's strategy definition itself moved to `strategies\`. |
| `conductor\` | Cross-session coordination files (see below). |
| `docs\` | Project documentation. **This file lives here.** |

_Also present at the code root and not covered by the table above: `british_ic\`, `canslim\`, `investech\`, `products\`, `top15\`, `crm\`, `AI Pathways\`, `Brandon W\`, `backups\` — research and project folders rather than parts of the running desk._

### What is on the desk dashboard (as of 2026-09-08)

The menu has **nine items**, cut down from thirteen on 2026-09-08:

Desk Pulse; Feeds & Connections; History & Event Log; Strategy 0 — Adaptive All-Weather Core;
Strategy Models — what each model holds and why; Trade Execution — pick, prepare, send;
Monthly Cash Distributions; Strategy 8 — British Iron Condor (0DTE); Research shelf.

Three things about that list are worth writing down, because they are changes rather than the way it has always been:

- **"Group trade" is now called "Trade Execution."** Same page, clearer name.
- **Three separate model pages became one.** "Strategy 0 — Model & Parameters", "Strategy Models — all models" and "Custom allocation" all answered the same question, so they are now the single **Strategy Models** page. Nothing any of them showed was dropped.
- **Two pages were retired outright: the Control Plane, and the desk's own Action Center.** The desk no longer keeps its own alert screen. `dashboard\desk\action_center.py` still exists, but it is now only a **poster**: it writes the desk's notices straight into the **client system's `public.tasks` table**, which is the Action Center Andrew actually reads. The old local SQLite notice store is gone, and nothing was displaying it anyway.

The page modules underneath were regrouped at the same time: per-strategy pages live in `dashboard\desk\strategy_views\` and the trading page lives in `dashboard\desk\execution\`.

### What's inside `conductor\` (the coordination hub)

| File / folder | Role |
|---|---|
| `STATUS.md` | The **live dashboard / source of truth** — start here to see current state. |
| `DECISIONS.md` | The decisions inbox/outbox (what was decided and what's pending). |
| `MISSION.md` | The standing mission / goals. |
| `handoffs\` | Dated drop-folder for session handoffs (where one work session leaves notes for the next). |

_Also at the root: `README.md` and `.gitignore`. (A stray folder named "Andrew is pissed off" also exists at the root and is not part of the structure.)_

---

## 2. The LOCAL root (on C: — NEVER synced; data, state & runtime)

`C:\TradingDesk-Local\`  — about **31.5 GB** total.

This holds the big, fast-changing stuff that must stay off Drive.

| Folder | Size | What it is |
|---|---|---|
| `warehouse\` | ~31 GB | The **options data warehouse** — ThetaData end-of-day option chains (SPY, SPXW, QQQ, XSP, + others). Actively growing. Contains `catalog.duckdb` (the index) and `ThetaTerminalv3.jar` (the data feed). |
| `venv\` | ~445 MB | The Python **virtual environment** everything runs on. The Python interpreter is at `C:\TradingDesk-Local\venv\Scripts\python.exe`. |
| `state\` | ~15 MB | Runtime state: `dailyreport\` (RRG/Tiingo outputs), `paperbot\` (including `fa_groups_backup.xml`), and gateway diagnostics. |
| `bt_data\` | ~2.2 MB | The backtester's **price data** (parquet files). The backtester reads its prices from here. (Moved here off Drive on 2026-06-27 — see warning below.) |
| `secrets\` | ~1 KB | Credentials. |

---

## 3. Key facts to remember

- **Code vs. data split is deliberate.** Code lives in the local git repository at `C:\TradingDesk\`, which is **outside** Google Drive; Drive receives only the backup bundles. Data + state + the venv live local on C: in `C:\TradingDesk-Local\` and are **never synced** — this keeps large/churning files out of Drive sync.

- **The Tiingo API key** is stored as a Windows **user environment variable** (and/or a local `.env`). It is **never on Drive and never committed to git.**

- **⚠️ Data-stability warning (important).** The backtester's price data was **moved OFF Google Drive into `C:\TradingDesk-Local\bt_data\` on 2026-06-27**, because Drive sync had been **silently reverting/corrupting the parquet files mid-session.** Do not move price data back onto Drive.

- **GFC / pre-2010 history is NOT fully on disk.** For the full universe we currently only have history back to about 2005 for **AGG and LQD**. A SPY-only CSV reaching back to 2008 exists at `msr\Flow Project\flow_verdict\data\`. **A full 2008 (Global Financial Crisis) backtest would require re-downloading extended history from Tiingo first.**

- **The old code copy is gone.** `C:\Users\andre\backtester\` no longer exists. The only backtester is the one inside the Drive code root.

---

_If something isn't where this document says it is, this document is wrong — fix it here and update the "Last verified" date._
