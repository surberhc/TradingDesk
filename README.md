# TradingDesk

The one home for the whole trading/market project. Everything that was scattered
across `Market_Data`, `Tier 1 Alpha`, `backtester`, and two stray local folders gets
sorted into the folders below.

**The rule:** *code* lives here in Google Drive (synced + backed up). *Data, running
state, the Python runtime, and secrets* live on local C: and are never synced.

## Folders (what goes where)

| Folder | Plain English | Came from |
|---|---|---|
| `strategies\` | the strategy recipes — one file per strategy; both the backtester and paperbot read these same files | (rebuilt from backtester) |
| `backtester\` | test a strategy on past data | Drive\backtester |
| `paperbot\` | run a strategy on the PAPER account (port 4002, account DU…141) | local trading_engine + Market_Data\trading_engine spec |
| `connections\` | the one shared way to reach IBKR + Tiingo, and the master clientId list (collision-proof) | NEW |
| `datacollector\` | gathers options market data into the warehouse | Market_Data\options_warehouse |
| `dailyreport\` | the 5 PM regime email | Market_Data\rrg_*.py, daily_run.py |
| `msr\` | turns the daily newsletter PDFs into feature data | Tier 1 Alpha |
| `docs\` | specs, handoffs, plans | scattered .md files |

## Local companions (NOT in Drive)
- `C:\MarketData\` — the data warehouse (options parquet + duckdb)
- `C:\TradingState\` — the paperbot order ledger (created when the bot runs)
- `rrg.db` — daily-report state (moves here from Drive)
- `C:\TradingDesk-Local\venv` — the Python runtime

## Status
**Empty shell — nothing migrated yet.** Folder names are up for review. Once approved,
files move in one careful pass (the backtester stays provably byte-identical).
