r"""
drive_oauth_consent.py — ONE-TIME, RUN BY HAND: mint the Drive API refresh token
that lets repo_backup.py prove a bundle actually reached Google's CLOUD.

WHY THIS EXISTS (read repo_backup.py's header first — this closes its last gap)
------------------------------------------------------------------------------
repo_backup.py proves a bundle is a complete, git-readable bundle sitting on the
real DriveFS *volume*. It does NOT prove the bytes ever reached Google. That gap is
not academic — it is EXACTLY the 2026-07-07..07-16 failure, in which Drive
faithfully synced a folder that had stopped changing, 85 commits never left this
machine, and every local check stayed green the whole time. Answering "did the
bytes reach Google?" requires asking Google, and the unattended 20:00 job has no
credential of its own. This script mints one, once, interactively.

THE TRAP — THIS IS LOAD-BEARING, NOT ADMIN TRIVIA
-------------------------------------------------
An OAuth app left in "Testing" publishing status issues refresh tokens that EXPIRE
AFTER 7 DAYS. Google does not warn you, and nothing about the flow below looks any
different. The failure that produces is the worst possible shape for this project:

    day 0     consent granted; the cloud check goes green
    day 1-7   it passes nightly; we learn to trust it
    day 8     the refresh token is dead. The check fails every night, forever.
    day 9+    the nightly page becomes noise, someone mutes it, and we are back to
              a backup nobody is really watching.

That is the 2026-07-16 silent-trust failure rebuilt from parts, with extra steps.
The ONE thing that prevents it is setting the OAuth app's publishing status to
"In production" (APIs & Services -> OAuth consent screen -> PUBLISH APP) BEFORE
relying on the token. Refresh tokens from an in-production app do not expire on a
timer.

Publishing to production with a SENSITIVE scope — which drive.metadata.readonly is —
does NOT require Google's verification review for a single-user internal app. You
click through one "Google hasn't verified this app" interstitial, here, by hand,
once. That is a one-time annoyance. A 7-day token bomb is not.

WHY WE CANNOT SIMPLY DETECT IT — be honest, because believing a check that proves
less than it appears to is what caused the incident in the first place: Google
exposes NO API for an app's own publishing status, and a Testing-mode refresh token
is byte-identical to a production one. There is no probe to write. So this script
does the only two honest things available:
  1. It ASKS you (there is no way to know without asking) and records your answer
     into the credential file, so the artifact carries the risk instead of hiding it.
  2. It stamps the consent date into the credential file so that when a refresh IS
     rejected, repo_backup.py can say out loud "this failed N days after consent —
     if N is about 7, THIS IS THE TRAP."
Detection therefore lands at the moment of failure, not before it. Do not mistake
question 1 for a check; it is a written-down promise, and the only real defence is
actually publishing the app.

SCOPE — deliberately the smallest one that works
------------------------------------------------
    https://www.googleapis.com/auth/drive.metadata.readonly
Metadata is ALL the cloud check needs: it reads name/size/md5Checksum and nothing
else. This scope is "sensitive"; the obvious-looking drive.readonly is "restricted"
and carries a materially higher verification bar (and would hand a backup-checking
script the ability to read every byte in the Drive). We request metadata only. Do
not widen this without a reason that survives being written down.

WHAT IT WRITES
--------------
    C:\TradingDesk-Local\secrets\drive_oauth.json   (client_id, client_secret,
                                                     refresh_token, + provenance)
NEVER the repo — the path lives under the local secrets folder alongside .env, is
outside Drive, and is not versioned. This script prints the PATH and never prints a
token, a secret, or a code. The file is locked down to the current user via icacls
(os.chmod on Windows only toggles the read-only bit and would prove nothing).

STDLIB ONLY, ON PURPOSE
-----------------------
No google-auth, no google-api-python-client, no requests. repo_backup.py is the most
safety-critical script in this repo and CLAUDE.md forbids new heavy dependencies; a
credential minted by this script is consumed there, so this script holds the same
line. Installed-app OAuth is a form POST and a loopback redirect — urllib and
http.server cover it. PKCE (S256) is used because this is an installed app whose
"secret" is not really secret.

HOW TO GET THE CLIENT (Andrew does this once, in the browser)
------------------------------------------------------------
  1. console.cloud.google.com -> create a project (any name).
  2. APIs & Services -> Library -> enable "Google Drive API".
  3. APIs & Services -> OAuth consent screen -> External -> fill the required
     fields -> add the scope  .../auth/drive.metadata.readonly
  4. *** PUBLISH APP -> publishing status must read "In production" ***  (THE TRAP)
  5. Credentials -> Create credentials -> OAuth client ID -> Application type:
     "Desktop app" -> download the JSON.
  6. Run:  <venv python> drive_oauth_consent.py --client-secrets <that.json>

Run:
    <venv python> drive_oauth_consent.py --client-secrets client_secret_xxx.json
    <venv python> drive_oauth_consent.py            # prompts for id/secret instead
    <venv python> drive_oauth_consent.py --force    # overwrite an existing credential

Exit codes: 0 = a refresh token was written. Non-zero = nothing was written.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import getpass
import hashlib
import http.server
import json
import os
import secrets
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

# The job that consumes this credential owns the path. Import it rather than keeping
# a hand-maintained SECOND COPY of the constant here — a copied constant drifting
# from its source is what caused the 2026-07-09 false pages (see heartbeat_alarm.py).
import repo_backup as rb

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
SCOPE = "https://www.googleapis.com/auth/drive.metadata.readonly"

# The consent redirect comes back to a loopback server on an EPHEMERAL port. A
# "Desktop app" client accepts any 127.0.0.1 port without pre-registering it, which
# is why no port is hardcoded (and why nothing has to be configured in the console).
REDIRECT_HOST = "127.0.0.1"

CONSENT_DEADLINE_S = 300   # how long we wait for the human to finish in the browser
TOKEN_TIMEOUT_S = 30       # never hang on the token exchange


_TRAP_BANNER = r"""
===============================================================================
  THE 7-DAY TRAP — answer the next question carefully.

  An OAuth app whose publishing status is "Testing" issues refresh tokens that
  EXPIRE AFTER 7 DAYS. The cloud check would pass all week, earn trust, then
  fail every night from day 8 forever — which is the silent-trust failure this
  whole piece of work exists to prevent, rebuilt from parts.

  Fix it BEFORE continuing, in the browser:
      APIs & Services -> OAuth consent screen -> PUBLISH APP
      publishing status must read "In production"

  (A sensitive scope like drive.metadata.readonly does NOT need Google's
  verification review for a single-user app. Click through the "unverified app"
  interstitial once and you are done.)
===============================================================================
"""


# --------------------------------------------------------------------------- #
# PKCE
# --------------------------------------------------------------------------- #
def _b64url(raw: bytes) -> str:
    """Base64url WITHOUT padding — what RFC 7636 asks for."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def make_pkce() -> tuple[str, str]:
    """-> (code_verifier, code_challenge). S256, per RFC 7636.

    This is an installed app: the "client secret" ships on the machine and is not
    really a secret, so the verifier is what actually binds the redirect to this
    process. 32 random bytes -> a 43-char verifier, the spec's minimum length.
    """
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


# --------------------------------------------------------------------------- #
# Client id/secret — from the console's JSON, or typed in
# --------------------------------------------------------------------------- #
def read_client_secrets(path) -> tuple[str, str]:
    """Pull (client_id, client_secret) out of the console's downloaded JSON.

    Google wraps them under "installed" for a Desktop client (what we want) or
    "web" for a Web client (wrong type — a web client will reject the loopback
    redirect, so we say so plainly rather than failing later with a vaguer error).
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if "installed" in data:
        blob = data["installed"]
    elif "web" in data:
        raise SystemExit(
            f"{path} is a 'Web application' OAuth client. This flow needs a "
            f"'Desktop app' client — a web client will refuse the loopback redirect. "
            f"Create one: Credentials -> Create credentials -> OAuth client ID -> "
            f"Application type: Desktop app.")
    else:
        blob = data  # already flat
    try:
        return blob["client_id"], blob["client_secret"]
    except KeyError as e:
        raise SystemExit(f"{path} has no {e} — is it really an OAuth client JSON?")


def prompt_client_secrets() -> tuple[str, str]:
    """Ask for the client id/secret. The secret is read with getpass — never echoed."""
    print("Paste the Desktop OAuth client's credentials (Credentials -> your client).")
    client_id = input("  client_id: ").strip()
    client_secret = getpass.getpass("  client_secret (not echoed): ").strip()
    if not client_id or not client_secret:
        raise SystemExit("client_id and client_secret are both required.")
    return client_id, client_secret


# --------------------------------------------------------------------------- #
# The loopback redirect — where Google hands back the authorization code
# --------------------------------------------------------------------------- #
_DONE_PAGE = (b"<html><body style='font-family:sans-serif'>"
              b"<h2>Consent captured.</h2>"
              b"<p>Close this tab and return to the terminal.</p>"
              b"</body></html>")

_FAIL_PAGE = (b"<html><body style='font-family:sans-serif'>"
              b"<h2>Consent failed.</h2>"
              b"<p>Read the terminal for the reason.</p>"
              b"</body></html>")


def _make_handler(holder: dict, expected_state: str):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler's spelling
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            # The browser also asks for /favicon.ico; ignore anything that carries
            # neither a code nor an error rather than treating it as the redirect.
            if "code" not in params and "error" not in params:
                self.send_response(404)
                self.end_headers()
                return
            state = (params.get("state") or [""])[0]
            if state != expected_state:
                # A redirect we did not initiate. Refuse it — this is the CSRF check.
                holder["error"] = ("state mismatch on the redirect — refusing the code "
                                   "(it did not come from this run)")
            elif "error" in params:
                holder["error"] = f"Google returned error={params['error'][0]}"
            else:
                holder["code"] = params["code"][0]
            body = _FAIL_PAGE if "error" in holder else _DONE_PAGE
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):  # keep http.server's stderr chatter out of the log
            pass

    return Handler


def capture_code(client_id: str, challenge: str, *,
                 deadline_s: int = CONSENT_DEADLINE_S,
                 open_fn=None, serve_cls=None) -> tuple[str, str]:
    """Run the consent flow. -> (code, redirect_uri). Raises SystemExit on failure.

    The server is bound FIRST, on port 0, because the redirect_uri must contain the
    real port before the auth URL can be built.
    """
    holder: dict = {}
    state = _b64url(secrets.token_bytes(16))
    serve_cls = serve_cls or http.server.HTTPServer
    srv = serve_cls((REDIRECT_HOST, 0), _make_handler(holder, state))
    try:
        redirect_uri = f"http://{REDIRECT_HOST}:{srv.server_port}"
        url = AUTH_URL + "?" + urllib.parse.urlencode({
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            # offline + consent is what actually yields a REFRESH token. Without
            # prompt=consent Google may return only an access token on a repeat
            # authorization, and an access token dies in an hour — useless to a
            # nightly unattended job.
            "access_type": "offline",
            "prompt": "consent",
        })

        print(f"\nOpening the consent screen in your browser. Listening on {redirect_uri}")
        print("If the browser does not open, paste this URL into it yourself:\n")
        print(url + "\n")
        (open_fn or webbrowser.open)(url)

        srv.timeout = 1.0
        end = time.monotonic() + deadline_s
        while not holder and time.monotonic() < end:
            srv.handle_request()
    finally:
        try:
            srv.server_close()
        except Exception:  # noqa: BLE001
            pass

    if "error" in holder:
        raise SystemExit(f"consent failed: {holder['error']}")
    if "code" not in holder:
        raise SystemExit(f"consent timed out after {deadline_s}s — nothing was written.")
    return holder["code"], redirect_uri


# --------------------------------------------------------------------------- #
# Code -> refresh token
# --------------------------------------------------------------------------- #
def exchange_code(code: str, verifier: str, redirect_uri: str,
                  client_id: str, client_secret: str, *,
                  urlopen_fn=None, timeout: int = TOKEN_TIMEOUT_S) -> dict:
    """Swap the authorization code for tokens. -> the token payload.

    Error bodies from Google are echoed because they are diagnostic and carry no
    secret. The REQUEST is never echoed — it holds the client_secret and the code.
    """
    body = urllib.parse.urlencode({
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "code_verifier": verifier,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }).encode("ascii")
    req = urllib.request.Request(
        rb.GOOGLE_TOKEN_URL, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with (urlopen_fn or urllib.request.urlopen)(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:400]
        except Exception:  # noqa: BLE001
            pass
        raise SystemExit(f"token exchange failed: HTTP {e.code} {detail}")
    except urllib.error.URLError as e:
        raise SystemExit(f"token exchange failed: network error ({e.reason!r})")


# --------------------------------------------------------------------------- #
# Writing the credential — path printed, values never
# --------------------------------------------------------------------------- #
def lock_down(path) -> str:
    """Restrict the credential to the current user. -> human note.

    os.chmod on Windows only flips the read-only bit — it does NOT restrict other
    users, so it would look like a permission check while proving nothing. icacls is
    what actually edits the NTFS ACL: strip inheritance, grant this user only.
    """
    notes = []
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError as e:
        notes.append(f"chmod failed ({e!r})")

    user = os.environ.get("USERNAME")
    domain = os.environ.get("USERDOMAIN")
    if not user:
        return "; ".join(notes + ["no USERNAME in env — ACL NOT restricted"])
    account = f"{domain}\\{user}" if domain else user
    try:
        p = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{account}:F"],
            capture_output=True, text=True, timeout=30, check=False)
        if p.returncode == 0:
            notes.append(f"ACL restricted to {account} only (inheritance removed)")
        else:
            notes.append(f"icacls exited {p.returncode}: "
                         f"{(p.stdout or p.stderr or '').strip()[:200]}")
    except (OSError, subprocess.SubprocessError) as e:
        notes.append(f"icacls failed ({e!r}) — ACL NOT restricted")
    return "; ".join(notes)


def write_credential(payload: dict, *, path=None, write_fn=None) -> Path:
    """Write drive_oauth.json. Prints the PATH; never a value."""
    target = Path(path or rb.DRIVE_OAUTH_FILE)
    target.parent.mkdir(parents=True, exist_ok=True)
    if write_fn is not None:
        write_fn(target, payload)
        return target
    tmp = str(target) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(payload, indent=2))
    os.replace(tmp, target)
    return target


def confirm_publishing_status(*, input_fn=None) -> str:
    """Ask whether the app is published. -> 'in_production' | 'testing_or_unknown'.

    THIS IS NOT A CHECK — there is no API for an app's publishing status and a
    Testing refresh token is byte-identical to a production one. It is a written-down
    answer that gets stamped into the credential so the eventual failure can name its
    own cause. Treating it as proof would be exactly the mistake this repo keeps
    paying for.
    """
    ask = input_fn or input
    print(_TRAP_BANNER)
    ans = (ask("Is the OAuth app's publishing status 'In production'? [yes/no] ")
           or "").strip().lower()
    if ans in ("y", "yes"):
        return "in_production"
    print("\n*** WARNING: proceeding against an app that is NOT confirmed published.\n"
          "*** If it is in 'Testing', this refresh token DIES IN 7 DAYS and the cloud\n"
          "*** check will fail every night from day 8. The answer is being recorded in\n"
          "*** the credential file, and repo_backup.py will name this as the likely\n"
          "*** cause when the refresh is rejected. Publish the app and re-run this.\n")
    return "testing_or_unknown"


def main() -> int:
    ap = argparse.ArgumentParser(
        description="One-time: mint the Drive API refresh token repo_backup.py uses "
                    "to prove a bundle reached Google's cloud.")
    ap.add_argument("--client-secrets", metavar="PATH",
                    help="the Desktop OAuth client JSON downloaded from the GCP "
                         "console. Omit to be prompted for client_id/client_secret.")
    ap.add_argument("--force", action="store_true",
                    help="overwrite an existing credential file.")
    ap.add_argument("--yes-published", action="store_true",
                    help="skip the publishing-status question (you are asserting the "
                         "app is 'In production' — see THE TRAP in this file's header).")
    args = ap.parse_args()

    target = Path(rb.DRIVE_OAUTH_FILE)
    if target.exists() and not args.force:
        print(f"A credential already exists at {target}\n"
              f"Re-run with --force to replace it. Nothing was written.")
        return 1

    if args.client_secrets:
        client_id, client_secret = read_client_secrets(args.client_secrets)
    else:
        client_id, client_secret = prompt_client_secrets()

    status = ("in_production" if args.yes_published
              else confirm_publishing_status())

    verifier, challenge = make_pkce()
    code, redirect_uri = capture_code(client_id, challenge)
    tokens = exchange_code(code, verifier, redirect_uri, client_id, client_secret)

    refresh = tokens.get("refresh_token")
    if not refresh:
        # Almost always prompt=consent being dropped, or a re-authorization of a
        # grant that already exists. An access token alone is useless to a nightly job.
        print("Google returned NO refresh_token — only a short-lived access token, "
              "which a nightly unattended job cannot use. Revoke this app's access at "
              "myaccount.google.com/permissions and run this script again.")
        return 3

    path = write_credential({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh,
        "scope": SCOPE,
        # Provenance — read back by repo_backup.py when a refresh is REJECTED, so the
        # failure can name the 7-day trap by its age instead of guessing.
        "obtained_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "publishing_status": status,
        "minted_by": "drive_oauth_consent.py",
    })
    acl = lock_down(path)

    print(f"\nrefresh token written to: {path}")
    print(f"  permissions: {acl}")
    print(f"  scope:       {SCOPE}")
    print(f"  publishing:  {status}")
    if status != "in_production":
        print("\n  *** THIS TOKEN LIKELY EXPIRES IN 7 DAYS. Publish the app "
              "('In production') and re-run with --force. ***")
    print("\nNext: flip CLOUD_VERIFY_REQUIRED to True in repo_backup.py once you have "
          "seen a run report cloud state 'verified'. Until then the check runs but "
          "cannot fail the backup job.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
