#!/usr/bin/env bash
# deploy.sh — idempotent setup for KalBot on Ubuntu 22.04
# Run as root: bash deploy.sh
set -euo pipefail

REPO_URL="${KALBOT_REPO_URL:-}"
DEPLOY_DIR="/opt/kalbot"
SERVICE_NAME="kalbot"
BOT_USER="kalbot"
ENV_FILE="${ENV_FILE:-.env}"  # local .env to copy, override with ENV_FILE=/path/to/.env

# ── 1. System deps ────────────────────────────────────────────────────────────
echo "[1/7] Installing system packages..."
apt-get update -qq
apt-get install -y -qq \
    python3.11 python3.11-venv python3.11-dev \
    sqlite3 curl git logrotate

# Install uv (idempotent — skips if already present at target version)
if ! command -v uv &>/dev/null; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.cargo/bin:$PATH"
fi

# ── 2. System user ────────────────────────────────────────────────────────────
echo "[2/7] Creating system user '$BOT_USER'..."
if ! id "$BOT_USER" &>/dev/null; then
    useradd --system --shell /usr/sbin/nologin --home "$DEPLOY_DIR" "$BOT_USER"
fi

# ── 3. Clone / update repo ────────────────────────────────────────────────────
echo "[3/7] Cloning/updating repo..."
if [[ -z "$REPO_URL" ]]; then
    echo "KALBOT_REPO_URL not set. Skipping git clone — copy files manually to $DEPLOY_DIR."
else
    if [[ -d "$DEPLOY_DIR/.git" ]]; then
        git -C "$DEPLOY_DIR" pull --ff-only
    else
        git clone "$REPO_URL" "$DEPLOY_DIR"
    fi
fi

mkdir -p "$DEPLOY_DIR"

# ── 4. Install Python deps ────────────────────────────────────────────────────
echo "[4/7] Installing Python dependencies..."
cd "$DEPLOY_DIR"
uv sync --no-dev 2>/dev/null || uv sync

# ── 5. Copy .env ──────────────────────────────────────────────────────────────
echo "[5/7] Copying .env..."
if [[ -f "$ENV_FILE" ]]; then
    install -o "$BOT_USER" -g "$BOT_USER" -m 600 "$ENV_FILE" "$DEPLOY_DIR/.env"
    echo "  Installed $ENV_FILE -> $DEPLOY_DIR/.env"
else
    echo "  WARNING: $ENV_FILE not found. Create $DEPLOY_DIR/.env manually before starting service."
fi

# Set ownership
chown -R "$BOT_USER:$BOT_USER" "$DEPLOY_DIR"

# ── 6. Install systemd service ────────────────────────────────────────────────
echo "[6/7] Installing systemd service..."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_SRC="$SCRIPT_DIR/../kalbot.service"

if [[ ! -f "$SERVICE_SRC" ]]; then
    echo "  ERROR: kalbot.service not found at $SERVICE_SRC"
    exit 1
fi

install -m 644 "$SERVICE_SRC" "/etc/systemd/system/$SERVICE_NAME.service"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"

# ── 7. Log rotation ───────────────────────────────────────────────────────────
echo "[7/7] Configuring log rotation..."
cat > "/etc/logrotate.d/$SERVICE_NAME" <<'EOF'
/var/log/kalbot/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    create 0640 kalbot kalbot
    postrotate
        systemctl kill --signal=USR1 kalbot.service 2>/dev/null || true
    endscript
}
EOF

mkdir -p /var/log/kalbot
chown "$BOT_USER:$BOT_USER" /var/log/kalbot

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "Setup complete."
echo ""
echo "Next steps:"
echo "  1. Verify $DEPLOY_DIR/.env has CLOB_API_KEY, PK, etc."
echo "  2. For live trading: configure WireGuard (see plans/DEPLOY.md)"
echo "  3. Start bot: systemctl start $SERVICE_NAME"
echo "  4. Check logs: journalctl -u $SERVICE_NAME -f"
