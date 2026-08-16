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
# Hardware video decode is the difference between smooth 1080p and a
# slideshow on a Pi 4. By default Chromium decodes H.264 in software, which
# a Pi cannot keep up with — hence the stutter. The feature flags below turn
# on the board's own decoder (V4L2), and --ignore-gpu-blocklist stops
# Chromium refusing it on unrecognised hardware. Every --enable-features and
# --disable-features value is merged into a single flag of each: Chromium
# honours only the last one it sees, so a second flag would silently drop
# the first.
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
    --ozone-platform=wayland \
    --use-gl=egl \
    --ignore-gpu-blocklist \
    --enable-gpu-rasterization \
    --enable-zero-copy \
    --enable-accelerated-video-decode \
    --enable-accelerated-mjpeg-decode \
    --enable-features=UseOzonePlatform,VaapiVideoDecoder,VaapiVideoDecodeLinuxGL,AcceleratedVideoDecodeLinuxGL \
    --disable-features=Translate,TranslateUI,MediaRouter,UseChromeOSDirectVideoDecoder
