"""
mailer.py — generic HTML email for the daily report.

Reuses the SAME credentials the RRG emailer used (%USERPROFILE%\\rrg_secrets.env:
RRG_SMTP_USER / RRG_SMTP_PASS / RRG_MAIL_FROM / RRG_MAIL_TO, Gmail STARTTLS), so the
recipient stays whatever that file already targets — no new secret to manage. This
just sends an arbitrary subject + HTML body (+ optional attachments) instead of the
RRG-specific report.
"""

from __future__ import annotations

import datetime as _dt
import os
import smtplib
import ssl
from email.message import EmailMessage

SECRETS = os.path.join(os.environ.get("USERPROFILE", ""), "rrg_secrets.env")

# Out-of-band email-channel tripwires. These are the ONLY signals that survive a
# dead email credential: last_email_ok.txt is a heartbeat the EOD/alarm reads, and
# TRADINGDESK_EMAIL_DOWN.txt is a plain-text flag dropped on the Desktop so a broken
# mail path is VISIBLE even though nothing can be emailed. Every file op below is
# wrapped so it can NEVER raise into send_html()'s caller.
_EMAIL_OK_FILE = r"C:\TradingDesk-Local\state\last_email_ok.txt"


def _desktop_down_file() -> str:
    return os.path.join(os.environ["USERPROFILE"], "Desktop",
                        "TRADINGDESK_EMAIL_DOWN.txt")


def _note_email_ok(subject: str) -> None:
    """SUCCESS side-effects: write the last-ok heartbeat and clear any Desktop
    email-down flag (the channel recovered). Never raises."""
    try:
        os.makedirs(os.path.dirname(_EMAIL_OK_FILE), exist_ok=True)
        with open(_EMAIL_OK_FILE, "w", encoding="utf-8") as f:
            f.write(f"{_dt.datetime.now().isoformat(timespec='seconds')}  {subject}\n")
    except Exception:
        pass
    try:
        down = _desktop_down_file()
        if os.path.exists(down):
            os.remove(down)
    except Exception:
        pass


def _note_email_down(subject: str, err: Exception) -> None:
    """FAILURE side-effect: drop a plain-English flag on the Desktop so a dead mail
    credential is visible without any email being sent. Never raises."""
    try:
        down = _desktop_down_file()
        os.makedirs(os.path.dirname(down), exist_ok=True)
        with open(down, "w", encoding="utf-8") as f:
            f.write(
                "TRADINGDESK EMAIL IS DOWN\n"
                "=========================\n"
                f"When:    {_dt.datetime.now().isoformat(timespec='seconds')}\n"
                f"Subject: {subject}\n"
                f"Error:   {type(err).__name__}: {err}\n\n"
                "The desk tried to send an email and FAILED. No email could be sent "
                "to warn you, so this file is the out-of-band signal. Check the Gmail "
                "app-password / SMTP creds in %USERPROFILE%\\rrg_secrets.env, then run "
                "C:\\TradingDesk-Local\\warehouse\\run_eod.bat manually. This file is "
                "auto-deleted on the next successful send.\n")
    except Exception:
        pass

_KEYS = ("RRG_SMTP_USER", "RRG_SMTP_PASS", "RRG_MAIL_FROM",
         "RRG_MAIL_TO", "RRG_SMTP_HOST", "RRG_SMTP_PORT")


def _load_cfg() -> dict:
    cfg: dict = {}
    if os.path.isfile(SECRETS):
        with open(SECRETS, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    for k in _KEYS:                       # real env vars win over the file
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    cfg.setdefault("RRG_SMTP_HOST", "smtp.gmail.com")
    cfg.setdefault("RRG_SMTP_PORT", "587")
    cfg.setdefault("RRG_MAIL_FROM", cfg.get("RRG_SMTP_USER", ""))
    return cfg


def recipient() -> str:
    """The configured To: address (so the report can show where it's going)."""
    return _load_cfg().get("RRG_MAIL_TO", "")


def send_html(subject: str, html: str,
              attachments: list[tuple] | None = None) -> bool:
    """Send an HTML email. attachments = [(filename, bytes, maintype, subtype), ...].
    Returns True on success, False on failure (never raises into the caller)."""
    try:
        cfg = _load_cfg()
        msg = EmailMessage()
        msg["From"] = cfg["RRG_MAIL_FROM"]
        msg["To"] = cfg["RRG_MAIL_TO"]
        msg["Subject"] = subject
        msg.set_content("This is an HTML report; your mail client did not render it.")
        msg.add_alternative(html, subtype="html")
        for fn, data, mt, st in (attachments or []):
            msg.add_attachment(data, maintype=mt, subtype=st, filename=fn)
        host, port = cfg["RRG_SMTP_HOST"], int(cfg["RRG_SMTP_PORT"])
        ctx = ssl.create_default_context()
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls(context=ctx)
            s.login(cfg["RRG_SMTP_USER"], cfg["RRG_SMTP_PASS"])
            s.send_message(msg)
        print(f"  email sent -> {cfg.get('RRG_MAIL_TO')}")
        _note_email_ok(subject)
        return True
    except Exception as e:
        print(f"  EMAIL FAILED: {type(e).__name__}: {e}")
        _note_email_down(subject, e)
        return False
