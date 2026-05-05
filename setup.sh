#!/bin/bash
# SmsSaaS first-time setup script
# Run on the gateway (192.168.4.25) as root or sudo
# Usage: bash setup.sh

set -euo pipefail
echo "=== SmsSaaS Setup ==="

INSTALL_DIR="/opt/smssaas"
API_PORT=8300

# ── 1. Install system dependencies ──────────────────────────────────────────
apt-get update -qq
apt-get install -y --no-install-recommends \
    python3.12 python3.12-venv python3-pip \
    android-tools-adb \
    ffmpeg espeak-ng libespeak-ng1 \
    curl sqlite3

# ── 2. Copy files ─────────────────────────────────────────────────────────────
mkdir -p "$INSTALL_DIR/data/recordings" \
         "$INSTALL_DIR/data/voicemail" \
         "$INSTALL_DIR/data/tts_cache"

cp -r api/   "$INSTALL_DIR/api/"
cp -r adb-bridge/ "$INSTALL_DIR/adb-bridge/"
chmod +x "$INSTALL_DIR/adb-bridge/watcher.sh"

# ── 3. Python virtualenv ──────────────────────────────────────────────────────
python3.12 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --quiet -r "$INSTALL_DIR/api/requirements.txt"

# ── 4. .env file ─────────────────────────────────────────────────────────────
if [[ ! -f "$INSTALL_DIR/.env" ]]; then
    cp .env.example "$INSTALL_DIR/.env"
    echo ""
    echo ">>> IMPORTANT: edit $INSTALL_DIR/.env and set:"
    echo "    ADMIN_KEY=<strong random key>"
    echo "    STRIPE_SECRET_KEY=sk_live_..."
    echo "    STRIPE_WEBHOOK_SECRET=whsec_..."
    echo ""
fi

# ── 5. Systemd services ───────────────────────────────────────────────────────
cp smssaas.service     "$INSTALL_DIR/smssaas.service"
cp smssaas-adb.service "$INSTALL_DIR/smssaas-adb.service"

systemctl link "$INSTALL_DIR/smssaas.service"
systemctl link "$INSTALL_DIR/smssaas-adb.service"
systemctl daemon-reload
systemctl enable smssaas smssaas-adb
systemctl restart smssaas smssaas-adb

# ── 6. nginx ──────────────────────────────────────────────────────────────────
echo ""
echo ">>> To expose the API via HTTPS, copy nginx-smssaas.conf to"
echo "    /etc/nginx/sites-enabled/smssaas.conf"
echo "    and edit the server_name + SSL cert paths, then: nginx -s reload"

# ── 7. ADB udev rule ──────────────────────────────────────────────────────────
if [[ ! -f /etc/udev/rules.d/99-android-adb.rules ]]; then
    cat > /etc/udev/rules.d/99-android-adb.rules <<'EOF'
# Allow ADB access for all Android devices (Google Pixel vendor 18d1)
SUBSYSTEM=="usb", ATTR{idVendor}=="18d1", MODE="0666", GROUP="plugdev"
EOF
    udevadm control --reload-rules
    echo ">>> udev rule installed for Google Pixel (vendor 18d1)"
fi

# ── 8. Stripe webhook ─────────────────────────────────────────────────────────
echo ""
echo ">>> Stripe webhook endpoint to register in Stripe Dashboard:"
echo "    https://sms.YOUR_DOMAIN.com/billing/webhook"
echo "    Events to enable: customer.subscription.*, invoice.payment_*"

echo ""
echo "=== Setup complete ==="
echo "API: http://127.0.0.1:$API_PORT"
echo "Check status: systemctl status smssaas smssaas-adb"
echo "Logs:         journalctl -u smssaas -f"
