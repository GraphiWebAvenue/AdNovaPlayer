#!/usr/bin/env bash
#
# In-session helper for the AdNova kiosk.
#
# The player service runs headless as its own user and cannot reach the
# desktop user's Wayland session — so screenshots and kiosk relaunches, both
# of which need that session, are done here, inside it. The player drops a
# trigger file in /tmp; this loop notices it and does the one fixed thing.
#
# There is no socket, no sudo, and no argument from the player: a trigger
# carries only "capture" or "relaunch", never a command, so the player can
# never make this run anything else. Started by adnova-kiosk.sh on every
# kiosk launch; the lock below keeps it to a single instance.
set -u

# One instance only. Relaunching the kiosk starts this script again; the
# lock makes the duplicate exit at once instead of piling up loops.
exec 9>/tmp/adnova-kiosk-helper.lock
flock -n 9 || exit 0

SHOT_REQ=/tmp/adnova-shot.req
SHOT_OUT=/tmp/adnova-shot.img
KIOSK_REQ=/tmp/adnova-kiosk.req
KIOSK_DONE=/tmp/adnova-kiosk.done
KIOSK="$(dirname "$0")/adnova-kiosk.sh"

while true; do
    # Screenshot: grab only when the request is newer than the last frame,
    # so a request file left lying around does not make us capture every
    # second. Written to a temp file and moved into place, so the player
    # never reads a half-written frame. PNG, not JPEG: the Pi's grim is built
    # without JPEG support ("jpeg support disabled"), and PNG is always
    # available. -s 0.5 halves the resolution to keep it a few hundred KB.
    if [ -f "$SHOT_REQ" ] && { [ ! -f "$SHOT_OUT" ] || [ "$SHOT_REQ" -nt "$SHOT_OUT" ]; }; then
        if grim -s 0.5 "$SHOT_OUT.tmp" 2>/dev/null; then
            mv -f "$SHOT_OUT.tmp" "$SHOT_OUT" 2>/dev/null || true
            chmod 0644 "$SHOT_OUT" 2>/dev/null || true
        else
            rm -f "$SHOT_OUT.tmp" 2>/dev/null || true
        fi
    fi

    # Kiosk relaunch: kill the browser and start it again, once per request.
    if [ -f "$KIOSK_REQ" ] && { [ ! -f "$KIOSK_DONE" ] || [ "$KIOSK_REQ" -nt "$KIOSK_DONE" ]; }; then
        touch "$KIOSK_DONE"
        pkill -f 'chromium' 2>/dev/null || true
        sleep 1
        if [ -f "$KIOSK" ]; then
            setsid bash "$KIOSK" >/dev/null 2>&1 &
        fi
    fi

    sleep 1
done
