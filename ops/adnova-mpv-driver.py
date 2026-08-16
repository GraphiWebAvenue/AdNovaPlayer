#!/usr/bin/env python3
"""
The display, driven by mpv instead of a browser.

Why this exists: Chromium on a Pi 4 has no working hardware H.264 path, so it
software-decodes every frame — one core pinned, the screen a slideshow, the
board hot. mpv uses the Pi's own V4L2 decoder (`--hwdec`), which drops a
1080p clip to a few percent of one core, smooth, cool.

This runs inside the desktop session (the same place the browser used to),
because only there is the Wayland display reachable. It keeps the player's
core untouched: the player still decides what plays and exposes it at
`/state`, exactly as the browser read it. This process is just a new pair of
eyes on that endpoint — it polls `/state` and tells mpv, over mpv's JSON IPC
socket, to show whatever the player named. No schedule logic lives here.

Everything is best-effort and self-healing: if mpv dies it is relaunched, if
the player is briefly unreachable the last frame stays, and a gap or the
built-in fallback is a black screen, never an error. Standard library only,
so it runs under the system python with no venv.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import urllib.request

PLAYER = os.environ.get("ADNOVA_KIOSK_URL", "http://127.0.0.1:8080").rstrip("/")
STATE_URL = f"{PLAYER}/state"
IPC_SOCK = "/tmp/adnova-mpv.sock"

# mpv, told to be a silent, controllable, full-screen surface and nothing
# else. --hwdec=auto is the whole point: it offloads decode to the board.
# --vo=gpu keeps output going through the Wayland compositor (so a grim
# screenshot still captures it), while decode stays on the hardware.
# A deliberately conservative, well-known flag set: a single bad flag makes
# mpv refuse to start, which on a signage box is a black screen. Only the two
# that matter for performance are non-obvious:
#
#   --hwdec=v4l2m2m-copy  the whole point. Measured on a real Pi 4: a 720p24
#       clip that pinned a core at ~90% under Chromium (and under mpv's own
#       cautious --hwdec=auto, which skips V4L2) dropped to ~8% of one core
#       and played perfectly with this explicit decoder. `-copy` reads the
#       decoded frames back to system memory, which any VO can display — a
#       touch more CPU than zero-copy but reliable, where zero-copy needs the
#       VO to import the decoder's buffers. h264_v4l2m2m is present in mpv's
#       `--hwdec=help` on this image, so this is a real hardware path.
#   --vo=gpu  composited GL output, so a grim screenshot still captures the
#       frame (a direct-to-KMS VO would show black in a grab).
#
# Everything else just makes mpv a silent, idle-capable, full-screen surface
# with no keyboard or OSD.
MPV_ARGS = [
    "mpv",
    "--idle=yes",                     # stay alive with nothing loaded
    "--force-window=yes",             # show a (black) window even when idle
    "--fullscreen",
    "--no-osc",
    "--osd-level=0",
    "--no-terminal",
    "--really-quiet",
    "--no-input-default-bindings",
    "--input-conf=/dev/null",
    "--cursor-autohide=always",
    "--hwdec=v4l2m2m-copy",
    "--vo=gpu",
    "--image-display-duration=inf",   # an image stays until the player moves on
    # Loop every clip. Without this a short ad plays once and freezes on its
    # last frame (--keep-open), which reads as "the video stopped". The player
    # still advances by changing /state — a new key triggers a fresh loadfile —
    # so looping only fills the time until then, never overrides the schedule.
    "--loop-file=inf",
    "--keep-open=yes",                # last resort: hold a frame, never go black
    "--audio-fallback-to-null=yes",   # no audio sink must never stop playback
    # A log we can read when something misbehaves on the device; --really-quiet
    # keeps it off the screen but the file still gets the full detail.
    "--log-file=/tmp/adnova-mpv.log",
    f"--input-ipc-server={IPC_SOCK}",
]


def launch_mpv() -> subprocess.Popen:
    # A throwaway socket from a previous run would make mpv refuse to bind.
    try:
        os.unlink(IPC_SOCK)
    except OSError:
        pass
    return subprocess.Popen(MPV_ARGS)  # noqa: S603 — fixed argv


def mpv_send(command: list) -> None:
    """One JSON command over the IPC socket. Silent on any failure."""
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2)
        sock.connect(IPC_SOCK)
        sock.sendall((json.dumps({"command": command}) + "\n").encode())
        sock.close()
    except OSError:
        pass


def get_state() -> dict | None:
    try:
        with urllib.request.urlopen(STATE_URL, timeout=3) as resp:  # noqa: S310 — localhost
            return json.loads(resp.read())
    except Exception:  # noqa: BLE001 — any failure just means "keep the last frame"
        return None


def key_of(state: dict | None) -> str | None:
    if not state:
        return None
    return f"{state.get('slot_id')}:{state.get('src')}:{state.get('schedule_version')}"


def show(state: dict) -> None:
    """Put the player's chosen item on mpv."""
    src = state.get("src")
    # The built-in fallback is a dark filler, not a file. Stop playback so the
    # forced window shows its black background — never an error, never a flash.
    if not src or src == "/fallback":
        mpv_send(["stop"])
        return

    url = f"{PLAYER}{src}"
    mpv_send(["loadfile", url, "replace"])
    # Looping is global (--loop-file=inf); only audio is per-item. An image
    # ignores loop and rests on --image-display-duration=inf.
    mpv_send(["set_property", "mute", "yes" if state.get("muted") else "no"])


def wait_for_socket(deadline: float) -> bool:
    while time.monotonic() < deadline:
        if os.path.exists(IPC_SOCK):
            return True
        time.sleep(0.1)
    return False


def main() -> None:
    mpv = launch_mpv()
    wait_for_socket(time.monotonic() + 8)
    current: str | None = None

    while True:
        # mpv is the one thing that must never stay dead: relaunch it and
        # force the next state to re-apply so the screen fills again.
        if mpv.poll() is not None:
            mpv = launch_mpv()
            wait_for_socket(time.monotonic() + 8)
            current = None

        state = get_state()
        if state is None:
            time.sleep(2)
            continue

        key = key_of(state)
        if key != current:
            current = key
            show(state)

        time.sleep(1)


if __name__ == "__main__":
    main()
