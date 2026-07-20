"""
Lightweight .env fallback loader for the Phase-1 feed.

Goal: let the app supply FRED_API_KEY itself when it is not already present in
the process environment, so the scheduled batch wrapper does not have to parse
secrets. Behavior contract:

  * If os.environ already has a non-empty FRED_API_KEY, it WINS -- we never
    overwrite an already-set environment variable.
  * Otherwise, read it from the local secrets .env file (SECRETS_ENV_PATH),
    which uses simple `KEY=value` lines. Surrounding whitespace, matching
    single/double quotes, and a trailing carriage return are stripped.
  * The key value is never printed or logged. Only its presence/length is ever
    surfaced (and only by callers, not here).

The secrets path is a module-level constant so it is easy to change.
"""

import os

# Module-level constant -- change here if the secrets location moves.
SECRETS_ENV_PATH = r"C:\TradingDesk-Local\secrets\.env"

# The only key this loader is responsible for backfilling.
_TARGET_KEY = "FRED_API_KEY"


def _clean_value(raw):
    """Strip surrounding whitespace, trailing CR, and matching quotes."""
    val = raw.strip()
    # Drop a stray trailing carriage return (CRLF files read line-by-line).
    if val.endswith("\r"):
        val = val[:-1].strip()
    # Strip one matching pair of surrounding quotes, if present.
    if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
        val = val[1:-1].strip()
    return val


def load_fred_api_key(path=SECRETS_ENV_PATH):
    """
    Ensure FRED_API_KEY is in os.environ if available.

    Returns the key value if one is now present (already-set or freshly loaded),
    else None. Never raises on a missing/unreadable file -- a failed backfill
    simply leaves the environment untouched, and the fetchers fall back to their
    existing "needs_api_key" status.
    """
    # Already set (and non-empty) -> respect it, do not overwrite.
    existing = os.environ.get(_TARGET_KEY)
    if existing and existing.strip():
        return existing

    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            for line in f:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if "=" not in stripped:
                    continue
                name, _, raw_val = stripped.partition("=")
                if name.strip() != _TARGET_KEY:
                    continue
                value = _clean_value(raw_val)
                if value:
                    os.environ[_TARGET_KEY] = value
                    return value
                break  # found the key but it was empty -- stop looking
    except OSError:
        # Missing or unreadable secrets file: leave env untouched.
        return os.environ.get(_TARGET_KEY)

    return os.environ.get(_TARGET_KEY)


# Load on import so simply importing this module backfills the key.
load_fred_api_key()
