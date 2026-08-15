"""
Turning the display off when nobody is looking.

A shop is shut for two-thirds of the day. Leaving the panel lit through
the night burns its backlight hours and the shop's electricity for an
audience of nobody, so the player powers the display down outside the
stand's operating hours and back up before it opens.

The mechanism is whatever the hardware offers, tried in order: HDMI-CEC
to tell the TV itself to sleep (the kind thing — a real TV standby, not
just a black HDMI signal), then the Pi's own display-power control as a
fallback. A device that supports neither simply keeps the screen on,
which is safe: a lit screen is never wrong, only wasteful.

All of it is best-effort. A failed power command is logged and shrugged
off — the one outcome that must never happen is the control code throwing
and taking the player down, turning a power-saving nicety into a dark
shop window during business hours.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from collections.abc import Callable

log = logging.getLogger("adnova.screen")

# A command runner, injectable so tests never shell out. Returns True on
# success. The default runs the real command with a short timeout.
Runner = Callable[[list[str]], bool]


def _run(argv: list[str]) -> bool:
    binary = shutil.which(argv[0])
    if binary is None:
        return False
    try:
        result = subprocess.run(  # noqa: S603 — fixed binaries, fixed args
            [binary, *argv[1:]],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("Screen command %s failed: %s", argv[0], exc)
        return False
    return result.returncode == 0


class Screen:
    """
    Display power, with the current state remembered so a no-op is free.

    The player asks for on() or off() every cycle; this only issues a
    command when the state actually changes, so the CEC bus is not spammed
    once a minute.
    """

    def __init__(self, runner: Runner | None = None) -> None:
        self._run = runner or _run
        self._on: bool | None = None  # unknown until first set

    def on(self) -> None:
        if self._on is True:
            return
        if self._power(True):
            self._on = True
            log.info("Display on.")

    def off(self) -> None:
        if self._on is False:
            return
        if self._power(False):
            self._on = False
            log.info("Display off (outside operating hours).")

    def apply(self, should_be_on: bool) -> None:
        """Convenience for the loop: one call, driven by the schedule."""
        self.on() if should_be_on else self.off()

    def _power(self, on: bool) -> bool:
        """
        Try CEC first, then the Pi's display control. Success if either
        works; a device with neither keeps the screen on.
        """
        # HDMI-CEC: ask the TV to wake or sleep. `on 0` / `standby 0`
        # target the TV at logical address 0.
        cec = "on 0" if on else "standby 0"
        if self._run(["cec-client", "-s", "-d", "1"]) and self._run(
            ["sh", "-c", f"echo '{cec}' | cec-client -s -d 1"]
        ):
            return True

        # Wayland/DRM power via wlr-randr, then the legacy vcgencmd path.
        state = "--on" if on else "--off"
        if self._run(["wlr-randr", "--output", "HDMI-A-1", state]):
            return True

        blank = "0" if on else "1"
        return self._run(["vcgencmd", "display_power", blank])
