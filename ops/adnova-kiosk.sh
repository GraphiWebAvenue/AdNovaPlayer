#!/usr/bin/env bash
#
# Launches the full-screen display that shows the player.
#
# Runs as the desktop user under the Pi's Wayland compositor (labwc on
# Raspberry Pi OS). It drives mpv, not a browser: Chromium on a Pi 4 has no
# working hardware H.264 path and software-decodes every frame, so video
# stuttered and the board ran hot. mpv uses the board's own V4L2 decoder and
# plays smoothly at a few percent of one core. The player's core is
# unchanged — it still decides what plays and exposes it at /state; the mpv
# driver just reads that endpoint and tells mpv what to show.
#
# The file keeps its historical name so the autostart entries set up by
# setup-kiosk keep working without re-registration.
#
set -euo pipefail

# One display only. setup-kiosk registers this launcher in more than one
# place (labwc autostart, wayfire, XDG), so a session that honours two of
# them would start two mpv instances fighting over the screen. The lock makes
# the extra launch exit at once; the fd is inherited across the exec below, so
# it is held for the life of the driver and freed the instant it is killed —
# exactly what a remote restart needs.
exec 9>/tmp/adnova-kiosk.lock
flock -n 9 || exit 0

# Wait for the local player to answer before starting the display, so the
# first thing on screen is its splash/fallback, never a connection error.
for _ in $(seq 1 60); do
    if curl -fsS --max-time 2 "http://127.0.0.1:8080/healthz" >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

# Start the in-session helper that answers the player's screenshot and
# restart requests — it needs this Wayland session, which the headless player
# process cannot reach. It locks itself to one instance, so launching it on
# every start (including a remote restart) is safe.
HELPER="$(dirname "$0")/adnova-kiosk-helper.sh"
[ -f "$HELPER" ] && setsid bash "$HELPER" >/dev/null 2>&1 &

# Hand the screen to the mpv driver. It launches mpv, polls the player's
# /state, and drives mpv over its IPC socket — hardware-decoding video the
# whole time. Runs under the system python (stdlib only), so no venv here.
if ! command -v mpv >/dev/null 2>&1; then
    echo "mpv is not installed. Install it: sudo apt install -y mpv" >&2
    exit 1
fi
exec python3 "$(dirname "$0")/adnova-mpv-driver.py"
