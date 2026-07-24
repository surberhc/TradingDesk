"""
cli.py — argparse CLI over the conductor SQLite DB (conductor/db.py).

Replaces hand-editing conductor/STATUS.md and conductor/DECISIONS.md. Usage:

    python conductor/cli.py log --title "..." [--body "..."] [--date 2026-07-09]
    python conductor/cli.py open --title "..." --area "..." [--notes "..."]
    python conductor/cli.py close --id 3
    python conductor/cli.py block --id 3 [--notes "..."]
    python conductor/cli.py decide --lane C --question "..." [--options "..."]
    python conductor/cli.py answer --id 2 --answer "..."
    python conductor/cli.py status
    python conductor/cli.py render

Run with the repo venv: C:\\TradingDesk-Local\\venv\\Scripts\\python.exe conductor/cli.py ...
"""
from __future__ import annotations

import argparse
import datetime as _dt
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from db import get_connection
import render as render_mod


def _today() -> str:
    return _dt.date.today().isoformat()


def _current_branch() -> str:
    """Default session_tag: the current git branch name. Falls back to 'unknown'."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=Path(__file__).resolve().parent.parent,
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def cmd_log(args):
    conn = get_connection()
    date = args.date or _today()
    session_tag = args.session_tag or _current_branch()
    conn.execute(
        "INSERT INTO log_entries (date, session_tag, title, body_md, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (date, session_tag, args.title, args.body or "", _dt.datetime.now().isoformat()),
    )
    conn.commit()
    new_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    conn.close()
    print(f"Logged entry #{new_id}: [{date}] {args.title}")


def cmd_open(args):
    conn = get_connection()
    session_tag = args.session_tag or _current_branch()
    today = _today()
    conn.execute(
        "INSERT INTO items (title, area, status, opened_date, last_touched, "
        "closed_date, session_tag, notes) VALUES (?, ?, 'open', ?, ?, NULL, ?, ?)",
        (args.title, args.area, today, today, session_tag, args.notes or ""),
    )
    conn.commit()
    new_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    conn.close()
    print(f"Opened item #{new_id} [{args.area}]: {args.title}")


def cmd_close(args):
    conn = get_connection()
    today = _today()
    cur = conn.execute(
        "UPDATE items SET status='done', last_touched=?, closed_date=? WHERE id=?",
        (today, today, args.id),
    )
    conn.commit()
    if cur.rowcount == 0:
        print(f"No item with id {args.id}")
    else:
        print(f"Closed item #{args.id}")
    conn.close()


def cmd_block(args):
    conn = get_connection()
    today = _today()
    if args.notes:
        cur = conn.execute(
            "UPDATE items SET status='blocked', last_touched=?, notes=? WHERE id=?",
            (today, args.notes, args.id),
        )
    else:
        cur = conn.execute(
            "UPDATE items SET status='blocked', last_touched=? WHERE id=?",
            (today, args.id),
        )
    conn.commit()
    if cur.rowcount == 0:
        print(f"No item with id {args.id}")
    else:
        print(f"Blocked item #{args.id}")
    conn.close()


def cmd_park(args):
    conn = get_connection()
    today = _today()
    if args.notes:
        cur = conn.execute(
            "UPDATE items SET status='parked', last_touched=?, notes=? WHERE id=?",
            (today, args.notes, args.id),
        )
    else:
        cur = conn.execute(
            "UPDATE items SET status='parked', last_touched=? WHERE id=?",
            (today, args.id),
        )
    conn.commit()
    if cur.rowcount == 0:
        print(f"No item with id {args.id}")
    else:
        print(f"Parked item #{args.id} (shelved — excluded from active list, retrievable)")
    conn.close()


def cmd_unpark(args):
    conn = get_connection()
    today = _today()
    cur = conn.execute(
        "UPDATE items SET status='open', last_touched=? WHERE id=?",
        (today, args.id),
    )
    conn.commit()
    if cur.rowcount == 0:
        print(f"No item with id {args.id}")
    else:
        print(f"Unparked item #{args.id} (back to open)")
    conn.close()


def cmd_decide(args):
    conn = get_connection()
    conn.execute(
        "INSERT INTO decisions (lane, question, options, status, answer, decided_date) "
        "VALUES (?, ?, ?, 'pending', NULL, NULL)",
        (args.lane, args.question, args.options or ""),
    )
    conn.commit()
    new_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    conn.close()
    print(f"Recorded pending decision #{new_id} [{args.lane}]: {args.question}")


def cmd_answer(args):
    conn = get_connection()
    today = _today()
    cur = conn.execute(
        "UPDATE decisions SET status='answered', answer=?, decided_date=? WHERE id=?",
        (args.answer, today, args.id),
    )
    conn.commit()
    if cur.rowcount == 0:
        print(f"No decision with id {args.id}")
    else:
        print(f"Answered decision #{args.id}")
    conn.close()


def cmd_status(args):
    conn = get_connection()
    open_items = conn.execute(
        "SELECT * FROM items WHERE status NOT IN ('done', 'parked') ORDER BY area, "
        "CASE status WHEN 'blocked' THEN 0 ELSE 1 END, opened_date"
    ).fetchall()
    pending = conn.execute(
        "SELECT * FROM decisions WHERE status = 'pending' ORDER BY id"
    ).fetchall()
    conn.close()

    print("=== OPEN ITEMS (by area) ===")
    if not open_items:
        print("  (none)")
    else:
        grouped: dict[str, list] = {}
        for it in open_items:
            grouped.setdefault(it["area"] or "unclassified", []).append(it)
        for area in sorted(grouped):
            print(f"  [{area}]")
            for it in grouped[area]:
                tag = " (BLOCKED)" if it["status"] == "blocked" else ""
                notes = f" — {it['notes']}" if it["notes"] else ""
                print(f"    #{it['id']}{tag} {it['title']}{notes}")

    print()
    print("=== PENDING DECISIONS ===")
    if not pending:
        print("  (none)")
    else:
        for d in pending:
            opts = f" | options: {d['options']}" if d["options"] else ""
            print(f"  #{d['id']} [{d['lane']}] {d['question']}{opts}")

    conn2 = get_connection()
    parked = conn2.execute(
        "SELECT * FROM items WHERE status = 'parked' ORDER BY area, id"
    ).fetchall()
    conn2.close()
    print()
    print(f"=== PARKED / SHELVED ({len(parked)}) — hidden from active list, `unpark --id N` to restore ===")
    for it in parked:
        print(f"    #{it['id']} [{it['area']}] {it['title']}")


def cmd_render(args):
    text = render_mod.render()
    print(f"Rendered {render_mod.STATUS_PATH}")
    print(f"Rendered {render_mod.EXPORT_PATH}")
    print(f"({len(text)} chars)")


def main():
    parser = argparse.ArgumentParser(description="TradingDesk conductor CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_log = sub.add_parser("log", help="add a dated log entry")
    p_log.add_argument("--title", required=True)
    p_log.add_argument("--body", default="")
    p_log.add_argument("--date", default=None)
    p_log.add_argument("--session-tag", dest="session_tag", default=None)
    p_log.set_defaults(func=cmd_log)

    p_open = sub.add_parser("open", help="open a new tracked item")
    p_open.add_argument("--title", required=True)
    p_open.add_argument("--area", required=True)
    p_open.add_argument("--notes", default="")
    p_open.add_argument("--session-tag", dest="session_tag", default=None)
    p_open.set_defaults(func=cmd_open)

    p_close = sub.add_parser("close", help="mark an item done")
    p_close.add_argument("--id", required=True, type=int)
    p_close.set_defaults(func=cmd_close)

    p_block = sub.add_parser("block", help="mark an item blocked")
    p_block.add_argument("--id", required=True, type=int)
    p_block.add_argument("--notes", default="")
    p_block.set_defaults(func=cmd_block)

    p_park = sub.add_parser("park", help="shelve an item (hidden until unparked)")
    p_park.add_argument("--id", required=True, type=int)
    p_park.add_argument("--notes", default="")
    p_park.set_defaults(func=cmd_park)

    p_unpark = sub.add_parser("unpark", help="restore a parked item to open")
    p_unpark.add_argument("--id", required=True, type=int)
    p_unpark.set_defaults(func=cmd_unpark)

    p_decide = sub.add_parser("decide", help="record a pending decision")
    p_decide.add_argument("--lane", required=True)
    p_decide.add_argument("--question", required=True)
    p_decide.add_argument("--options", default="")
    p_decide.set_defaults(func=cmd_decide)

    p_answer = sub.add_parser("answer", help="answer a pending decision")
    p_answer.add_argument("--id", required=True, type=int)
    p_answer.add_argument("--answer", required=True)
    p_answer.set_defaults(func=cmd_answer)

    p_status = sub.add_parser("status", help="print open items + pending decisions")
    p_status.set_defaults(func=cmd_status)

    p_render = sub.add_parser("render", help="regenerate STATUS.md + status_export.md")
    p_render.set_defaults(func=cmd_render)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
