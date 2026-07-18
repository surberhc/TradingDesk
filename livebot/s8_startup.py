"""s8_startup.py — S8 live-pilot STARTUP CONNECT-RETRY (shared, pure, offline-testable).

WHY THIS EXISTS (the Monday-breaking gap it closes)
---------------------------------------------------
Both all-day S8 pilot processes (``s8_service`` and ``s8_collector``) are launched by
Windows Task Scheduler a few minutes after the IB Gateway's own scheduled start. If the
Gateway is not yet ACCEPTING API connections at that moment — IBC still booting, or a 2FA
approval still pending on the phone — ``ib.connect`` raises ``ConnectionRefusedError``
(WinError 1225) and, before this module, the process died instantly with a raw multi-line
uncaught traceback. The only cushion was the gateway lead time, which IBC startup plus a
slow 2FA can easily consume.

Crucially, we must NOT rely on Windows Task Scheduler's "restart on failure" as the safety
net: that setting triggers on *unexpected termination*, and does NOT reliably fire on a
non-zero exit code returned by a cmd.exe wrapper. So the processes have to be SELF-HEALING
at startup: they bounded-retry the connect themselves.

WHAT IT DOES
------------
``connect_with_retry`` polls the injected connect callable every
``STARTUP_CONNECT_POLL_SECS`` for up to ``STARTUP_CONNECT_WAIT_SECS``, logging ONE legible
line per attempt. A connection-style failure (``ConnectionRefusedError`` / ``OSError`` /
``TimeoutError``) is treated as "gateway not ready yet" and retried. Any OTHER exception is
a genuine bug or misconfiguration and PROPAGATES immediately — it is never swallowed or
retried forever. If the bounded window elapses, ``StartupConnectTimeout`` is raised; the
callers turn that into a single logged line + a nonzero rc via ``SystemExit`` — never a raw
traceback.

PURE SEAM: the connect callable, the clock and the sleep are all injected, so a fake clock
makes this instant and fully offline-testable (no broker, no network, no real sleeps) —
exactly the pattern ``s8_collector.wait_for_live_spot`` already uses for the startup
DATA wait. The two are complementary and ordered: connect-retry first (is the gateway
there at all?), then the data wait (is live data flowing yet?).

Zero-transmit is untouched: this module only ever calls the read-only connect callable it
is handed; it has no order path and no knowledge of orders.
"""

from __future__ import annotations

import time

# --- Bounded startup connect window ---------------------------------------------------- #
# ~20 minutes at a ~20s cadence: comfortably covers IBC startup plus a slow/late 2FA
# approval, while still being bounded so a truly dead gateway ends in a clean exit rather
# than a process that hangs forever pretending to be healthy.
STARTUP_CONNECT_WAIT_SECS = 1200.0   # ~20 min total bounded startup window
STARTUP_CONNECT_POLL_SECS = 20.0     # re-try the connect every ~20s

# Failures that mean "the gateway isn't accepting API connections YET" (retry), as opposed
# to a genuine bug (propagate). ConnectionRefusedError is the WinError 1225 case seen in the
# dry run; OSError covers the other socket-level refusals/unreachables; TimeoutError covers
# a gateway that accepts the socket but never completes the API handshake in time.
_RETRYABLE_CONNECT_ERRORS = (ConnectionRefusedError, TimeoutError, OSError)


class StartupConnectTimeout(RuntimeError):
    """Raised when the bounded startup connect window elapses with the gateway still down.

    A CAUGHT, handled condition — callers turn it into a clean logged exit + nonzero rc,
    never a raw uncaught traceback. Distinct type so the startup path can catch exactly
    this and let genuine bugs propagate.
    """


def is_retryable_connect_error(exc: BaseException) -> bool:
    """True if ``exc`` looks like "the gateway is not accepting API connections yet"."""
    return isinstance(exc, _RETRYABLE_CONNECT_ERRORS)


def connect_with_retry(
    connect,
    *,
    label: str = "s8",
    port: int = 4003,
    # Resolved at CALL time (not bound as a def-time default) so the module constants stay
    # the single source of truth and a test can shrink the window by monkeypatching them.
    timeout_secs: float = None,   # type: ignore[assignment]  -> STARTUP_CONNECT_WAIT_SECS
    poll_secs: float = None,      # type: ignore[assignment]  -> STARTUP_CONNECT_POLL_SECS
    clock=time.monotonic,
    sleep=time.sleep,
    log=print,
):
    """Call ``connect()`` until it succeeds, or the bounded startup window elapses.

    PURE seam — ``connect``, ``clock`` and ``sleep`` are injected, so a fake clock makes
    this instant and fully offline-testable with no broker/network/real sleeps.

    Returns whatever ``connect()`` returns on the first success. Retries (logging one line
    per attempt) while it raises a retryable connect error. Raises ``StartupConnectTimeout``
    once ``timeout_secs`` has elapsed — the caller turns that into a clean exit. Any
    non-connection exception propagates immediately (a bug is not something to retry).
    """
    timeout_secs = STARTUP_CONNECT_WAIT_SECS if timeout_secs is None else timeout_secs
    poll_secs = STARTUP_CONNECT_POLL_SECS if poll_secs is None else poll_secs
    deadline = clock() + float(timeout_secs)
    attempt = 0
    while True:
        attempt += 1
        try:
            ib = connect()
        except Exception as exc:  # noqa: BLE001 — re-raised below unless it's a connect refusal
            if not is_retryable_connect_error(exc):
                raise
            log(f"{label}: waiting for IB Gateway on port {port} (attempt {attempt}) — "
                f"not accepting API connections yet ({type(exc).__name__}: {exc})")
        else:
            if attempt > 1:
                log(f"{label}: IB Gateway on port {port} accepted the connection "
                    f"after {attempt} attempt(s).")
            return ib
        # Give up only once the bounded window is exhausted.
        if clock() >= deadline:
            raise StartupConnectTimeout(
                f"gateway never became available on port {port} after "
                f"{float(timeout_secs) / 60.0:g} minutes ({attempt} attempt(s)) — "
                f"exiting for restart.")
        sleep(float(poll_secs))
