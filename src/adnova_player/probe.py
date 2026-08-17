"""
Decode-probe for cached media.

A file can pass its SHA-256 check (its bytes are exactly what the signed
manifest promised) and still be undecodable on this hardware: a codec
mpv can't handle, a container the panel's decoder chokes on, or a source
that was already truncated when it was uploaded. The checksum proves
authenticity, not playability.

So before a slot's media becomes eligible for the screen, we decode-probe
it once, in the background. A file that fails the probe is then treated
exactly like a cache miss — the fallback covers its slot and Dashboard is
told — instead of the paid slot showing a black or garbled frame.

Fail-open on the *tool*: if ffprobe is not installed, or gives an
inconclusive result (timeout, unreadable output), we do NOT quarantine.
Never let a missing dev tool blank a whole fleet. We quarantine only on a
definitive "this does not decode" verdict (ffprobe error exit, or a file
with no decodable streams at all).
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger("adnova.probe")

# A still or a short clip probes in well under a second; a generous cap
# keeps a pathological file from stalling the fetch loop.
PROBE_TIMEOUT_SECONDS = 20


def _run_ffprobe(path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration:stream=codec_type",
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
        timeout=PROBE_TIMEOUT_SECONDS,
        check=False,
    )


def probe_media(
    path: Path,
    runner: Callable[[Path], subprocess.CompletedProcess] | None = None,
) -> tuple[bool, str]:
    """
    Return ``(decodable, detail)`` for the file at ``path``.

    ``runner`` is injectable so tests never need a real ffprobe; by
    default it shells out. Decodable = the probe tool is absent/
    inconclusive (fail-open), OR it exited cleanly and found at least one
    stream. Not decodable = a clean "no" from the tool.
    """
    use_default = runner is None
    runner = runner or _run_ffprobe

    if use_default and shutil.which("ffprobe") is None:
        return True, "ffprobe unavailable; skipped"

    try:
        proc = runner(path)
    except (OSError, subprocess.SubprocessError) as exc:
        # Inconclusive (timeout, spawn failure) → fail open, but note it.
        return True, f"probe inconclusive: {exc}"

    if proc.returncode != 0:
        detail = (proc.stderr or "ffprobe reported errors").strip()
        return False, detail[:200]

    try:
        data = json.loads(proc.stdout or "{}")
    except ValueError:
        return True, "probe output unparseable; skipped"

    if not (data.get("streams") or []):
        return False, "no decodable streams"

    return True, ""
