#!/usr/bin/env bash
#
# Turn a fresh Raspberry Pi OS into an AdNova player. Run once, as root.
#
#   sudo bash provision.sh
#
# It asks for the four things the install sheet carries — stand id, stand
# key, the manifest public key, and an admin password — installs
# everything, wires up the services, and leaves a device that plays on
# every boot and recovers from anything.
#
# Idempotent: run it again and it changes only what has drifted. Safe to
# re-run to repair a device rather than reimaging it.
#
set -euo pipefail

APP_USER=adnova
BASE=/opt/adnova-player
CACHE=/var/lib/adnova-player
ENV_DIR=/etc/adnova-player
ENV_FILE="$ENV_DIR/env"
# The repo is private, reached through the read-only deploy key the
# bootstrap installed. The `github-player` host alias in root's ssh config
# points at github.com with that key; auto-update reuses the same remote.
REPO="git@github-player:GraphiWebAvenue/AdNovaPlayer.git"

blue() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mNOTE:\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mFAIL:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run this with sudo"

# ── Packages ────────────────────────────────────────────────────────────
blue "installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
    git python3 python3-venv python3-pip curl \
    chromium-browser 2>/dev/null || apt-get install -y -qq \
    git python3 python3-venv python3-pip curl chromium
# Optional but nice: screenshot grabber, CEC control, image tools.
apt-get install -y -qq grim cec-utils python3-pil 2>/dev/null || true

# ── User and directories ────────────────────────────────────────────────
if ! id "$APP_USER" >/dev/null 2>&1; then
    blue "creating the $APP_USER user"
    # A real login user: the kiosk browser needs a session. Added to
    # video/render so it can drive the display, and tty for the console.
    useradd --create-home --shell /bin/bash --groups video,render,tty,audio "$APP_USER"
fi

mkdir -p "$BASE" "$CACHE" "$ENV_DIR"
chown -R "$APP_USER":"$APP_USER" "$CACHE"
chmod 750 "$ENV_DIR"

# ── Code ────────────────────────────────────────────────────────────────
if [ -d "$BASE/current/.git" ]; then
    blue "updating the checkout"
    git -C "$BASE/current" fetch --quiet origin main
    git -C "$BASE/current" reset --quiet --hard origin/main
else
    blue "cloning the player"
    git clone --quiet "$REPO" "$BASE/current"
fi

# ── Virtualenv ──────────────────────────────────────────────────────────
# Outside the checkout, so an update's git reset never deletes it.
if [ ! -x "$BASE/venv/bin/python" ]; then
    blue "creating the virtualenv"
    python3 -m venv "$BASE/venv"
fi
blue "installing dependencies"
"$BASE/venv/bin/pip" install --quiet --upgrade pip
"$BASE/venv/bin/pip" install --quiet "$BASE/current"

# ── Credentials ─────────────────────────────────────────────────────────
if [ -f "$ENV_FILE" ]; then
    blue "environment file already exists — leaving it alone"
else
    blue "let's provision this device"
    echo
    read -rp "  Stand id (from the install sheet): " STAND_ID
    read -rp "  Stand key (64 characters): " STAND_KEY
    echo "  Manifest public key — run 'php artisan adnova:trust' on Dashboard."
    read -rp "  Key id: " KEY_ID
    read -rp "  Public key (base64): " PUB_KEY
    read -rp "  Admin username for the on-site page [admin]: " ADMIN_USER
    ADMIN_USER="${ADMIN_USER:-admin}"
    read -rsp "  Admin password: " ADMIN_PASS; echo

    [ -n "$STAND_ID" ] && [ -n "$STAND_KEY" ] || die "stand id and key are required"

    PASS_HASH="$("$BASE/venv/bin/python" -c \
        "from adnova_player.admin import hash_password; print(hash_password('''$ADMIN_PASS'''))")"
    TRUSTED_KEYS="{}"
    [ -n "$KEY_ID" ] && [ -n "$PUB_KEY" ] && TRUSTED_KEYS="{\"$KEY_ID\": \"$PUB_KEY\"}"

    cat > "$ENV_FILE" <<EOF
# AdNova Player — provisioned $(date -u +%Y-%m-%dT%H:%M:%SZ). Edit by hand after.
ADNOVA_STAND_ID=$STAND_ID
ADNOVA_STAND_KEY=$STAND_KEY
ADNOVA_BASE_URL=https://dashboard.adnovatech.online
ADNOVA_CACHE_DIR=$CACHE
ADNOVA_TRUSTED_KEYS=$TRUSTED_KEYS
ADNOVA_ADMIN_USER=$ADMIN_USER
ADNOVA_ADMIN_PASSWORD_HASH=$PASS_HASH
EOF
    warn "the stand key and admin hash are in $ENV_FILE, root-only."
fi

# The env holds the key: root-owned, service-user-readable, nobody else.
chown root:"$APP_USER" "$ENV_FILE"
chmod 640 "$ENV_FILE"
chown -R root:"$APP_USER" "$BASE"
chmod +x "$BASE"/current/ops/*.sh

# ── Auto-login to the desktop ───────────────────────────────────────────
# The kiosk browser needs a graphical session. Point the Pi's display
# manager at the adnova user with no password, so a reboot lands straight
# in the player with nobody at the keyboard.
if command -v raspi-config >/dev/null 2>&1; then
    blue "enabling desktop autologin as $APP_USER"
    raspi-config nonint do_boot_behaviour B4 2>/dev/null || true
    sed -i "s/^autologin-user=.*/autologin-user=$APP_USER/" \
        /etc/lightdm/lightdm.conf 2>/dev/null || true
fi

# ── Hardware watchdog ───────────────────────────────────────────────────
# The Pi's silicon watchdog, pointed at systemd. If systemd itself wedges,
# the board reboots. Under the software watchdog, not instead of it.
if ! grep -q "^dtparam=watchdog=on" /boot/firmware/config.txt 2>/dev/null; then
    blue "enabling the hardware watchdog"
    echo "dtparam=watchdog=on" >> /boot/firmware/config.txt 2>/dev/null || \
        echo "dtparam=watchdog=on" >> /boot/config.txt 2>/dev/null || true
fi
mkdir -p /etc/systemd/system.conf.d
cat > /etc/systemd/system.conf.d/adnova-watchdog.conf <<'EOF'
# If systemd stops petting the hardware watchdog, let the board reboot.
[Manager]
RuntimeWatchdogSec=15
RebootWatchdogSec=2min
EOF

# ── Services ────────────────────────────────────────────────────────────
blue "installing the services"
install -m 644 "$BASE/current/ops/adnova-player.service"  /etc/systemd/system/
install -m 644 "$BASE/current/ops/adnova-update.service"  /etc/systemd/system/
install -m 644 "$BASE/current/ops/adnova-update.timer"    /etc/systemd/system/

systemctl daemon-reload
systemctl enable --quiet adnova-player.service adnova-update.timer
systemctl restart adnova-player.service
systemctl start adnova-update.timer

# The kiosk browser is not a system service: on Pi OS Bookworm the display
# is a Wayland desktop that autologs in, and the browser has to start
# inside that session, not beside it. setup-kiosk.sh registers it in the
# desktop user's autostart.
blue "setting up the full-screen kiosk"
bash "$BASE/current/ops/setup-kiosk.sh"

sleep 3
if systemctl is-active --quiet adnova-player; then
    blue "the player is running"
    curl -fsS --max-time 5 http://127.0.0.1:8080/healthz && echo
else
    journalctl -u adnova-player -n 30 --no-pager
    die "the player did not start — see the log above"
fi

echo
blue "provisioned. Reboot to bring up the full-screen kiosk:"
echo "    sudo reboot"
echo
echo "  After reboot the TV shows the AdNova splash, then the schedule."
echo "  On-site status page:  http://<this-pi-ip>:8080/admin  (admin login)"
echo "  Service logs:         journalctl -u adnova-player -f"
