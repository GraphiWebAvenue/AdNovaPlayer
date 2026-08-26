#!/usr/bin/env bash
#
# In-session helper for the AdNova display.
#
# Two jobs, both in the desktop session the headless, hardened player process
# cannot reach: (1) relaunch the display (mpv driver) when the player asks,
# and (2) power the panel on/off when an operator flips it from Dashboard. The
# player — which may only write its own state dir under ProtectSystem=strict —
# drops a request under /var/lib/adnova-player/ipc; this loop, running in the
# session and in the adnova group, notices it and does the one fixed thing.
# No socket, no sudo, no argument crosses over — only a fixed word is read.
#
# Screenshots are NOT handled here anymore: adnova_player.shots grabs and
# uploads the screen on its own, independently of the player, so it works
# even when the player is down.
#
# Started by adnova-kiosk.sh on every launch; the lock keeps it to one.
set -u

# Per-uid, for the same reason the launcher's lock is: a fixed name under /tmp
# lets whichever account gets there first lock every other account out
# permanently. That is not theoretical — it is how a stand lost its screen.
UID_N="$(id -u)"
LOCK="${XDG_RUNTIME_DIR:-/tmp}/adnova-kiosk-helper.$UID_N.lock"
if ! exec 9>"$LOCK"; then
    exec 9>/dev/null   # no lock available; one helper is better than none
fi
flock -n 9 || exit 0

# The request is written by the player in the one dir it may write to; the
# .done marker is ours to write, so it lives in our own /tmp (writable for the
# desktop user — the read-only /tmp was only the player's hardened view).
KIOSK_REQ="/var/lib/adnova-player/ipc/restart-kiosk.req"
KIOSK_DONE="/tmp/adnova-kiosk.$UID_N.done"
KIOSK="$(dirname "$0")/adnova-kiosk.sh"

# Manual screen-power override from Dashboard (screen_on / screen_off command).
# The player writes the fixed word "on" or "off" here; we drive the panel. The
# autonomous operating-hours logic still runs, so this is a momentary override
# that the hours schedule reasserts on its next cycle — which is the intent.
SCREEN_REQ="/var/lib/adnova-player/ipc/screen.req"
SCREEN_DONE="/tmp/adnova-screen.$UID_N.done"

set_screen() {
    # $1 = on|off. Try the Pi's own HDMI power first (no compositor needed),
    # then wlr-randr as a Wayland fallback. Best-effort; a missing tool is fine.
    local want="$1" val
    [ "$want" = "off" ] && val=0 || val=1
    if command -v vcgencmd >/dev/null 2>&1; then
        vcgencmd display_power "$val" >/dev/null 2>&1 || true
    fi
    if command -v wlr-randr >/dev/null 2>&1; then
        local out
        out="$(wlr-randr 2>/dev/null | awk 'NR==1{print $1}')"
        if [ -n "$out" ]; then
            wlr-randr --output "$out" --"$want" >/dev/null 2>&1 || true
        fi
    fi
}

while true; do
    # Display relaunch: kill the current display and start it again, once per
    # request. Killing the driver and mpv frees the launcher's flock, so the
    # relaunched instance acquires it instead of exiting immediately.
    if [ -f "$KIOSK_REQ" ] && { [ ! -f "$KIOSK_DONE" ] || [ "$KIOSK_REQ" -nt "$KIOSK_DONE" ]; }; then
        touch "$KIOSK_DONE" 2>/dev/null || true
        pkill -f 'adnova-mpv-driver' 2>/dev/null || true
        pkill -x mpv 2>/dev/null || true
        pkill -f 'chromium' 2>/dev/null || true
        sleep 2

        # The launcher supervises the driver now, so killing the driver is
        # normally the whole job — the supervisor brings it straight back
        # within a couple of seconds. Only when no supervisor exists at all
        # is there nothing to do that, and only then do we start one. Testing
        # for the supervisor rather than the driver matters: right here the
        # driver is deliberately dead and a healthy supervisor is mid-backoff,
        # so keying off the driver would kill the very thing about to fix it.
        # The pattern needs the literal dot — adnova-kiosk-helper.sh (this
        # script) must never match it.
        if ! pgrep -f 'adnova-kiosk\.sh' >/dev/null 2>&1; then
            if [ -f "$KIOSK" ]; then
                setsid bash "$KIOSK" >/dev/null 2>&1 &
            fi
        fi
    fi

    # Screen on/off: act once per request, reading only the fixed word.
    if [ -f "$SCREEN_REQ" ] && { [ ! -f "$SCREEN_DONE" ] || [ "$SCREEN_REQ" -nt "$SCREEN_DONE" ]; }; then
        touch "$SCREEN_DONE" 2>/dev/null || true
        want="$(tr -d '[:space:]' < "$SCREEN_REQ" 2>/dev/null)"
        if [ "$want" = "on" ] || [ "$want" = "off" ]; then
            set_screen "$want"
        fi
    fi

    sleep 1
done
