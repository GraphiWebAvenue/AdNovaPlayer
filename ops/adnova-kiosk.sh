#!/usr/bin/env bash
#
# Launches the full-screen browser that shows the player.
#
# Runs as the kiosk user under the Pi's Wayland compositor (labwc on
# Raspberry Pi OS Bookworm). Chromium points at the local server and
# nothing else — it never reaches Dashboard, holds no credential, and is
# just a rendering surface. Everything sensitive is on the Python side of
# http://127.0.0.1.
#
# Restarted by its own systemd unit if it ever exits, so a browser crash
# is a two-second black flash, not a dead screen.
#
set -euo pipefail

# One kiosk only. setup-kiosk registers the launcher in more than one place
# (labwc autostart, wayfire, XDG) so a session that honours two of them fires
# two browsers — both full-screen, fighting over the display, which looks
# like a black or flickering screen. The lock makes the extra launch exit at
# once. The fd is inherited across the exec below, so the lock is held for as
# long as the browser lives and released the moment it is killed — which is
# exactly what a remote kiosk-restart needs.
exec 9>/tmp/adnova-kiosk.lock
flock -n 9 || exit 0

URL="${ADNOVA_KIOSK_URL:-http://127.0.0.1:8080/}"

# Wait for the local player to answer before pointing the browser at it,
# so the first thing on screen is the splash, never a connection error.
for _ in $(seq 1 60); do
    if curl -fsS --max-time 2 "http://127.0.0.1:8080/healthz" >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

# Find the browser. Raspberry Pi OS ships chromium; some images call it
# chromium-browser.
BROWSER="$(command -v chromium || command -v chromium-browser || true)"
if [ -z "$BROWSER" ]; then
    echo "No chromium found. Install it: sudo apt install -y chromium-browser" >&2
    exit 1
fi

# A dedicated, throwaway profile so nothing persists between runs and a
# corrupt profile never wedges the browser.
PROFILE="$(mktemp -d /tmp/adnova-kiosk.XXXXXX)"
trap 'rm -rf "$PROFILE"' EXIT

# Start the in-session helper that answers the player's screenshot and
# kiosk-restart requests — it needs this Wayland session, which the headless
# player process cannot reach. It locks itself to one instance, so launching
# it on every kiosk start (including a remote restart) is safe.
HELPER="$(dirname "$0")/adnova-kiosk-helper.sh"
[ -f "$HELPER" ] && setsid bash "$HELPER" >/dev/null 2>&1 &

# The flags, each earning its place on a 2 GB signage box:
#   --kiosk / --start-fullscreen  full screen, no chrome, no way out
#   --noerrdialogs / --disable-* silence every popup, prompt and nag that
#                                 could freeze on screen with nobody to
#                                 dismiss it
#   --autoplay-policy            let video (and audio, when unmuted) start
#                                 without a user gesture there will never be
#   --check-for-update-interval  never let Chromium try to update itself
#   --disable-features=Translate,...  strip weight the panel does not need
#   --ozone-platform=wayland     match the Pi's compositor for hw video
#   --password-store=basic       the important one for an autologin kiosk:
#                                 keep Chromium off the system keyring. Under
#                                 passwordless autologin the keyring stays
#                                 locked, so anything touching it pops an
#                                 "Unlock Keyring" dialog on every launch —
#                                 useless for signage, which stores no
#                                 passwords. basic makes it never ask.
#
# Video decode: do NOT override the Pi's own GL and decode configuration.
#
# An earlier version forced a pile of flags here — --use-gl=egl,
# --enable-features=VaapiVideoDecoder,VaapiVideoDecodeLinuxGL,..., and
# --disable-features=UseChromeOSDirectVideoDecoder — on the theory that they
# would turn on hardware decode. Measured on a real Pi 4 (Trixie, labwc,
# Wayland) they did the opposite: a plain 720p24 H.264 clip that a Pi decodes
# in its sleep pinned one core at ~90% and played frame-by-frame. Stripping
# every one of those overrides — letting Chromium keep the flags the Pi's own
# wrapper injects via /etc/chromium.d — halved the CPU and made playback
# smooth. The Pi 4 has no VA-API H.264 driver (its decoder is V4L2), so the
# Vaapi flags never engaged hardware decode; they only forced a slower
# software path. If a future Pi image gains a working VA-API driver, add the
# flags back behind a real measurement, not a hunch.
exec "$BROWSER" \
    --kiosk \
    --start-fullscreen \
    --user-data-dir="$PROFILE" \
    --app="$URL" \
    --password-store=basic \
    --use-mock-keychain \
    --noerrdialogs \
    --disable-infobars \
    --disable-session-crashed-bubble \
    --disable-component-update \
    --check-for-update-interval=31536000 \
    --autoplay-policy=no-user-gesture-required \
    --disable-pinch \
    --overscroll-history-navigation=0 \
    --disable-notifications \
    --hide-scrollbars \
    --ozone-platform=wayland
