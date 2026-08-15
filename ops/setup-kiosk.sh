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

# The desktop user is the first real account on the Pi — uid 1000, the one
# created when the card was written. Deterministic on purpose: the adnova
# service account is a later uid with no password and no desktop, and
# autologging in as it (an earlier mistake) left a login nobody could pass.
GUI_USER="$(getent passwd 1000 | cut -d: -f1)"
[ -n "${GUI_USER:-}" ] || { echo "Could not find the desktop user (uid 1000)." >&2; exit 1; }
GUI_HOME="$(getent passwd "$GUI_USER" | cut -d: -f6)"

blue "the desktop user is '$GUI_USER'"

# ── Fix the autologin ───────────────────────────────────────────────────
# Make the desktop log in as this user, undoing any earlier autologin set
# to the passwordless service account. Both the modern raspi-config path
# and the lightdm file are covered, since which one applies varies by image.
blue "setting desktop autologin to '$GUI_USER'"
if command -v raspi-config >/dev/null 2>&1; then
    SUDO_USER="$GUI_USER" raspi-config nonint do_boot_behaviour B4 2>/dev/null || true
fi
if [ -f /etc/lightdm/lightdm.conf ]; then
    sed -i "s/^autologin-user=.*/autologin-user=$GUI_USER/" /etc/lightdm/lightdm.conf 2>/dev/null || true
fi

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
