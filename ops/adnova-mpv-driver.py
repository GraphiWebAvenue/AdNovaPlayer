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
# Where the driver leaves a snapshot of what is REALLY on the panel — the file
# the player folds into its heartbeat so Dashboard reports verified playback
# (the clock is advancing, this src is up) rather than only what was intended.
DISPLAY_STATE = os.environ.get("ADNOVA_DISPLAY_STATE", "/var/lib/adnova-player/display.json")

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
    # Force OpenGL, not Vulkan. On a Pi 4 driving a 4K panel, mpv's default
    # Vulkan backend fails to allocate the 3840x2160 swapchain
    # (VK_ERROR_OUT_OF_HOST_MEMORY, repeated) and the picture freezes. The
    # VideoCore's OpenGL ES path handles 4K fine. Wayland context to match the
    # compositor (and keep the frame composited, so grim still captures it).
    "--gpu-api=opengl",
    "--gpu-context=wayland",
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


class FreezeDetector:
    """
    Decide when the picture has frozen and mpv must be nudged back to life.

    mpv can sit on a single decoded frame with its clock stopped — a stalled
    V4L2 decoder, a half-opened file, a lost Wayland surface — and from the
    outside the shop screen just holds one image forever while the schedule
    rolls on underneath it. Nobody is standing there to notice.

    This reads no pixels; it watches mpv's own playback clock. A video that
    should be advancing must keep moving `time-pos`. If the clock stops for
    longer than `freeze_seconds` while mpv is neither paused nor idle, the
    picture has frozen and the caller is told to recover. A looping clip is
    fine — its clock wraps to zero each loop, which still counts as movement.

    Stills are exempt: an image legitimately never advances, so only items the
    player marked as a video are watched, and the black fallback never is.
    """

    def __init__(self, freeze_seconds: float = 12.0) -> None:
        self._freeze_seconds = freeze_seconds
        self._last_pos: float | None = None
        self._since: float | None = None

    def reset(self) -> None:
        """Forget history — called on every genuine item change or recovery."""
        self._last_pos = None
        self._since = None

    def observe(
        self, now: float, is_video: bool, playing: bool, time_pos: float | None
    ) -> bool:
        """
        Feed one reading; return True when a recovery is warranted.

        now        monotonic seconds
        is_video   the current item is a video (an image cannot freeze)
        playing    mpv holds a real file and is not paused/idle
        time_pos   mpv's playback clock, or None when it could not be read
        """
        if not (is_video and playing) or time_pos is None:
            # Nothing that can freeze, or no clock to judge by. Start clean so
            # a later stall is timed from now, not from stale history.
            self.reset()
            return False

        if self._last_pos is None or abs(time_pos - self._last_pos) > 0.05:
            # First reading, or the clock moved (including a loop wrap): healthy.
            self._last_pos = time_pos
            self._since = now
            return False

        # The clock has not moved since we first saw this position.
        if self._since is not None and (now - self._since) >= self._freeze_seconds:
            self.reset()
            return True
        return False


def display_record(
    state: dict | None, time_pos: float | None, playing: bool, freezes: int
) -> dict:
    """
    The health snapshot the player folds into its heartbeat.

    A pure shaping of what the driver knows about the panel right now: the src
    it last applied, whether mpv's clock is advancing, and how many freezes it
    has had to recover from. Dashboard reads this to show *verified* playback —
    proof the pixels are moving — instead of trusting that what was scheduled
    is what a distant screen is actually doing.
    """
    st = state or {}
    return {
        "src": st.get("src"),
        "slot_id": st.get("slot_id"),
        "kind": st.get("kind"),
        "time_pos": round(time_pos, 3) if isinstance(time_pos, (int, float)) else None,
        "playing": bool(playing),
        "freeze_recoveries": int(freezes),
    }


def write_display_state(record: dict) -> None:
    """Atomically drop the snapshot where the player will read it. Best-effort."""
    record = {**record, "at": time.time()}
    tmp = DISPLAY_STATE + ".part"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(record, f)
        os.replace(tmp, DISPLAY_STATE)
    except OSError:
        pass


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


def mpv_get(prop: str) -> float | None:
    """
    Read one numeric property back over the IPC socket.

    Unlike mpv_send this waits for the reply, so the watchdog can read the
    playback clock. Any failure — no socket, a timeout, a non-numeric or
    error reply — returns None, which the detector treats as "no clock to
    judge by" rather than a freeze, so a flaky read never triggers a reload.
    """
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2)
        sock.connect(IPC_SOCK)
        sock.sendall((json.dumps({"command": ["get_property", prop]}) + "\n").encode())
        buf = b""
        while b"\n" in buf or not buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
            for line in buf.split(b"\n"):
                if not line.strip():
                    continue
                msg = json.loads(line)
                if msg.get("error") == "success" and isinstance(msg.get("data"), (int, float)):
                    sock.close()
                    return float(msg["data"])
                if "error" in msg:
                    sock.close()
                    return None
            break
        sock.close()
    except (OSError, ValueError):
        return None
    return None


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
    last_state: dict | None = None
    detector = FreezeDetector()
    consecutive = 0
    total_freezes = 0

    while True:
        # mpv is the one thing that must never stay dead: relaunch it and
        # force the next state to re-apply so the screen fills again.
        if mpv.poll() is not None:
            mpv = launch_mpv()
            wait_for_socket(time.monotonic() + 8)
            current = None
            detector.reset()
            consecutive = 0

        state = get_state()
        if state is None:
            time.sleep(2)
            continue

        is_video = bool(state.get("kind") == "video")
        playing = is_video and state.get("src") not in (None, "/fallback")
        pos = mpv_get("time-pos") if is_video else None

        key = key_of(state)
        if key != current:
            current = key
            last_state = state
            show(state)
            detector.reset()          # a new item is healthy by definition
            consecutive = 0
        else:
            # Same item still up: make sure it is actually playing, not frozen
            # on a decoded frame while the clock is dead.
            if detector.observe(time.monotonic(), is_video, playing, pos):
                total_freezes += 1
                consecutive += 1
                if consecutive >= 2:
                    # A reload did not thaw it: the decoder or the surface is
                    # wedged, so bring mpv back from scratch.
                    try:
                        mpv.kill()
                    except OSError:
                        pass
                    mpv = launch_mpv()
                    wait_for_socket(time.monotonic() + 8)
                    current = None
                    consecutive = 0
                elif last_state is not None:
                    # First strike: reload the same file in place, which
                    # reopens the demuxer and decoder without a full restart.
                    show(last_state)

        # Leave a fresh snapshot of what the panel is really doing, for the
        # player to report. Best-effort: a write failure never stops playback.
        write_display_state(display_record(state, pos, playing, total_freezes))

        time.sleep(1)


if __name__ == "__main__":
    main()
