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
# Deliberately NOT `set -e`. This launcher's one job is to reach mpv, and
# every step before it is a best-effort tweak — resolution, audio routing —
# that must never be able to stop the screen from coming up. An unguarded
# `set -e` did exactly that: a stand with no HDMI audio sink at autostart
# (a normal race with PipeWire) died on the sink lookup before the helper,
# the uploader and mpv were ever started, and left no trace anywhere. The
# failures that DO matter are handled where they happen; see the mpv check.
set -uo pipefail

# Every path this script owns carries the uid of whoever is running it.
#
# This is not tidiness. A fixed name under /tmp is a cross-user landmine: a
# stale system-level adnova-kiosk.service, running as the service account,
# created /tmp/adnova-kiosk.lock owned by `adnova` with mode 0644 — and from
# that moment the real launcher, running as the desktop user, could not open
# it. `exec 9>` failed, the shell died on the redirection, and the panel
# stayed black through every reboot with nothing logged anywhere. One
# zero-byte file, left by a service that was itself already broken, held a
# stand dark indefinitely. Per-uid names mean one account can no longer lock
# another one out of its own screen.
UID_N="$(id -u)"

# The lock belongs in the session's own runtime dir when there is one: it is
# per-user and per-boot by construction, which is exactly the scope of "one
# display for this session". /tmp is the fallback for a session without it.
RUNDIR="${XDG_RUNTIME_DIR:-/tmp}"
LOCK="$RUNDIR/adnova-kiosk.$UID_N.lock"

# The log cannot follow it there: XDG_RUNTIME_DIR is mode 0700 and the player
# service runs as a different user, so it could never read it. /tmp keeps it
# world-readable — the player's hardened view of /tmp is read-only, not
# private — and the tail of it rides the diagnostics bundle, so "the display
# never came up and nobody noticed" stops being a state this fleet can be in.
LOG="/tmp/adnova-kiosk-launch.$UID_N.log"

if [ -f "$LOG" ] && [ "$(stat -c %s "$LOG" 2>/dev/null || echo 0)" -gt 1000000 ]; then
    : >"$LOG" 2>/dev/null || true
fi

# Never die trying to open the log. Losing the trail is bad; losing the screen
# because we could not write about it would be absurd.
if ! : >>"$LOG" 2>/dev/null; then
    LOG=/dev/null
fi
exec 2>>"$LOG"
log() { printf '%s %s\n' "$(date -Is 2>/dev/null || date)" "$*" >>"$LOG" 2>/dev/null || true; }
trap 'log "launcher exited (status $?)"' EXIT

log "starting as $(id -un) (uid $UID_N) — session ${WAYLAND_DISPLAY:-none}"

# One display only. setup-kiosk registers this launcher in more than one
# place (labwc autostart, wayfire, XDG), so a session that honours two of
# them would start two mpv instances fighting over the screen. The lock makes
# the extra launch exit at once; the fd is inherited across the exec below, so
# it is held for the life of the driver and freed the instant it is killed —
# exactly what a remote restart needs.
#
# Opening it is itself allowed to fail. That is the failure that cost a stand
# its screen, and a redirection error would otherwise end this shell on the
# spot with nothing written down. Say so, and carry on without the lock — a
# second display is a visible, fixable annoyance; no display is an outage.
HAVE_LOCK=1
if ! exec 9>"$LOCK"; then
    log "WARNING could not open $LOCK — continuing without the single-instance lock"
    HAVE_LOCK=0
fi

# Only consult the lock when we actually hold a real one. Falling back to
# flock on /dev/null would be worse than useless: if it failed we would read
# it as "another launcher owns the screen" and exit, turning the missing lock
# straight back into the black panel this whole change exists to prevent.
if [ "$HAVE_LOCK" = 1 ] && ! flock -n 9; then
    # Usually this is the duplicate autostart entry doing its job. But a lock
    # held while NO driver runs means a wedged launcher from an earlier
    # session is blocking the screen — indistinguishable from the outside,
    # and previously silent. Name the difference so the log can be read.
    if pgrep -f adnova-mpv-driver >/dev/null 2>&1; then
        log "another launcher holds the lock and the driver is up — nothing to do"
    else
        log "WARNING lock held but no mpv driver is running — a wedged launcher is blocking the display"
    fi
    exit 0
fi

# Wait for the local player to answer before starting the display, so the
# first thing on screen is its splash/fallback, never a connection error.
for _ in $(seq 1 60); do
    if curl -fsS --max-time 2 "http://127.0.0.1:8080/healthz" >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

# Cap every connected output at 1080p. The kernel video= param does not stick
# on Wayland — the compositor re-reads EDID and picks the panel's native mode,
# often 4K — so it has to be set here, in the session, on every start. A Pi 4
# renders 1080p smoothly but stutters scaling frames to 4K even with hardware
# decode, so this is what keeps playback smooth on whatever panel a stand has.
if command -v wlr-randr >/dev/null 2>&1; then
    for out in $(wlr-randr 2>/dev/null | awk '/^[A-Za-z0-9]/{print $1}'); do
        wlr-randr --output "$out" --mode 1920x1080 >/dev/null 2>&1 || true
    done
fi

# Route audio to HDMI. The default sink can land on the 3.5mm jack, leaving
# the video silent. Find the HDMI sink and make it the default — its numeric
# id is assigned fresh each boot, so re-find it here rather than hard-code one.
# A stand with no HDMI sink yet (PipeWire still settling) simply keeps the
# default sink — silent video is survivable, a dark screen is not. The `|| true`
# on the assignment is load-bearing: without it a no-match `grep` propagates
# through `pipefail` and takes the whole launcher with it.
if command -v wpctl >/dev/null 2>&1; then
    sink="$(wpctl status 2>/dev/null | sed -n '/Sinks:/,/Sources:/p' \
        | grep -i hdmi | grep -oE '[0-9]+' | head -1)" || true
    if [ -n "${sink:-}" ]; then
        wpctl set-default "$sink" >/dev/null 2>&1 || log "could not set HDMI sink $sink"
    else
        log "no HDMI audio sink found — continuing on the default sink"
    fi
fi

# Start the in-session helper that answers the player's screenshot and
# restart requests — it needs this Wayland session, which the headless player
# process cannot reach. It locks itself to one instance, so launching it on
# every start (including a remote restart) is safe.
HELPER="$(dirname "$0")/adnova-kiosk-helper.sh"
[ -f "$HELPER" ] && setsid bash "$HELPER" >/dev/null 2>&1 &

# Screenshot uploader: grabs the screen and posts it to Dashboard on a slow
# loop — in-session (grim needs the display) and under the player's venv (to
# reuse its signing and credentials). It runs independently of the player
# service, so an operator in another city can see the screen even if the
# player has crashed. The player itself cannot do this: ProtectSystem=strict
# stops it writing the exchange files this used to need.
VENV_PY=/opt/adnova-player/venv/bin/python
[ -x "$VENV_PY" ] && setsid "$VENV_PY" -m adnova_player.shots >/dev/null 2>&1 &

# Hand the screen to the mpv driver. It launches mpv, polls the player's
# /state, and drives mpv over its IPC socket — hardware-decoding video the
# whole time. Runs under the system python (stdlib only), so no venv here.
if ! command -v mpv >/dev/null 2>&1; then
    log "FATAL mpv is not installed — install it: sudo apt install -y mpv"
    exit 1
fi

# Supervise the driver rather than exec into it. The player service has
# Restart=always, a systemd watchdog and the board's hardware watchdog under
# it; the display stack — the only part that actually puts pixels on the
# panel — had none of that, because it starts from a desktop autostart hook
# and not from systemd. This loop is that missing net, in the one place that
# needs no new unit and no session plumbing to be wrong about.
#
# The flock on fd 9 stays held by this shell for the life of the supervisor,
# so a second autostart entry still finds the lock taken and exits.
DRIVER="$(dirname "$0")/adnova-mpv-driver.py"
backoff=2
while true; do
    log "starting the mpv driver"
    started=$(date +%s)
    python3 "$DRIVER"
    status=$?
    ran=$(( $(date +%s) - started ))

    # A driver that stayed up for a while and then died is an ordinary crash:
    # come straight back. One that dies immediately is failing to start at
    # all — back off so a broken release cannot spin the board's CPU.
    if [ "$ran" -ge 60 ]; then
        backoff=2
    else
        backoff=$(( backoff * 2 ))
        [ "$backoff" -gt 60 ] && backoff=60
    fi
    log "driver exited (status $status) after ${ran}s — restarting in ${backoff}s"
    sleep "$backoff"
done
