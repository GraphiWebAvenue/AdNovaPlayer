#!/usr/bin/env bash
#
# In-session helper for the AdNova display.
#
# One job: relaunch the display (mpv driver) when the player asks. That needs
# the desktop session, which the headless, hardened player process cannot
# reach. The player — which may only write its own state dir under
# ProtectSystem=strict — drops a request under /var/lib/adnova-player/ipc;
# this loop, running in the session and in the adnova group, notices it and
# does the one fixed thing. No socket, no sudo, no argument crosses over.
#
# Screenshots are NOT handled here anymore: adnova_player.shots grabs and
# uploads the screen on its own, independently of the player, so it works
# even when the player is down.
#
# Started by adnova-kiosk.sh on every launch; the lock keeps it to one.
set -u

exec 9>/tmp/adnova-kiosk-helper.lock
flock -n 9 || exit 0

# The request is written by the player in the one dir it may write to; the
# .done marker is ours to write, so it lives in our own /tmp (writable for the
# desktop user — the read-only /tmp was only the player's hardened view).
KIOSK_REQ="/var/lib/adnova-player/ipc/restart-kiosk.req"
KIOSK_DONE="/tmp/adnova-kiosk.done"
KIOSK="$(dirname "$0")/adnova-kiosk.sh"

while true; do
    # Display relaunch: kill the current display and start it again, once per
    # request. Killing the driver and mpv frees the launcher's flock, so the
    # relaunched instance acquires it instead of exiting immediately.
    if [ -f "$KIOSK_REQ" ] && { [ ! -f "$KIOSK_DONE" ] || [ "$KIOSK_REQ" -nt "$KIOSK_DONE" ]; }; then
        touch "$KIOSK_DONE" 2>/dev/null || true
        pkill -f 'adnova-mpv-driver' 2>/dev/null || true
        pkill -x mpv 2>/dev/null || true
        pkill -f 'chromium' 2>/dev/null || true
        sleep 1
        if [ -f "$KIOSK" ]; then
            setsid bash "$KIOSK" >/dev/null 2>&1 &
        fi
    fi

    sleep 1
done
