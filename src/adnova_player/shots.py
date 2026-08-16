"""
Screenshot uploader — the eyes on a stand nobody can drive to.

Runs inside the desktop session (grim needs the Wayland display) and,
deliberately, independently of the player service. The player is hardened
with ProtectSystem=strict, so it cannot even write a trigger file to /tmp —
and more importantly, if the player has crashed, an operator in another city
still needs to see what the screen shows (the desktop, a boot message, an
error) before deciding whether to reach for SSH. So the capture lives here,
outside the player, on its own.

It runs under the player's venv, so it signs and uploads with exactly the
same code as every other call (adnova_player.api / .signing) and reads the
stand's credentials from the same env file the service uses. Best-effort and
quiet: a failed grab or a down network is retried on the next tick, never a
crash. It grabs on a slow interval so a distant operator always has a recent
frame without the device streaming video off a shop's uplink.
"""
from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path

from .api import DashboardApi
from .config import load as load_config

log = logging.getLogger("adnova.shots")

ENV_FILE = os.environ.get("ADNOVA_ENV_FILE", "/etc/adnova-player/env")
INTERVAL = int(os.environ.get("ADNOVA_SCREENSHOT_SECONDS", "30"))


def _read_env(path: str) -> dict[str, str]:
    """Parse the stand's env file (KEY=value lines) into a dict."""
    env: dict[str, str] = {}
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip('"').strip("'")
    except OSError as exc:
        log.error("Cannot read %s: %s", path, exc)
    return env


def _grab() -> bytes | None:
    """
    A half-resolution PNG of the whole screen, or None if the grab failed.

    PNG because the Pi's grim is built without JPEG support; -s 0.5 keeps the
    file to a few hundred KB. grim captures the compositor's output, so it
    sees whatever is really on the panel — mpv, the desktop, a boot screen.
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp.close()
    try:
        result = subprocess.run(  # noqa: S603 — fixed argv
            ["grim", "-s", "0.5", tmp.name],
            capture_output=True,
            timeout=15,
        )
        if result.returncode != 0:
            return None
        data = Path(tmp.name).read_bytes()
        return data or None
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def main() -> None:
    env = {**os.environ, **_read_env(ENV_FILE)}
    config = load_config(env)
    api = DashboardApi(config)
    log.info(
        "Screenshot uploader started for stand %s (every %ss).",
        config.stand_id,
        INTERVAL,
    )
    while True:
        png = _grab()
        if png is not None:
            api.send_screenshot(png)
        else:
            log.warning("No screenshot this tick (grim unavailable or no display).")
        time.sleep(max(10, INTERVAL))


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )
    main()
