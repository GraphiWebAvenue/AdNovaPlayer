"""
Boot self-test and the operator's diagnostics bundle.

A Pi sits behind a shop's NAT with no inbound access, so the only way to
tell why one is misbehaving is to have the device look at itself and report
home. Two things live here:

**A boot self-test** — a handful of cheap checks run once at start-up
(config, writable card, the decoder and player binaries, signing keys). It
never blocks the player from starting; a signage box shows *something*
before it complains. Its results ride the heartbeat so an operator sees a
stand that came up degraded — no mpv, a read-only card — without a visit.

**A diagnostics bundle** — the same checks plus a health snapshot and
version, gathered on request into one redacted dict. It is built to be
*safe to send*: the stand key and signing keys never appear in it, only
whether they are present. That is the whole point of assembling it here
rather than shipping raw config or logs that might carry a secret.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class Check:
    """One self-test result: a name, whether it passed, and a human line."""

    name: str
    ok: bool
    # "critical" means the device is degraded (no decoder, no player); "warn"
    # means something to watch (no signing keys yet) that still runs fine.
    severity: str
    detail: str


def _writable(path: Path) -> bool:
    """A real write-and-delete probe — see health._storage_writable for why."""
    if not path.exists():
        return False
    probe = path / ".adnova-selftest"
    try:
        probe.write_bytes(b"ok")
        probe.unlink()
        return True
    except OSError:
        return False


def run_self_test(
    *,
    stand_id: int | None,
    cache_dir: Path,
    has_trusted_keys: bool,
    which: Callable[[str], str | None] = shutil.which,
    writable: Callable[[Path], bool] = _writable,
) -> list[Check]:
    """
    Run the boot checks and return them, most-severe first.

    `which` and `writable` are injected so the test suite can exercise every
    branch — a missing mpv, a read-only card — without needing that hardware
    state to actually exist on the box running the tests.
    """
    checks = [
        Check(
            "identity", stand_id is not None, "critical",
            f"stand id {stand_id}" if stand_id is not None else "no stand id",
        ),
        Check(
            "storage", writable(cache_dir), "critical",
            "state directory is writable" if writable(cache_dir)
            else "state directory is read-only or missing",
        ),
        Check(
            "player", which("mpv") is not None, "critical",
            "mpv present" if which("mpv") else "mpv not found — the screen cannot play",
        ),
        Check(
            "decoder", which("ffprobe") is not None, "warn",
            "ffprobe present" if which("ffprobe")
            else "ffprobe not found — media is played without a decode pre-check",
        ),
        Check(
            "signing", has_trusted_keys, "warn",
            "signing keys provisioned" if has_trusted_keys
            else "no signing keys — manifests are trusted without a signature",
        ),
    ]
    # Failures first (criticals before warnings), so the log and the bundle
    # lead with what actually needs attention.
    order = {"critical": 0, "warn": 1}
    return sorted(checks, key=lambda c: (c.ok, order.get(c.severity, 9)))


def failures(checks: list[Check]) -> list[str]:
    """The names of the checks that did not pass, for a compact heartbeat field."""
    return [c.name for c in checks if not c.ok]


# The in-session processes that together put pixels on the panel. Named here
# because "which of these is missing" is the single most useful fact about a
# misbehaving stand, and the operator has no other way to learn it: the player
# service can be perfectly healthy while every one of these is gone.
_SESSION_PROCESSES = {
    "mpv_driver": "adnova-mpv-driver",
    "kiosk_launcher": r"adnova-kiosk\.sh",
    "kiosk_helper": "adnova-kiosk-helper",
    "screenshot_uploader": "adnova_player.shots",
}

# The launcher writes one log per uid — a fixed name would let whichever
# account got there first lock the others out of their own file, which is
# exactly the trap that once held a stand's screen dark. So this is a glob,
# and the newest match is the session that last tried to start the display.
KIOSK_LOG_GLOB = "/tmp/adnova-kiosk-launch.*.log"


def session_processes(runner: Callable[[list[str]], bool] | None = None) -> dict[str, bool]:
    """
    Which parts of the display stack are actually running.

    Uses pgrep against a closed map of patterns — nothing from the network
    reaches this. `runner` is injected so the tests can exercise both answers
    without needing the real processes to exist.
    """
    import subprocess

    def _default(argv: list[str]) -> bool:
        try:
            return subprocess.run(  # noqa: S603 — fixed argv from a closed map
                argv, capture_output=True, timeout=5
            ).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    run = runner or _default

    def _safely(pattern: str) -> bool:
        # An unanswerable question is answered "not running", never raised.
        # This bundle is assembled precisely when a stand is already
        # misbehaving; it must not be the thing that takes the loop down.
        try:
            return bool(run(["pgrep", "-f", pattern]))
        except Exception:  # noqa: BLE001 — see above
            return False

    return {name: _safely(pattern) for name, pattern in _SESSION_PROCESSES.items()}


def kiosk_log_tail(pattern: str = KIOSK_LOG_GLOB, lines: int = 40) -> list[str]:
    """
    The last few lines the display launcher wrote, or [].

    The launcher runs in the desktop session and writes to /tmp; the player's
    hardened view of the filesystem is read-only, not private, so it can read
    this even though it could never write it. This is where "no HDMI audio
    sink found" or "could not open the lock" shows up — the difference between
    a stand that is dark for a knowable reason and one that is dark for no
    reason anybody can see.

    The most recently written file wins. There is one per uid, and the
    interesting one is whichever session last tried to bring the display up.
    """
    import glob

    try:
        candidates = glob.glob(pattern)
    except OSError:
        return []
    if not candidates:
        return []

    try:
        newest = max(candidates, key=lambda p: os.stat(p).st_mtime)
    except OSError:
        return []

    try:
        with open(newest, encoding="utf-8", errors="replace") as f:
            # Bounded read: the launcher caps each file at 1 MB, and a
            # diagnostics bundle is not the place to ship all of it.
            return [line.rstrip("\n") for line in f.readlines()[-lines:]]
    except OSError:
        return []


def redacted_bundle(
    *,
    stand_id: int | None,
    player_version: str,
    checks: list[Check],
    health: dict,
    session: dict[str, bool] | None = None,
    kiosk_log: list[str] | None = None,
    display: dict | None = None,
) -> dict:
    """
    Assemble the operator's diagnostics bundle — safe to send by construction.

    Only presence-of-secret booleans ever leave the device, never a key. The
    caller passes an already-shaped health dict; this frames it with identity,
    version, and the self-test so Dashboard can show one full picture of a
    stand it cannot reach.
    """
    return {
        "stand_id": stand_id,
        "player_version": player_version,
        "self_test": [asdict(c) for c in checks],
        "self_test_failures": failures(checks),
        "health": health,
        # The three things that separate "the brain is fine, the screen is
        # dead" from every other fault. Absent (not empty) when the caller
        # did not gather them, so an older bundle stays readable.
        **({"session": session} if session is not None else {}),
        **({"kiosk_log": kiosk_log} if kiosk_log is not None else {}),
        **({"display": display} if display is not None else {}),
    }
