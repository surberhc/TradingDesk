"""
Minimal .env loader for the Phase-2 breadth feed.

Reads simple KEY=value lines from the desk .env into os.environ WITHOUT echoing
any value. Rules:
  - strip surrounding whitespace, matching quotes, and trailing CR (\r)
  - a value already present in the real environment WINS (we never overwrite it)
  - missing/malformed file never crashes the caller
  - returns the set of key NAMES loaded -- never the values

The Tiingo API key is read through this loader and is NEVER printed anywhere.
"""

import os

import config


def load_dotenv(path=None):
    """Load KEY=value lines from `path` (default config.DOTENV_PATH) into
    os.environ without overwriting existing env vars. Returns set of key names.
    """
    path = path or config.DOTENV_PATH
    loaded = set()
    if not path or not os.path.exists(path):
        return loaded
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip().lstrip("﻿")  # tolerate BOM
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.lower().startswith("export "):
                    line = line[len("export "):]
                key, _, val = line.partition("=")
                key = key.strip()
                # strip whitespace, surrounding quotes, and any stray CR
                val = val.strip().strip("\r").strip('"').strip("'").strip("\r")
                if not key:
                    continue
                # Environment var already set takes priority.
                if key not in os.environ:
                    os.environ[key] = val
                loaded.add(key)
    except Exception:
        # A malformed .env must never abort the feed.
        pass
    return loaded


def get_tiingo_key():
    """Return the Tiingo API key (or None) WITHOUT printing it."""
    # Env var wins if already set; otherwise pull from the desk .env.
    if os.environ.get(config.TIINGO_KEY_ENV):
        return os.environ[config.TIINGO_KEY_ENV]
    load_dotenv()
    return os.environ.get(config.TIINGO_KEY_ENV) or None


def get_thetadata_key():
    """Return the ThetaData API key (or None) WITHOUT printing it.

    Used only to confirm the exchange-breadth source is configured before
    probing the local ThetaData Terminal. The value is never echoed or placed
    in a URL/log.
    """
    key_env = getattr(config, "THETADATA_KEY_ENV", "THETADATA_API_KEY")
    if os.environ.get(key_env):
        return os.environ[key_env]
    load_dotenv()
    return os.environ.get(key_env) or None
