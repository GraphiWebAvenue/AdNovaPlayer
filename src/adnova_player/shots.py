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
# How often to ask Dashboard whether a screenshot is wanted while none is —
# a tiny JSON poll, so this can be brisk without costing bandwidth. Captures
# only happen when the operator asks (mode "once") or has a live view open.
IDLE_POLL_SECONDS = int(os.environ.get("ADNOVA_SHOT_POLL_SECONDS", "8"))


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
    # Keep trying to load the config: right after a reboot the group
    # membership that lets us read the env file may not be in effect yet, and
    # a distant stand must recover on its own without a crash-loop.
    while True:
        try:
            config = load_config({**os.environ, **_read_env(ENV_FILE)})
            break
        except Exception as exc:  # noqa: BLE001 — any config error just means "not ready"
            log.error("Cannot load config yet (%s); retrying in 15s.", exc)
            time.sleep(15)

    api = DashboardApi(config)
    log.info("Screenshot uploader started for stand %s.", config.stand_id)

    # Poll the operator's intent on a light cadence and grab only when it is
    # actually wanted: a single shot on request, or a frame every few seconds
    # while a live view is open — nothing at all the rest of the time, so an
    # idle stand in another city uses no bandwidth on pictures no one wants.
    while True:
        policy = api.get_screenshot_policy() or {}
        mode = policy.get("mode", "idle")

        if mode in ("once", "live"):
            png = _grab()
            if png is not None:
                api.send_screenshot(png)
            else:
                log.warning("Grab failed (grim unavailable or no display).")

        if mode == "live":
            time.sleep(max(3, int(policy.get("interval", 10))))
        elif mode == "once":
            time.sleep(3)   # one shot done; look again soon in case live opens
        else:
            time.sleep(IDLE_POLL_SECONDS)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )
    main()
