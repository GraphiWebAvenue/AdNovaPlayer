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
exec "$BROWSER" \
    --kiosk \
    --start-fullscreen \
    --user-data-dir="$PROFILE" \
    --app="$URL" \
    --noerrdialogs \
    --disable-infobars \
    --disable-session-crashed-bubble \
    --disable-component-update \
    --check-for-update-interval=31536000 \
    --autoplay-policy=no-user-gesture-required \
    --disable-pinch \
    --overscroll-history-navigation=0 \
    --disable-features=Translate,TranslateUI,MediaRouter \
    --disable-notifications \
    --hide-scrollbars \
    --ozone-platform=wayland \
    --enable-features=UseOzonePlatform
