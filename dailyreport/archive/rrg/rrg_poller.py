from ib_async import IB, Stock
import sqlite3, os, shutil, tempfile
from datetime import datetime
from connections import clientids
# State (rrg.db) lives on local C:, not Drive: Drive lacks SQLite locking and can
# corrupt the DB mid-write. Code lives in TradingDesk\dailyreport.
STATE_DIR = r'C:\TradingDesk-Local\state\dailyreport'
DB = os.path.join(STATE_DIR, 'rrg.db')
BACKUP_DB = os.path.join(STATE_DIR, 'rrg.db.prepoll.bak')
BENCHMARK = 'SPY'
SECTORS = ['XLK','XLF','XLE','XLV','XLI','XLP','XLY','XLU','XLB','XLRE','XLC']
SYMBOLS = [BENCHMARK] + SECTORS
def get_bars(ib, sym, bar_size, duration):
    c = Stock(sym, 'SMART', 'USD')
    return ib.reqHistoricalData(c, endDateTime='', durationStr=duration,
        barSizeSetting=bar_size, whatToShow='TRADES', useRTH=True, formatDate=1)
def main():
    # Write to a LOCAL temp DB (Drive mount lacks SQLite locking), seeded from
    # canonical so existing history is preserved, then promote on success.
    work = tempfile.mkdtemp(prefix='rrg_poll_')
    tmp_db = os.path.join(work, 'rrg.db')
    try:
        # seed temp from canonical so INSERT OR REPLACE merges into existing bars
        if os.path.isfile(DB):
            shutil.copy2(DB, tmp_db)
            print(f'seeded temp from canonical ({os.path.getsize(tmp_db)} bytes)')
        conn = sqlite3.connect(tmp_db)
        conn.execute('''CREATE TABLE IF NOT EXISTS bars(
            symbol TEXT, date TEXT, timeframe TEXT, open REAL, high REAL,
            low REAL, close REAL, volume REAL, pulled_at TEXT,
            PRIMARY KEY(symbol, date, timeframe))''')
        ib = IB()
        ib.connect('127.0.0.1', 4002,
                   clientId=clientids.CLIENT_IDS["dailyreport_poller"], readonly=True)
        now = datetime.now().isoformat()
        rows = 0
        try:
            for tf, size, dur in [('daily','1 day','1 Y'), ('weekly','1 week','3 Y')]:
                for sym in SYMBOLS:
                    for b in get_bars(ib, sym, size, dur):
                        conn.execute(
                            'INSERT OR REPLACE INTO bars VALUES(?,?,?,?,?,?,?,?,?)',
                            (sym, str(b.date), tf, b.open, b.high, b.low,
                             b.close, b.volume, now))
                        rows += 1
                    ib.sleep(1)
            conn.commit()
        finally:
            conn.close()
            ib.disconnect()
        # promote: back up canonical, then overwrite with merged temp DB
        if os.path.isfile(DB):
            shutil.copy2(DB, BACKUP_DB)
            print(f'backed up prior canonical -> {BACKUP_DB}')
        shutil.copy2(tmp_db, DB)
        print(f'{rows} rows written; promoted temp -> {DB}')
    finally:
        shutil.rmtree(work, ignore_errors=True)
main()
