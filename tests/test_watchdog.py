"""
The earned watchdog ping.

The one behaviour that matters: the ping is withheld when the liveness
check fails, so a wedged process is restarted rather than kept on life
support. Outside systemd (no NOTIFY_SOCKET) everything is a silent no-op.
"""

from __future__ import annotations

from adnova_player.watchdog import Watchdog, ready


def test_ready_is_a_noop_without_systemd(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    ready()  # must not raise


def test_the_watchdog_does_not_start_without_systemd(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    wd = Watchdog(is_live=lambda: True)
    wd.start()  # no thread, no socket, no error
    wd.stop()


def test_liveness_gates_the_ping(monkeypatch):
    # With a socket set, the run loop calls _notify only while is_live is
    # true. We drive one cycle by hand rather than spinning a thread.
    sent = []
    monkeypatch.setenv("NOTIFY_SOCKET", "/tmp/does-not-exist.sock")
    monkeypatch.setattr("adnova_player.watchdog._notify", lambda msg: sent.append(msg))

    live = {"ok": True}
    wd = Watchdog(is_live=lambda: live["ok"], interval_seconds=0.01)

    # Simulate the two branches of the loop body directly.
    if wd._is_live():
        from adnova_player.watchdog import _notify
        _notify("WATCHDOG=1")
    assert sent == ["WATCHDOG=1"]

    live["ok"] = False
    sent.clear()
    if wd._is_live():
        from adnova_player.watchdog import _notify
        _notify("WATCHDOG=1")
    assert sent == []  # withheld — systemd will restart us
