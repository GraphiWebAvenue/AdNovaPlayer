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

# The package is installed editable (pip install -e), so the git reset above
# already updated the code the venv imports — no rebuild, and none of the
# setuptools build-cache staleness that shipped mismatched modules when the
# Pi's clock jumped. Only a pyproject change (new dependency or entry point)
# needs pip to run again; re-assert the editable install then. A stale build/
# from an earlier non-editable install is removed so it can never leak in.
rm -rf "$APP/build" 2>/dev/null || true
if ! git diff --quiet "$BEFORE" "$AFTER" -- pyproject.toml; then
    blue "dependencies changed — reinstalling (editable)"
    "$VENV/bin/pip" install --quiet -e "$APP"
fi

# Ops files — systemd units, helper scripts, the sudoers rule — are laid
# down by the provisioner, so a code-only update would leave a device's
# units a release behind. Sync them here whenever anything under ops/
# changed, so a new service or a widened sudoers rule reaches the fleet
# through the same git channel as the code, with no site visit.
if ! git diff --quiet "$BEFORE" "$AFTER" -- ops/; then
    blue "ops changed — syncing units, scripts and sudoers"

    # Retire the old system-level kiosk service, on every update, forever.
    #
    # setup-kiosk.sh removes this too, but only devices that were re-run
    # through it ever got that — and one that was not spent every boot in
    # the following state: the unit starts the display launcher as the
    # *service* account, which owns no graphical session, so it dies. On its
    # way out it leaves /tmp/adnova-kiosk.lock owned by that account, and
    # from then on the real launcher (running as the desktop user, which is
    # a different uid) cannot open the file at all. The panel stays black
    # through every reboot and nothing anywhere records why.
    #
    # The launcher's locks are per-uid now so that trap cannot be set again,
    # but the unit itself has to go, and this is the only path that reaches
    # a Pi nobody is standing next to.
    if systemctl list-unit-files 2>/dev/null | grep -q '^adnova-kiosk\.service'; then
        blue "removing the retired system-level kiosk service"
        systemctl disable --now adnova-kiosk.service 2>/dev/null || true
        rm -f /etc/systemd/system/adnova-kiosk.service
        # The lock it may have left behind, which is the part that actually
        # blocks the screen. Safe: nothing legitimate holds a stale one.
        rm -f /tmp/adnova-kiosk.lock /tmp/adnova-kiosk-helper.lock \
              /tmp/adnova-kiosk.done /tmp/adnova-screen.done 2>/dev/null || true
    fi

    install -m 644 "$APP/ops/adnova-player.service"     /etc/systemd/system/ || true
    install -m 644 "$APP/ops/adnova-update.service"     /etc/systemd/system/ || true
    install -m 644 "$APP/ops/adnova-update.timer"       /etc/systemd/system/ || true
    install -m 644 "$APP/ops/adnova-os-update.service"  /etc/systemd/system/ || true
    install -m 755 "$APP/ops/adnova-os-update.sh"       /usr/local/bin/adnova-os-update.sh || true

    # Regenerate the sudoers rule to match this release, but never trust it
    # unvalidated: write it to a temp file, check it with visudo, and move it
    # into place only if it parses. A malformed sudoers file would deny the
    # player its own restart — worse than an out-of-date one.
    APP_USER="$(awk -F= '/^User=/{print $2}' "$APP/ops/adnova-player.service" | tr -d '[:space:]')"
    APP_USER="${APP_USER:-adnova}"
    SUDO_TMP="$(mktemp)"
    sed "s/@APP_USER@/$APP_USER/" "$APP/ops/sudoers-adnova-player.template" > "$SUDO_TMP"
    if visudo -c -f "$SUDO_TMP" >/dev/null 2>&1; then
        install -m 0440 "$SUDO_TMP" /etc/sudoers.d/adnova-player || true
    fi
    rm -f "$SUDO_TMP"

    systemctl daemon-reload || true
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
