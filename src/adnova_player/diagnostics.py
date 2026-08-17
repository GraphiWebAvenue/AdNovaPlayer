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


def redacted_bundle(
    *,
    stand_id: int | None,
    player_version: str,
    checks: list[Check],
    health: dict,
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
    }
