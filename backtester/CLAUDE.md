# CLAUDE.md — Adaptive All-Weather Core Backtester

This file is read at the start of every session. Follow it before doing anything else.

## What this project is

A backtesting engine for a rules-based, multi-engine tactical asset-allocation strategy ("Adaptive All-Weather Core, smoothness-first revision"). It reads historical ETF price data from local files, runs the strategy month by month, and produces an HTML results report. This is in-house research tooling. It is NOT connected to any brokerage and places no live trades.

The full strategy logic lives in SPEC.md. The data layer lives in DATA.md. Read both before writing strategy or data code.

## Where this project lives (IMPORTANT — read first)

- This project lives at `C:\TradingDesk\backtester` — a plain local folder, deliberately
  OUTSIDE Google Drive. Open future sessions here. It moved off Drive 2026-07-16 after
  Drive silently synced the wrong folder for 9 days and orphaned the repo; Drive is now
  only a backup destination for git bundles, never the working copy. Any
  `My Drive (andrew@surberhc.com)\TradingDesk` copy is a DEAD orphan — never use it.
- The virtual environment is kept LOCAL on purpose (so 320 MB of machine-specific files
  aren't version-tracked or synced):
  `C:\TradingDesk-Local\venv`  ->  python at `C:\TradingDesk-Local\venv\Scripts\python.exe`
- The Tiingo API key is read from the `TIINGO_API_KEY` environment variable (set as a
  Windows USER env var). A local `.env` outside the repo also works. `.env` is never
  committed.

## Tech stack and environment

- OS: Windows. The user runs commands in Command Prompt (cmd).
- Python: 3.12 via the LOCAL venv above (not in the project folder, to avoid Drive sync).
- Run Python with the local venv, from the Drive project folder. In cmd:
  `"C:\TradingDesk-Local\venv\Scripts\python.exe" -m src.run`
  (or activate it: `C:\TradingDesk-Local\venv\Scripts\activate`)
- Core libraries: pandas, numpy, requests (data download), plotly (charts), jinja2 (HTML report), pytest (tests). Pin versions in requirements.txt.
- Do not introduce heavy backtesting frameworks (backtrader, zipline, vectorbt). This engine is purpose-built and self-contained so the user can read and understand every part. Plain, well-commented Python only.

## Project structure (create and maintain this layout)

- .env : SECRET. Tiingo API key. NEVER read, print, or commit this.
- .gitignore : must list .env and data/ and .venv/
- CLAUDE.md : this file
- SPEC.md : strategy specification — the source of truth for logic
- DATA.md : data download specification
- requirements.txt
- README.md : running status log: what's built, what's next
- data/ : downloaded price files (Parquet/CSV). Treat as READ-ONLY once downloaded.
- src/download_data.py : fetches data from Tiingo into data/ (per DATA.md)
- src/data_loader.py : loads + validates local data, handles inception dates
- src/engines/ : one file per engine (regime, equity, defensive, duration, etc.)
- src/portfolio.py : combines engine outputs into target weights
- src/backtest.py : the month-by-month simulation loop
- src/metrics.py : performance + risk metrics
- src/report.py : builds the HTML results report
- src/run.py : top-level entry point the user runs
- tests/ : one test file per engine + integration tests
- output/ : generated HTML reports + result files

## Hard rules — do not violate

1. NEVER read, print, echo, or display the contents of .env. It holds a live API key. If you need the key in code, read it via environment variable (os.environ / python-dotenv) — never hard-code it, never log it.
2. NEVER commit .env, data/, or .venv/ to git. Ensure .gitignore covers them.
3. Treat downloaded files in data/ as read-only. Never overwrite or edit them as a side effect. Re-downloading is an explicit, separate action.
4. No look-ahead bias. A signal computed for month-end date T may only use data available on or before T. Execution happens at T+1 (next trading day). This is the single most important correctness rule. Guard it everywhere.
5. Build one engine at a time, and write a pytest test for each engine as you build it, before wiring it into the portfolio. Even though the user wants the full spec built before the first end-to-end run, each engine must be independently tested so failures can be isolated. Do not wire all engines together until each has a passing unit test.
6. Ask before installing anything not in requirements.txt.

## Workflow conventions

- Before writing code for a new piece, state your plan in one short paragraph.
- After building each engine: write its test, run it, report pass/fail.
- Keep functions small and named in plain English. Comment the "why," not the "what."
- Use type hints. Prefer pandas vectorized operations but clarity beats cleverness.
- When a number in the spec is a tunable parameter, read it from a single central config.py (or params file) so the user can change one value in one place — never scatter magic numbers through the code.
- Update README.md status section after each meaningful step so a fresh session can pick up where the last left off.

## How the user works

The user is the strategy owner and decision-maker, new to Claude Code, and works by copy-paste. When you need them to run something, give the exact command in a copy-paste-ready block, one command at a time, and tell them what success looks like. Do not assume coding fluency. Explain results in plain English, lead with the answer.
