"""
Proof of play, as a picture.

A playback log says an ad was shown; a screenshot shows it *was on the
screen*. For an advertiser paying for a physical window, that image is
the difference between a number in a report and evidence. Every few
minutes the player grabs a frame and uploads a small JPEG thumbnail, tied
to the slot that was playing when it was taken.

Capturing a framebuffer is entirely hardware-specific, so the mechanism
is tried in order — the Wayland grabber, then the KMS/framebuffer tools —
and a device where none work simply sends no screenshots. That is a
missing nicety, never a fault: proof-of-play is evidence on top of the
playback log, not a substitute for it, and the screen keeps playing
either way.

Everything here is best-effort and off the hot path. It never blocks
playback, never raises into a loop, and downscales hard so a thumbnail is
a few kilobytes, not a megabyte off a shop's uplink every few minutes.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger("adnova.capture")

# The grabbers, in order of preference. Each writes a PNG/JPEG to the path
# given as its last argument; the first that exists and succeeds wins.
_GRABBERS: list[list[str]] = [
    ["grim"],              # Wayland (labwc/wayfire, the Pi's current default)
    ["wayshot", "-f"],     # Wayland alternative
    ["fbgrab"],            # raw framebuffer
    ["scrot", "-o"],       # X11
]

Runner = Callable[[list[str]], bool]


def _run(argv: list[str]) -> bool:
    binary = shutil.which(argv[0])
    if binary is None:
        return False
    try:
        result = subprocess.run(  # noqa: S603 — fixed binaries, path as last arg
            [binary, *argv[1:]],
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


class ScreenCapture:
    """
    Grabs and downscales a frame. Returns JPEG bytes, or None if this
    hardware has no working grabber — in which case the caller sends
    nothing and moves on.
    """

    def __init__(self, runner: Runner | None = None, max_edge: int = 480) -> None:
        self._run = runner or _run
        self._max_edge = max_edge
        self._grabber: list[str] | None = None

    def available(self) -> bool:
        return self._pick() is not None

    def grab(self) -> bytes | None:
        grabber = self._pick()
        if grabber is None:
            return None

        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "frame.png"
            if not self._run([*grabber, str(raw)]) or not raw.exists():
                return None
            return self._downscale(raw)

    # ── internals ────────────────────────────────────────────────────────

    def _pick(self) -> list[str] | None:
        if self._grabber is not None:
            return self._grabber
        for candidate in _GRABBERS:
            if shutil.which(candidate[0]):
                self._grabber = candidate
                return candidate
        return None

    def _downscale(self, source: Path) -> bytes | None:
        """
        Shrink to a thumbnail with Pillow if present, else send the raw
        grab. The thumbnail keeps the uplink cost trivial; the fallback
        keeps proof-of-play working on a minimal image.
        """
        try:
            from PIL import Image
        except ImportError:
            return source.read_bytes()

        try:
            with Image.open(source) as img:
                img = img.convert("RGB")
                img.thumbnail((self._max_edge, self._max_edge))
                out = source.with_suffix(".jpg")
                img.save(out, "JPEG", quality=60)
                return out.read_bytes()
        except Exception:  # noqa: BLE001 — a bad grab must not raise into the loop
            log.warning("Could not downscale a screenshot; sending nothing this cycle.")
            return None
