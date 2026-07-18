"""test_s8_gateway_alert.py — OFFLINE tests for the GATEWAY DOWN / RELAUNCH email failsafe.

NO real email, NO real broker, NO real gateway, NO real sleeps: the mailer, the
``ensure_gateway`` callable, the clock, the dedup-marker path and BOTH diagnostic probes are
all injected, so every case here runs instantly against fakes.

What is proved:
  * ONE DOWN EMAIL + A BACK-UP FOLLOW-UP when the injected ensure_gateway returns True.
  * A FAILED RELAUNCH sends the relaunch-FAILED email, and NOT a back-up one — silence must
    never be able to mean "fine".
  * DEDUP — two processes (service + collector) handling the SAME outage inside the cooldown
    produce EXACTLY ONE down email between them.
  * AFTER THE COOLDOWN a genuinely new outage DOES alert again (the dedup marker is a
    recency window, not a permanent mute).
  * A RAISING MAILER and a RAISING ensure_gateway are both swallowed — alerting is
    best-effort and can never propagate into the pilot loop.
  * DIAGNOSTICS carry the OBSERVED fields and no fabricated cause.
  * The inverse 2FA warning (the whole point of the failsafe) appears in every email.

Run (from C:\\TradingDesk\\livebot):
    powershell -Command "$env:PYTHONPATH=''; C:\\TradingDesk-Local\\venv\\Scripts\\python.exe -m pytest test_s8_gateway_alert.py -q"
"""

from __future__ import annotations

import pytest

import s8_gateway_alert as ga


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #

class FakeMailer:
    """Captures send_html calls instead of sending anything. NO SMTP, ever."""

    def __init__(self, raises: bool = False) -> None:
        self.sent = []
        self.raises = raises

    def send_html(self, subject, html):
        if self.raises:
            raise RuntimeError("SMTP is down (simulated)")
        self.sent.append((subject, html))
        return True

    @property
    def subjects(self):
        return [s for s, _ in self.sent]


class FakeClock:
    """Advance-only fake wall clock; nothing here ever sleeps for real."""

    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = float(t)

    def __call__(self) -> float:
        return self.t

    def advance(self, secs: float) -> None:
        self.t += float(secs)


def _probes(port_listening=False, process_alive=True):
    """Injected diagnostic probes with fixed, known observations."""
    return {
        "probe_port": lambda *a, **k: port_listening,
        "probe_process": lambda: process_alive,
    }


def _kwargs(tmp_path, mailer, clock, ensure, **extra):
    kw = dict(
        mailer=mailer,
        ensure_gateway=ensure,
        clock=clock,
        lock_path=tmp_path / "state" / ga.ALERT_LOCK_NAME,
        log=lambda *_a, **_k: None,
        **_probes(),
    )
    kw.update(extra)
    return kw


def _count(mailer, needle):
    return sum(1 for s in mailer.subjects if needle in s)


# --------------------------------------------------------------------------- #
# Happy path: one down email, then a back-up follow-up
# --------------------------------------------------------------------------- #

def test_down_then_back_up_sends_exactly_one_down_and_one_backup(tmp_path):
    m, clock = FakeMailer(), FakeClock()

    def ensure():
        clock.advance(42.0)      # the relaunch "took" 42s of fake time
        return True

    res = ga.handle_gateway_down(
        "s8_service", error=ConnectionResetError("connection lost"),
        last_connect_ok_ts=clock() - 30.0,
        **_kwargs(tmp_path, m, clock, ensure))

    assert res["alerted"] is True
    assert res["deduped"] is False
    assert res["relaunched"] is True
    assert res["seconds_down"] == pytest.approx(42.0)

    assert _count(m, "GATEWAY DOWN - relaunching, approve the 2FA") == 1
    assert _count(m, "gateway back up after 42s") == 1
    assert _count(m, "RELAUNCH FAILED") == 0
    assert len(m.sent) == 2

    down_body = m.sent[0][1]
    # The email must authorise the 2FA push it precedes...
    assert "2FA" in down_body
    assert "initiated by the desk" in down_body.lower()
    # ...and must carry the INVERSE rule that makes absence the security signal.
    assert "DO NOT APPROVE IT" in down_body
    # ...and must refuse to guess a cause.
    assert "NOT determinable" in down_body


def test_every_email_carries_the_inverse_2fa_warning(tmp_path):
    m, clock = FakeMailer(), FakeClock()
    ga.handle_gateway_down("s8_service",
                           **_kwargs(tmp_path, m, clock, lambda: True))
    assert len(m.sent) == 2
    for _subject, body in m.sent:
        assert "DO NOT APPROVE IT" in body


# --------------------------------------------------------------------------- #
# Failed relaunch: the failure gets its OWN email; no back-up email is sent
# --------------------------------------------------------------------------- #

def test_failed_relaunch_sends_failed_alert_and_no_backup(tmp_path):
    m, clock = FakeMailer(), FakeClock()

    res = ga.handle_gateway_down("s8_collector",
                                 **_kwargs(tmp_path, m, clock, lambda: False))

    assert res["relaunched"] is False
    assert _count(m, "GATEWAY DOWN - relaunching") == 1
    assert _count(m, "GATEWAY RELAUNCH FAILED - still down") == 1
    assert _count(m, "back up") == 0
    # Silence must never mean fine: the failure is stated plainly.
    assert "RELAUNCH FAILED" in m.sent[1][1]
    assert "did NOT come back up" in m.sent[1][1]


def test_raising_ensure_gateway_is_reported_as_a_failed_relaunch(tmp_path):
    m, clock = FakeMailer(), FakeClock()

    def boom():
        raise RuntimeError("IBC bat missing")

    res = ga.handle_gateway_down("s8_service", **_kwargs(tmp_path, m, clock, boom))

    assert res["relaunched"] is False
    assert res["error"] is None            # swallowed, not propagated
    assert _count(m, "GATEWAY RELAUNCH FAILED - still down") == 1
    assert "IBC bat missing" in m.sent[1][1]


# --------------------------------------------------------------------------- #
# DEDUP — the service and the collector both detect ONE drop -> ONE email
# --------------------------------------------------------------------------- #

def test_dedup_two_processes_same_outage_send_exactly_one_down_email(tmp_path):
    clock = FakeClock()
    m_service, m_collector = FakeMailer(), FakeMailer()

    r1 = ga.handle_gateway_down("s8_service",
                                **_kwargs(tmp_path, m_service, clock, lambda: True))
    clock.advance(3.0)   # the collector notices the same drop 3s later
    r2 = ga.handle_gateway_down("s8_collector",
                                **_kwargs(tmp_path, m_collector, clock, lambda: True))

    assert r1["deduped"] is False and r1["alerted"] is True
    assert r2["deduped"] is True and r2["alerted"] is False

    total_down = _count(m_service, "GATEWAY DOWN") + _count(m_collector, "GATEWAY DOWN")
    assert total_down == 1
    assert m_collector.sent == []          # the second detector is entirely silent


def test_after_the_cooldown_a_genuinely_new_outage_alerts_again(tmp_path):
    clock = FakeClock()
    m = FakeMailer()

    ga.handle_gateway_down("s8_service", **_kwargs(tmp_path, m, clock, lambda: True))
    assert _count(m, "GATEWAY DOWN") == 1

    clock.advance(ga.ALERT_COOLDOWN_SECS + 1.0)   # a separate, later outage
    res = ga.handle_gateway_down("s8_service", **_kwargs(tmp_path, m, clock, lambda: True))

    assert res["deduped"] is False
    assert _count(m, "GATEWAY DOWN") == 2


def test_marker_within_cooldown_suppresses_even_a_different_source(tmp_path):
    """Recency, not event-id equality, is the dedup key — two processes cannot agree on an
    id for a drop they each noticed independently."""
    clock = FakeClock()
    path = tmp_path / "state" / ga.ALERT_LOCK_NAME

    assert ga.acquire_alert_marker("a", path=path, now=clock,
                                   log=lambda *_a: None) is True
    clock.advance(ga.ALERT_COOLDOWN_SECS - 1.0)
    assert ga.acquire_alert_marker("b", path=path, now=clock,
                                   log=lambda *_a: None) is False
    clock.advance(2.0)
    assert ga.acquire_alert_marker("c", path=path, now=clock,
                                   log=lambda *_a: None) is True


def test_marker_fails_open_when_the_state_dir_is_unusable(tmp_path):
    """A filesystem problem must produce an EXTRA email, never a missing one — silence is
    what tells Andrew a 2FA push is not ours."""
    blocker = tmp_path / "notadir"
    blocker.write_text("i am a file", encoding="utf-8")
    assert ga.acquire_alert_marker(
        "x", path=blocker / "state" / ga.ALERT_LOCK_NAME,
        now=FakeClock(), log=lambda *_a: None) is True


# --------------------------------------------------------------------------- #
# Best-effort: nothing here may ever propagate into the pilot loop
# --------------------------------------------------------------------------- #

def test_raising_mailer_is_swallowed_and_never_propagates(tmp_path):
    clock = FakeClock()
    m = FakeMailer(raises=True)

    res = ga.handle_gateway_down("s8_service", **_kwargs(tmp_path, m, clock, lambda: True))

    assert res["alerted"] is False       # the send failed...
    assert res["relaunched"] is True     # ...but the relaunch still happened
    assert res["error"] is None          # and nothing escaped


def test_a_broken_everything_still_returns_instead_of_raising(tmp_path):
    """Even with a raising mailer AND a raising ensure_gateway, the caller sees a dict."""
    clock = FakeClock()

    def boom():
        raise RuntimeError("nope")

    res = ga.handle_gateway_down("s8_collector",
                                 **_kwargs(tmp_path, FakeMailer(raises=True), clock, boom))
    assert isinstance(res, dict)
    assert res["relaunched"] is False


def test_individual_senders_never_raise_on_a_broken_mailer():
    m = FakeMailer(raises=True)
    log = lambda *_a, **_k: None  # noqa: E731
    diag = ga.capture_diagnostics(source="s8_service", **_probes())
    assert ga.send_gateway_down_alert(diag, True, mailer=m, log=log) is False
    assert ga.send_gateway_back_up_alert(diag, 10, mailer=m, log=log) is False
    assert ga.send_gateway_relaunch_failed_alert(diag, 10, mailer=m, log=log) is False


# --------------------------------------------------------------------------- #
# DIAGNOSTICS — observed facts only, no fabricated cause
# --------------------------------------------------------------------------- #

def test_diagnostics_contain_the_observed_fields():
    clock = FakeClock()
    exc = ConnectionResetError("WinError 10054 remote host closed")
    d = ga.capture_diagnostics(
        source="s8_service", error=exc, last_connect_ok_ts=clock() - 125.0,
        now=clock, probe_port=lambda *a, **k: False, probe_process=lambda: True)

    assert d["source"] == "s8_service"
    assert d["port"] == ga.LIVE_TRADE_PORT == 4003
    assert d["port_listening"] is False
    assert d["gateway_process_alive"] is True
    assert d["error_type"] == "ConnectionResetError"
    assert "10054" in d["error"]
    assert d["seconds_since_last_connect"] == pytest.approx(125.0)
    assert d["observed_at_ct"] and "20" in d["observed_at_ct"]


def test_diagnostics_never_fabricate_a_cause():
    d = ga.capture_diagnostics(source="s8_collector", **_probes())
    assert d["observations_only"] is True
    assert d["cause"] == "NOT DETERMINABLE FROM THIS MACHINE"
    # No inference fields of any kind.
    assert not any(k in d for k in ("likely_cause", "probable_cause", "reason", "diagnosis"))


def test_undeterminable_probes_are_reported_as_unknown_not_as_down():
    d = ga.capture_diagnostics(
        source="s8_service",
        probe_port=lambda *a, **k: None, probe_process=lambda: None)
    assert d["port_listening"] is None
    assert d["gateway_process_alive"] is None
    # Unknown is rendered as UNKNOWN, never collapsed into a confident "NO".
    lines = "\n".join(ga.format_diagnostics_lines(d))
    assert lines.count("UNKNOWN (could not be determined)") >= 2


def test_raising_probes_do_not_break_diagnostics():
    def boom(*_a, **_k):
        raise OSError("probe exploded")

    d = ga.capture_diagnostics(source="s8_service", probe_port=boom, probe_process=boom)
    assert d["port_listening"] is None
    assert d["gateway_process_alive"] is None


# --------------------------------------------------------------------------- #
# Zero-transmit: this module has no order path at all
# --------------------------------------------------------------------------- #

def test_module_has_no_order_path():
    import pathlib

    src = pathlib.Path(ga.__file__).read_text(encoding="utf-8")
    for forbidden in ("placeOrder", "bracketOrder", "order_router", "transmit = True"):
        assert forbidden not in src


# --------------------------------------------------------------------------- #
# SHARPENED GATEWAY PROCESS PROBE
#
# The old probe matched ANY javaw.exe/java.exe, so it could not tell THIS Gateway's JVM
# apart from the paper gateway's or the ThetaData terminal's. It now discriminates on the
# live-trade listening port / install dir. These tests feed the parser FAKE PowerShell
# output -- no PowerShell is spawned, no process is touched.
# --------------------------------------------------------------------------- #

class _Out:
    def __init__(self, stdout=""):
        self.stdout = stdout


def _nt(monkeypatch):
    monkeypatch.setattr(ga.os, "name", "nt")


def test_probe_identifies_the_live_trade_gateway(monkeypatch):
    _nt(monkeypatch)
    out = _Out('{"found":true,"pids":[4242]}')
    assert ga.gateway_process_alive(run=lambda *a, **k: out) is True


def test_probe_reports_false_when_the_scan_ran_and_found_nothing(monkeypatch):
    """An enumeration that genuinely succeeded and matched nothing is a real NO."""
    _nt(monkeypatch)
    out = _Out('{"found":false,"pids":[]}')
    assert ga.gateway_process_alive(run=lambda *a, **k: out) is False


def test_probe_does_not_misidentify_an_unrelated_java_process(monkeypatch):
    """The paper gateway / ThetaData terminal are java too. The PowerShell filters them
    out by port+install dir, so an 'unrelated java only' box reports found=false -- NOT
    the old blanket True."""
    _nt(monkeypatch)
    out = _Out('{"found":false,"pids":[]}')
    assert ga.gateway_process_alive(run=lambda *a, **k: out) is not True


def test_probe_scopes_its_query_to_this_instance(monkeypatch):
    """The command actually sent must carry BOTH discriminators: the live-trade port and
    the live-trade install dir."""
    _nt(monkeypatch)
    seen = {}

    def fake_run(cmd, **_k):
        seen["cmd"] = cmd
        return _Out('{"found":true,"pids":[1]}')

    ga.gateway_process_alive(run=fake_run)
    script = seen["cmd"][-1]
    assert str(ga.LIVE_TRADE_PORT) in script
    assert "IBC-Live-Trade" in script
    assert "4002" not in script            # never the PAPER gateway's port
    assert "25503" not in script           # never the ThetaData terminal's port


def test_probe_returns_none_not_false_when_it_cannot_determine(monkeypatch):
    """UNKNOWN must never collapse into a confident 'NO' -- that is the whole point."""
    _nt(monkeypatch)
    for stdout in ("PROBE_FAILED", "", "   ", "not json at all", '"a string"', "[1,2]"):
        assert ga.gateway_process_alive(run=lambda *a, **k: _Out(stdout)) is None

    def boom(*_a, **_k):
        raise OSError("powershell missing")

    assert ga.gateway_process_alive(run=boom) is None


def test_probe_returns_none_off_windows(monkeypatch):
    monkeypatch.setattr(ga.os, "name", "posix")
    assert ga.gateway_process_alive() is None


def test_unknown_probe_renders_as_unknown_in_the_email():
    lines = ga.format_diagnostics_lines({"gateway_process_alive": None, "port": 4003})
    assert any("UNKNOWN (could not be determined)" in ln for ln in lines)
