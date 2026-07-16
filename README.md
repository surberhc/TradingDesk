# TradingDesk

An Interactive Brokers trading system — research, paper execution, data, and reporting.

## Which folder do I work out of?

**This one: `C:\TradingDesk`.** It holds the code and it is the git repo. Everything you
edit by hand lives here.

`C:\TradingDesk-Local\` sits next to it on the C: drive and is **not** part of the repo —
it holds 107 GB of market data, the venv, secrets, run state, and backups. Code *writes*
to it; you almost never hand-edit anything there. The one exception is `secrets\`.

| | `C:\TradingDesk` | `C:\TradingDesk-Local` |
|---|---|---|
| what | code | data, venv, secrets, state, backups |
| size | 3,880 files, 2.0 GB | 550,071 files, **107 GB** |
| in git? | **yes — this is the repo** | no. never versioned, never synced |
| you edit it? | **yes** | no — written by code (except `secrets\`) |

**About the name:** the `-Local` suffix is a **fossil**. It once meant "local, i.e. NOT in
Google Drive," back when the code itself lived in Drive. The code moved out of Drive on
2026-07-16 (Drive had silently synced the wrong folder for 9 days, 2026-07-07 → 07-16).
Both folders are local now, so the suffix distinguishes nothing. The distinction that
actually survives is **versioned vs. not-versioned**. It is deliberately **not** being
renamed: `TradingDesk-Local` is hardcoded in ~330 places across 165 tracked files (venv
paths, warehouse roots, backup destinations, the `RepoBackupDaily` scheduled task).

**The rule:** *code* here at `C:\TradingDesk`, a plain local folder, deliberately OUTSIDE
Google Drive. *Data, running state, the Python runtime, and secrets* on
`C:\TradingDesk-Local\`. Drive is a backup destination for git bundles only — never the
working copy. Never work in, or point code at, any `My Drive` path.

## Running things

- Python is the local venv — every command needs it:
  `C:\TradingDesk-Local\venv\Scripts\python.exe`
- Code derives repo paths from `__file__` (e.g. `Path(__file__).resolve().parents[N]`),
  never absolute strings. The Drive move above is why.

## Folders

| Folder | Plain English |
|---|---|
| `strategies\` | the strategy recipes — one file per strategy; the backtester and paperbot both read these same files (the shared brain) |
| `backtester\` | test a strategy on past data |
| `paperbot\` | run a strategy on the PAPER account (port 4002, account DU…141) |
| `livebot\` | S8's live pilot — its own package. Connects to the live-TRADE Gateway (port 4003, a real funded account) to read genuine data, but is **zero-transmit**: `PILOT_MODE = True` is hardcoded in `s8_runner.py` and it only logs "WOULD HAVE TRANSMITTED" |
| `connections\` | the one shared way to reach IBKR + Tiingo. Holds the authoritative clientId registry (`connections\connections\clientids.py` — never collide) and `GATEWAYS.md` |
| `datacollector\` | gathers options market data into the warehouse |
| `dailyreport\` | the 5 PM regime email |
| `dashboard\` | read-only Streamlit app showing S0 state (and an S8 monitor tab); never places, arms, or transmits an order |
| `msr\` | turns the daily newsletter PDFs into feature data |
| `conductor\` | STATUS + handoffs, SQLite-backed. **`STATUS.md` is generated** — don't hand-edit it; run `conductor\cli.py log/open/close` then `conductor\cli.py render` |
| `docs\` | specs, handoffs, plans |
| `products\` | deploy-ready packagings of a strategy that import the shared `strategies` engine rather than forking it. Currently holds one: `S4_vol_control_fund` |
| `british_ic\` | forensic reconstruction of the externally-traded British IC strategy (SPX 0DTE credit spreads) from a real IBKR Flex export — the research S8 was derived from |
| `canslim\` | diligence on an outside advisor's real-money IBD/CAN SLIM growth-stock strategy — is the edge real, and what would it cost to run ourselves? |
| `investech\` | **shelved 2026-07-10** — abandoned analysis of the InvesTech market-timing newsletter; kept for reference. Don't resume without explicit direction |
| `Brandon W\` | raw source material (SMS alert exports, PDFs, spreadsheets) for reverse-engineering the "SPX Cash Flow Secrets" 0DTE strategy — S6 research |
| `AI Pathways\` | preserved third-party source material: backtester prompts transcribed from a YouTube video, kept as a validation cross-reference |
| `.claude\` | Claude Code project config — dashboard launch profile + local permission allowlist |

See `CLAUDE.md` for the operating contract (the two non-negotiables, the verification bar,
guardrails) and `docs\CONCURRENT_SESSIONS.md` for the worktree workflow.
