#!/usr/bin/env python3
"""
rrg_emailer.py — send the RRG report by email.

Credentials are read from %USERPROFILE%\\rrg_secrets.env (OUTSIDE the synced
Drive folder) and/or real environment variables. Required keys:
    RRG_SMTP_USER   full sending address / SMTP login   (andrew@surberhc.com)
    RRG_SMTP_PASS   Gmail App Password (16 chars, no spaces)
    RRG_MAIL_FROM   From: header        (defaults to RRG_SMTP_USER)
    RRG_MAIL_TO     To: header          (andrew@taxfavoredretirement.com)
Optional:
    RRG_SMTP_HOST   default smtp.gmail.com
    RRG_SMTP_PORT   default 587 (STARTTLS)

Public API:
    send_success(out_dir)        -> inline HTML report + CSV attachment
    send_failure(stage, detail)  -> short plain-text alert
Both return True on success, False on failure (never raise into the caller).
"""

import os, ssl, smtplib, mimetypes
from email.message import EmailMessage

import csv as _csv
import rrg_report

SECRETS = os.path.join(os.environ.get("USERPROFILE", ""), "rrg_secrets.env")


def _subject_tilt(out_dir):
    """Authoritative headline tilt for the subject line: latest stabilized
    weekly state from rrg_regime.csv (falls back to 'update' if unavailable)."""
    path = os.path.join(out_dir, "rrg_regime.csv")
    last = "update"
    if os.path.isfile(path):
        with open(path, newline="") as f:
            for row in _csv.DictReader(f):
                if row.get("timeframe") == "weekly":
                    last = row.get("tilt", last)
    return last


def _load_cfg():
    cfg = {}
    if os.path.isfile(SECRETS):
        with open(SECRETS, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    # real env vars win over the file
    for k in ("RRG_SMTP_USER", "RRG_SMTP_PASS", "RRG_MAIL_FROM",
              "RRG_MAIL_TO", "RRG_SMTP_HOST", "RRG_SMTP_PORT"):
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    cfg.setdefault("RRG_SMTP_HOST", "smtp.gmail.com")
    cfg.setdefault("RRG_SMTP_PORT", "587")
    cfg.setdefault("RRG_MAIL_FROM", cfg.get("RRG_SMTP_USER", ""))
    return cfg


def _send(msg, cfg):
    host, port = cfg["RRG_SMTP_HOST"], int(cfg["RRG_SMTP_PORT"])
    ctx = ssl.create_default_context()
    with smtplib.SMTP(host, port, timeout=30) as s:
        s.starttls(context=ctx)
        s.login(cfg["RRG_SMTP_USER"], cfg["RRG_SMTP_PASS"])
        s.send_message(msg)


def send_success(out_dir):
    try:
        cfg = _load_cfg()
        html = rrg_report.build_report_html(out_dir, embed="cid")

        msg = EmailMessage()
        msg["From"] = cfg["RRG_MAIL_FROM"]
        msg["To"] = cfg["RRG_MAIL_TO"]
        msg["Subject"] = f"RRG Evening Report — weekly tilt {_subject_tilt(out_dir)}"
        msg.set_content("Your mail client does not render HTML. See the attached "
                        "rrg_table.csv, or open rrg_report.html.")
        msg.add_alternative(html, subtype="html")

        # inline chart referenced by cid:rrgchart inside the HTML part
        png = os.path.join(out_dir, "rrg_quadrant.png")
        with open(png, "rb") as f:
            html_part = msg.get_payload()[1]
            html_part.add_related(f.read(), maintype="image", subtype="png",
                                  cid=f"<{rrg_report.CHART_CID}>",
                                  filename="rrg_quadrant.png")

        # CSV attachment for Excel
        csv_path = os.path.join(out_dir, "rrg_table.csv")
        with open(csv_path, "rb") as f:
            msg.add_attachment(f.read(), maintype="text", subtype="csv",
                               filename="rrg_table.csv")

        _send(msg, cfg)
        print(f"  email sent -> {cfg['RRG_MAIL_TO']}")
        return True
    except Exception as e:
        print(f"  EMAIL FAILED: {type(e).__name__}: {e}")
        return False


def send_failure(stage, detail=""):
    try:
        cfg = _load_cfg()
        msg = EmailMessage()
        msg["From"] = cfg["RRG_MAIL_FROM"]
        msg["To"] = cfg["RRG_MAIL_TO"]
        msg["Subject"] = f"RRG pipeline FAILED at {stage}"
        msg.set_content(f"The RRG daily run failed at: {stage}\n\n{detail}\n\n"
                        "Canonical rrg.db was left untouched. No report generated.")
        _send(msg, cfg)
        print(f"  failure alert sent -> {cfg['RRG_MAIL_TO']}")
        return True
    except Exception as e:
        print(f"  FAILURE-ALERT EMAIL ALSO FAILED: {type(e).__name__}: {e}")
        return False


if __name__ == "__main__":
    import sys
    out = sys.argv[1] if len(sys.argv) > 1 else "."
    ok = send_success(out)
    sys.exit(0 if ok else 1)
