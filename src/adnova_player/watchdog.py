"""
Telling systemd we are still alive — but only when we truly are.

systemd's software watchdog restarts the service if it stops hearing from
it. The trap is a watchdog that pings unconditionally: it keeps a wedged
process on life support, defeating the whole point. So the ping here is
*earned*. The agent stamps a heartbeat each time its loops make progress,
and this only notifies systemd while those stamps are fresh. A deadlocked
fetch thread, a hung server — the stamps go stale, the pings stop, and
systemd restarts us into a working state.

Under this sits the hardware watchdog (/dev/watchdog), which the installer
points at systemd itself. If systemd wedges too, the Pi's silicon reboots
the board. The screen recovers no matter which layer failed.

Speaks the sd_notify protocol directly over NOTIFY_SOCKET, so there is no
dependency to ship to the fleet. Off a real systemd — in a test, or on a
desktop — every call is a silent no-op.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from collections.abc import Callable

from . import event_log

log = logging.getLogger("adnova.watchdog")


def _notify(message: str) -> bool:
    """
    Send one sd_notify datagram. Returns whether systemd was even there.

    NOTIFY_SOCKET is set by systemd only when the unit opted into the
    protocol. Absent, this is a no-op — the player runs fine by hand.
    """
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False

    # A leading '@' means an abstract socket; systemd encodes that as a
    # NUL first byte.
    path = "\0" + addr[1:] if addr.startswith("@") else addr
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(path)
            sock.sendall(message.encode("utf-8"))
        return True
    except OSError as exc:
        log.warning("sd_notify failed: %s", exc)
        return False


def ready() -> None:
    """Tell systemd the service is up. Sent once the loops are running."""
    _notify("READY=1\nSTATUS=Playing")


class Watchdog:
    """
    Pings systemd's watchdog while a liveness check keeps returning true.

    `is_live` is the agent's own judgement — its loops ticked recently. If
    it goes false, the pings stop and systemd takes over. The interval is
    half of WatchdogSec so a single missed cycle is not fatal.
    """

    def __init__(self, is_live: Callable[[], bool], interval_seconds: float = 25.0) -> None:
        self._is_live = is_live
        self._interval = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Edge-triggered: a stall lasts until the restart, and one event is
        # the record of it. Repeating it every 25s would bury the cause.
        self._stalled = False

    def start(self) -> None:
        if not os.environ.get("NOTIFY_SOCKET"):
            # Not under systemd — nothing to feed. Running by hand is fine.
            return
        self._thread = threading.Thread(target=self._run, name="watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            if self._is_live():
                _notify("WATCHDOG=1")
            elif not self._stalled:
                # Deliberately silent to systemd: let the ping lapse so it
                # restarts us. Loud everywhere else, and only on the edge —
                # the restart is seconds away, so the record has one chance
                # to be written and shipped before the process is replaced.
                self._stalled = True
                log.error("Liveness check failed; withholding the watchdog ping.")
                event_log.record(
                    "watchdog.stalled", "error",
                    "The agent's loops stopped making progress; letting systemd restart us.",
                )


def monotonic() -> float:
    return time.monotonic()
