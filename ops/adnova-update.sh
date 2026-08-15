#!/usr/bin/env bash
#
# Update the player from its signed git channel, safely.
#
# Runs from a timer, not continuously. Pulls origin/main, and only if
# something changed: installs any new dependencies, then restarts the
# service. If the new version does not come back healthy, it rolls back to
# the exact commit that was running and restarts that — so a bad release
# is a blip, never a fleet of dark screens someone must drive out to fix.
#
# Code updates come through git (a channel we control and could sign),
# never through the ad content. Media is data the browser renders; it has
# no path to the shell. This is the only thing that changes what runs.
#
set -euo pipefail

APP=/opt/adnova-player/current
VENV=/opt/adnova-player/venv

blue() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mFAIL:\033[0m %s\n' "$*" >&2; exit 1; }

cd "$APP" || die "$APP is missing"

# A hand-edit on the device is either an emergency fix nobody committed or
# a sign something is wrong; either way, do not silently overwrite it.
if [ -n "$(git status --porcelain)" ]; then
    die "working tree is dirty — refusing to update over local changes"
fi

git fetch --quiet origin main
BEFORE="$(git rev-parse HEAD)"
AFTER="$(git rev-parse origin/main)"

if [ "$BEFORE" = "$AFTER" ]; then
    exit 0   # nothing to do; the common case, and silent
fi

blue "updating ${BEFORE:0:7} -> ${AFTER:0:7}"
git reset --quiet --hard origin/main

# Dependencies only when they changed — a pip run on every update would
# thrash a shop's uplink and the Pi's card for nothing.
if ! git diff --quiet "$BEFORE" "$AFTER" -- pyproject.toml; then
    blue "dependencies changed — installing"
    "$VENV/bin/pip" install --quiet --upgrade "$APP"
fi

blue "restarting"
systemctl restart adnova-player

# Prove the new version actually plays before trusting it. is-active alone
# would pass for a process failing every request.
for _ in $(seq 1 20); do
    if curl -fsS --max-time 2 http://127.0.0.1:8080/healthz >/dev/null 2>&1; then
        blue "updated to ${AFTER:0:7} and healthy"
        exit 0
    fi
    sleep 1
done

printf '\033[1;31mFAIL:\033[0m the new version did not come back healthy. Rolling back.\n' >&2
git reset --quiet --hard "$BEFORE"
systemctl restart adnova-player
die "rolled back to ${BEFORE:0:7}"
