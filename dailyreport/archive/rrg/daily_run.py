import subprocess, sys, os, shutil, tempfile, atexit, glob, traceback
from datetime import datetime
import rrg_emailer
from connections import ibkr_paper as _ibkr
# Code lives in this folder (TradingDesk\dailyreport, in Drive). STATE — rrg.db,
# outputs, logs — lives on local C: so Drive sync can't corrupt the running DB.
CODE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = r'C:\TradingDesk-Local\state\dailyreport'
os.makedirs(STATE_DIR, exist_ok=True)
POLLER = os.path.join(CODE_DIR, 'rrg_poller.py')
COMPUTE = os.path.join(CODE_DIR, 'rrg_compute.py')
CANONICAL_DB = os.path.join(STATE_DIR, 'rrg.db')
BACKUP_DB = os.path.join(STATE_DIR, 'rrg.db.precompute.bak')

# --- run logging: tee all output (incl. subprocesses + email result) to a
# dated log so every scheduled run is auditable after the fact. ---
LOG_DIR = os.path.join(STATE_DIR, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)
_run_start = datetime.now()
LOG_PATH = os.path.join(LOG_DIR, f'rrg_run_{_run_start:%Y-%m-%d}.log')

class _Tee:
    def __init__(self, *streams): self._streams = streams
    def write(self, s):
        for st in self._streams:
            try: st.write(s); st.flush()
            except Exception: pass
    def flush(self):
        for st in self._streams:
            try: st.flush()
            except Exception: pass

_logf = open(LOG_PATH, 'a', encoding='utf-8')
sys.stdout = _Tee(sys.stdout, _logf)
sys.stderr = _Tee(sys.stderr, _logf)
atexit.register(lambda: (print(f'=== run ended {datetime.now():%Y-%m-%d %H:%M:%S} ==='),
                         _logf.flush(), _logf.close()))

def _excepthook(et, ev, tb):
    print('UNHANDLED EXCEPTION:')
    traceback.print_exception(et, ev, tb)
    try: rrg_emailer.send_failure('unhandled exception', f'{et.__name__}: {ev}')
    except Exception: pass
sys.excepthook = _excepthook

# prune logs older than 30 days
for _old in glob.glob(os.path.join(LOG_DIR, 'rrg_run_*.log')):
    try:
        if (_run_start - datetime.fromtimestamp(os.path.getmtime(_old))).days > 30:
            os.remove(_old)
    except Exception: pass

print('=' * 60)
print(f'RRG DAILY RUN START {_run_start:%Y-%m-%d %H:%M:%S}  (log: {LOG_PATH})')
print('checking if gateway already up (launching if needed)...')
# Shared, launch-mutex-protected gateway check/launch (connections.ibkr_paper.ensure_gateway):
# replaces this file's former local gateway_running()/Popen duplicate so RRG's launch
# path is coordinated with every other consumer instead of racing its own StartGatewayPaper.bat.
if not _ibkr.ensure_gateway():
    print('GATEWAY NEVER CAME UP — aborting, no poll run')
    sys.exit(1)
print('gateway ready, running poller...')
poll = subprocess.run([sys.executable, POLLER], capture_output=True, text=True)
if poll.stdout: print(poll.stdout, end='')
if poll.stderr: print(poll.stderr, end='')
if poll.returncode != 0:
    print(f'POLLER FAILED (exit {poll.returncode}) — skipping compute, canonical untouched')
    rrg_emailer.send_failure('poller', f'rrg_poller.py exited {poll.returncode}')
    sys.exit(1)
print('poller complete')
# --- guarded compute step ---
# Compute on a LOCAL temp copy (Drive mount lacks SQLite locking); only
# write back to canonical on a clean exit, after backing up the prior DB.
print('starting guarded compute...')
work = tempfile.mkdtemp(prefix='rrg_run_')
try:
    tmp_db = os.path.join(work, 'rrg.db')
    out_dir = os.path.join(work, 'out')
    os.makedirs(out_dir, exist_ok=True)
    shutil.copy2(CANONICAL_DB, tmp_db)
    print(f'  copied canonical -> {tmp_db}')
    # positional args only: <db_path> <out_dir>  (NO --db flag)
    comp = subprocess.run([sys.executable, COMPUTE, tmp_db, out_dir],
                          capture_output=True, text=True)
    if comp.stdout: print(comp.stdout, end='')
    if comp.stderr: print(comp.stderr, end='')
    if comp.returncode != 0:
        print(f'COMPUTE FAILED (exit {comp.returncode}) — canonical + backup UNTOUCHED')
        rrg_emailer.send_failure('compute', f'rrg_compute.py exited {comp.returncode}')
        sys.exit(1)
    print('  compute exit 0 — writing back')
    # rolling backup of prior canonical, then overwrite
    if os.path.isfile(CANONICAL_DB):
        shutil.copy2(CANONICAL_DB, BACKUP_DB)
        print(f'  backed up prior canonical -> {BACKUP_DB}')
    shutil.copy2(tmp_db, CANONICAL_DB)
    print(f'  wrote rrg.db -> {CANONICAL_DB}')
    # copy back every output compute produced (no hardcoded names)
    copied = 0
    for name in os.listdir(out_dir):
        src = os.path.join(out_dir, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(STATE_DIR, name))
            print(f'  wrote output -> {name}')
            copied += 1
    print(f'  {copied} output file(s) written back')
    # build the standalone HTML report into the canonical folder, then email it
    try:
        import rrg_report
        rrg_report.write_standalone(STATE_DIR)
        print('  wrote rrg_report.html')
    except Exception as e:
        print(f'  report build skipped: {type(e).__name__}: {e}')
    print('sending evening report email...')
    ok = rrg_emailer.send_success(STATE_DIR)
    print(f'  email send result: {"SENT" if ok else "FAILED"}')
finally:
    shutil.rmtree(work, ignore_errors=True)
print('daily run complete')
