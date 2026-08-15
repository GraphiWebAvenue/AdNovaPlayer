#!/usr/bin/env bash
#
# Make the kiosk browser start full-screen on every boot.
#
# The first design ran the browser as a system-level graphical service
# with a hardcoded user and display. On Raspberry Pi OS Bookworm the
# desktop is Wayland (labwc or wayfire) and autologs in as the first
# user, so that service never had a session to draw into — the Pi booted
# to the desktop and nothing appeared.
#
# This rides on the desktop that already comes up instead of fighting it:
# it registers the kiosk launcher in the autostart of whichever compositor
# and user own the graphical session. Belt and braces — it writes the
# labwc hook, the wayfire hook, and an XDG autostart entry, so whichever
# one this image uses picks it up.
#
#   sudo bash setup-kiosk.sh
#
set -euo pipefail

APP=/opt/adnova-player/current
LAUNCHER="$APP/ops/adnova-kiosk.sh"

blue() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { echo "Please run with sudo: sudo bash setup-kiosk.sh" >&2; exit 1; }

# The user who owns the graphical session — the one the desktop autologs
# in as. On a Pi that is the first real user (uid 1000); fall back to it
# if logind cannot say.
GUI_USER="$(loginctl list-users --no-legend 2>/dev/null | awk '$1>=1000 && $1<60000 {print $2; exit}')"
[ -n "${GUI_USER:-}" ] || GUI_USER="$(getent passwd 1000 | cut -d: -f1)"
[ -n "${GUI_USER:-}" ] || { echo "Could not find the desktop user." >&2; exit 1; }
GUI_HOME="$(getent passwd "$GUI_USER" | cut -d: -f6)"

blue "setting the kiosk to start for user '$GUI_USER'"

# A tiny launcher line, shared by every hook below.
run_as_user() { sudo -u "$GUI_USER" bash -c "$1"; }

# ── labwc (the current Pi OS Bookworm default) ──────────────────────────
# Its autostart is a shell script sourced when the compositor starts.
LABWC_DIR="$GUI_HOME/.config/labwc"
run_as_user "mkdir -p '$LABWC_DIR'"
if ! run_as_user "grep -q adnova-kiosk '$LABWC_DIR/autostart' 2>/dev/null"; then
    run_as_user "printf '%s\n' '$LAUNCHER &' >> '$LABWC_DIR/autostart'"
fi
run_as_user "chmod +x '$LABWC_DIR/autostart' 2>/dev/null || true"

# ── wayfire (older Bookworm images) ─────────────────────────────────────
WAYFIRE="$GUI_HOME/.config/wayfire.ini"
if run_as_user "test -f '$WAYFIRE'"; then
    run_as_user "grep -q '\[autostart\]' '$WAYFIRE' || printf '\n[autostart]\n' >> '$WAYFIRE'"
    if ! run_as_user "grep -q adnova-kiosk '$WAYFIRE'"; then
        run_as_user "sed -i '/\[autostart\]/a adnova_kiosk = $LAUNCHER' '$WAYFIRE'"
    fi
fi

# ── XDG autostart (honoured by most desktop sessions as a last resort) ──
XDG_DIR="$GUI_HOME/.config/autostart"
run_as_user "mkdir -p '$XDG_DIR'"
run_as_user "cat > '$XDG_DIR/adnova-kiosk.desktop' <<EOF
[Desktop Entry]
Type=Application
Name=AdNova Kiosk
Exec=$LAUNCHER
X-GNOME-Autostart-enabled=true
NoDisplay=true
EOF"

# ── Retire the old system service if it is still around ─────────────────
if systemctl list-unit-files 2>/dev/null | grep -q '^adnova-kiosk.service'; then
    blue "removing the old system-level kiosk service"
    systemctl disable --now adnova-kiosk.service 2>/dev/null || true
    rm -f /etc/systemd/system/adnova-kiosk.service
    systemctl daemon-reload
fi

blue "done. Reboot to see the kiosk full-screen:"
echo "    sudo reboot"
