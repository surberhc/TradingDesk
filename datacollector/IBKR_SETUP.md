# IB Gateway — settings for an all-day, every-day collector

Our forward collector connects to IB Gateway at `127.0.0.1:4002` (`readonly`) using
`ib_async`, reusing the RRG project's pattern. For a process that runs *through the
whole session, every day*, two Gateway settings matter. Confirm these once.

## 1. THE ONE THAT BITES — daily auto-logoff  ⚠️

IB Gateway logs itself out once every 24 hours. If that lands during market hours,
the collector dies mid-session. The fix is to make it **auto-restart at an off-hours
time** instead.

Steps (in **IB Gateway**, not TWS):
1. Top menu → **Configure → Settings** (the gear).
2. Left panel → **Lock and Exit**.
3. Choose **Auto restart** (NOT "Auto log off"), and set the time to the middle of
   the night, e.g. **11:45 PM**.
   - *Auto restart* re-authenticates automatically — no password typing, the session
     continues. This is what we want.
   - *Auto log off* would kill it and demand a manual login. Do not pick this.
4. **OK / Apply.**

Unavoidable caveat: even with auto-restart, IBKR forces a **full manual re-login about
once a week** (typically after the weekend, Sunday evening). Between those, auto-restart
keeps it alive. (For true unattended 24/7 we could later add IBC/IBController to automate
even the weekly login — a future upgrade, not needed now.)

## 2. API / socket settings — confirm

**Configure → Settings → API → Settings:**
- ✅ **Enable ActiveX and Socket Clients**
- **Socket port = 4002** (the Gateway paper port we connect to)
- ✅ **Read-Only API** — fine to leave ON; we only READ data, never trade
- ✅ **Allow connections from localhost only** (safe — everything runs on this machine)
- **Master API client ID:** leave blank

## 3. Already handled / confirmed
- **Live data:** verified — this connection returns LIVE (not delayed) data; your
  subscriptions are shared to the paper account. Nothing to change.
- **Machine sleep:** already set to never sleep on AC power.
- **clientId hygiene:** each script uses a distinct id so they never collide. RRG = 1;
  our test scripts = 21–24; the production collector gets its own (e.g. 30). Never run
  two clients on the same id at once.

## 4. Tomorrow's live validation (run at/after 09:30 ET)
With the Gateway running and logged in, from the warehouse folder:
```
<venv python> ibkr_option_stream_test.py 300 SPXW SPY
```
Watch for: `quoting`, `greeks`, and `OI` climbing toward the line count, `errors=0`,
`disconnects` empty (the one logged at the very end is the script's own clean shutdown).
Sustained over ~5 min with no line-limit (322/326) errors = the all-day-capable proof.
