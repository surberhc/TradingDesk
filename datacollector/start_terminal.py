"""
start_terminal.py — launch the ThetaData v3 local Terminal (the REST gateway).

ThetaData v3 works by running a local Java process (ThetaTerminalv3.jar) that
authenticates with your API key and serves a REST API on 127.0.0.1:25503. Your
data code then talks to that local endpoint — the key is only needed HERE, to
start the Terminal.

Security: the key is read from the secret .env (outside Drive) and handed to the
Java process via the THETA_DATA_API_KEY environment variable. It is NEVER printed,
echoed, or written anywhere by this script (per CLAUDE.md secret-handling rules).

Prereqs (one-time, on your side):
  1. Install Java 21+  (e.g. `winget install EclipseAdoptium.Temurin.21.JDK`).
  2. This script auto-downloads ThetaTerminalv3.jar to C:\\TradingDesk-Local\\warehouse if missing.

Run:  python start_terminal.py     (leave it running in its own window while you pull data)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import urllib.request

import config

JAR_URL = "https://download-unstable.thetadata.us/ThetaTerminalv3.jar"


def _read_key() -> str:
    """Read the ThetaData key from the secret .env. Returns the value (never logged)."""
    if not config.SECRET_ENV.exists():
        sys.exit(f"Secret file not found: {config.SECRET_ENV}")
    for line in config.SECRET_ENV.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, val = line.partition("=")
        if name.strip() == config.THETA_KEY_NAME:
            val = val.strip().strip('"').strip("'")
            if not val:
                sys.exit(f"{config.THETA_KEY_NAME} is present but empty in {config.SECRET_ENV}")
            return val
    sys.exit(f"{config.THETA_KEY_NAME} not found in {config.SECRET_ENV}")


def _ensure_java() -> str:
    # Prefer PATH; fall back to a freshly-installed Adoptium JDK that a not-yet-
    # restarted shell wouldn't see on PATH yet.
    java = shutil.which("java")
    if java:
        return java
    import glob
    hits = glob.glob(r"C:\Program Files\Eclipse Adoptium\jdk-21*\bin\java.exe")
    if hits:
        return hits[0]
    sys.exit("Java not found. Install Java 21+ "
             "(`winget install EclipseAdoptium.Temurin.21.JDK`), reopen the shell, retry.")


def _ensure_jar() -> None:
    config.DATA_ROOT.mkdir(parents=True, exist_ok=True)
    if config.THETA_TERMINAL_JAR.exists():
        return
    print(f"Downloading Theta Terminal -> {config.THETA_TERMINAL_JAR} ...")
    urllib.request.urlretrieve(JAR_URL, config.THETA_TERMINAL_JAR)
    print("  done.")


def main() -> None:
    java = _ensure_java()
    _ensure_jar()
    key = _read_key()                      # value held only in memory
    env = dict(os.environ, THETA_DATA_API_KEY=key)   # belt-and-suspenders
    print(f"Launching Theta Terminal (REST on {config.THETA_BASE_URL}). "
          "Leave this window open while pulling data. Ctrl-C to stop.\n")
    # This build doesn't read the env var reliably, so pass --api-key (the value is
    # never printed by us). Run from the jar's local dir so any creds/.env lookups
    # land in C:\TradingDesk-Local\warehouse, not the Drive code folder.
    subprocess.run([java, "-jar", str(config.THETA_TERMINAL_JAR), "--api-key", key],
                   env=env, cwd=str(config.DATA_ROOT), check=False)


if __name__ == "__main__":
    main()
