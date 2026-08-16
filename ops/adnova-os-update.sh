#!/usr/bin/env bash
#
# Applies operating-system and security updates on the Pi.
#
# Triggered from Dashboard's remote command menu (the "os_update" command),
# which the player turns into `systemctl start adnova-os-update.service` —
# never an arbitrary shell string. It runs unattended, so every choice here
# favours "leave the device working" over "get the newest bits":
#
#   * `upgrade`, not `full-upgrade`/`dist-upgrade` — never removes an
#     installed package to satisfy a dependency, so it cannot uninstall the
#     browser or the player under us.
#   * no automatic reboot — a shop screen going dark unannounced is worse
#     than a kernel that updates on the next manual reboot. If a reboot is
#     needed, it is reported, and the operator reboots from Dashboard.
#   * the outcome is written to a small JSON file the player reads and
#     reports on its next heartbeat, so Dashboard shows "ok" or the error
#     rather than the operator guessing whether the click did anything.
#
set -uo pipefail

RESULT="/var/lib/adnova-player/os-update.json"
LOG="/var/log/adnova-player/os-update.log"
mkdir -p "$(dirname "$RESULT")" "$(dirname "$LOG")" 2>/dev/null || true

export DEBIAN_FRONTEND=noninteractive

# ISO-8601 UTC, matching what the rest of the system logs.
now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# Write the result file the player polls. $1=status (ok|error), $2=detail.
write_result() {
    local status="$1" detail="$2" reboot="no"
    [ -f /var/run/reboot-required ] && reboot="required"
    # detail is our own text, but escape the quotes and backslashes anyway
    # so the file is always valid JSON for the player to parse.
    detail="${detail//\\/\\\\}"
    detail="${detail//\"/\\\"}"
    printf '{"status":"%s","detail":"%s","reboot":"%s","at":"%s"}\n' \
        "$status" "$detail" "$reboot" "$(now)" > "$RESULT"
}

{
    echo "=== adnova-os-update $(now) ==="

    if ! apt-get update -y 2>&1; then
        write_result "error" "apt-get update failed"
        exit 1
    fi

    # Count what will change, for a human-readable result line.
    upgradable="$(apt-get -s upgrade 2>/dev/null | grep -c '^Inst' || true)"

    if ! apt-get upgrade -y 2>&1; then
        write_result "error" "apt-get upgrade failed"
        exit 1
    fi

    apt-get autoremove -y 2>&1 || true
    apt-get clean 2>&1 || true

    write_result "ok" "Updated ${upgradable} package(s)"
    echo "=== done $(now) ==="
} >> "$LOG" 2>&1
