#!/usr/bin/env bash
# Grand Prix Alpha-Scalp — One-time VPS setup script
# Run this ONCE on a fresh Ubuntu 22.04 / 24.04 VPS as root or sudo user.
#
# What it does:
#   1. Installs Docker + docker compose plugin
#   2. Clones the private GitHub repo (requires SSH key already set up)
#   3. Prompts you to create .env from the template
#   4. Builds the Docker image and starts the bot
#   5. Installs the auto-update cron job (runs every 5 minutes)
#
# Usage:
#   chmod +x setup_vps.sh
#   ./setup_vps.sh

set -euo pipefail

# ─── Config — edit these ──────────────────────────────────────────────────────
REPO_URL="git@github.com:DevSiddh/alpha-scalp-bot.git"   # your private repo SSH URL
DEPLOY_DIR="/opt/alpha-scalp"
LOG_FILE="/var/log/alpha-scalp-update.log"
CRON_INTERVAL="*/5 * * * *"   # every 5 minutes
# ─────────────────────────────────────────────────────────────────────────────

echo "═══════════════════════════════════════════════════════"
echo "  Grand Prix Alpha-Scalp — VPS Setup"
echo "═══════════════════════════════════════════════════════"

# ── Step 1: Install Docker ────────────────────────────────────────────────────
if ! command -v docker &>/dev/null; then
    echo "[1/5] Installing Docker..."
    apt-get update -q
    apt-get install -y -q ca-certificates curl gnupg lsb-release
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
    echo \
      "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
      https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
      > /etc/apt/sources.list.d/docker.list
    apt-get update -q
    apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-compose-plugin
    echo "[1/5] Docker installed: $(docker --version)"
else
    echo "[1/5] Docker already installed: $(docker --version)"
fi

# ── Step 2: Clone the repo ────────────────────────────────────────────────────
echo "[2/5] Cloning repo to $DEPLOY_DIR..."
echo ""
echo "  IMPORTANT: Your VPS SSH public key must be added to GitHub first."
echo "  Generate one if needed: ssh-keygen -t ed25519 -C 'vps-alpha-scalp'"
echo "  Then add ~/.ssh/id_ed25519.pub to: GitHub → Settings → SSH Keys"
echo ""
read -r -p "  Press ENTER when SSH key is set up..."

if [ -d "$DEPLOY_DIR/.git" ]; then
    echo "  Repo already exists at $DEPLOY_DIR, skipping clone."
else
    git clone "$REPO_URL" "$DEPLOY_DIR"
fi
cd "$DEPLOY_DIR"

# ── Step 3: Create .env ───────────────────────────────────────────────────────
echo "[3/5] Setting up .env..."
if [ -f "$DEPLOY_DIR/.env" ]; then
    echo "  .env already exists — skipping."
else
    echo ""
    echo "  Fill in your API keys below."
    echo "  (Tip: open a second terminal, edit $DEPLOY_DIR/.env, then return here)"
    echo ""
    cat > "$DEPLOY_DIR/.env" << 'ENVTEMPLATE'
# ── Exchange ──────────────────────────────────────────────────────
BINANCE_API_KEY=
BINANCE_SECRET=
BINANCE_DEMO_TRADING=true      # set false for real money

# ── Trading ───────────────────────────────────────────────────────
SCALP_SYMBOLS=BTC/USDT
PASSIVE_SHADOW_SYMBOLS=ETH/USDT,SOL/USDT
TIMEFRAME=3m
LEVERAGE=5
GRAND_PRIX_ENABLED=true
USE_WEBSOCKET=true

# ── Risk ──────────────────────────────────────────────────────────
RISK_PER_TRADE=0.02
DAILY_DRAWDOWN_LIMIT=0.03
EQUITY_FLOOR_PCT=0.80
MAX_OPEN_POSITIONS=3
INITIAL_BALANCE=100

# ── Telegram ──────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# ── LLM (DeepSeek) ────────────────────────────────────────────────
LLM_API_KEY=
LLM_API_URL=https://api.deepseek.com/v1/chat/completions
LLM_MODEL=deepseek-chat
PITBOSS_ENABLED=true

# ── Paper trading ─────────────────────────────────────────────────
PAPER_TRADING_MODE=false
PAPER_SLIPPAGE_MODEL=realistic

# ── All other keys: copy from CLAUDE.md .env section ─────────────
ENVTEMPLATE
    echo "  Created .env template at $DEPLOY_DIR/.env"
    echo ""
    read -r -p "  Edit .env now, then press ENTER to continue..."
fi

# ── Step 4: Build image and start ────────────────────────────────────────────
echo "[4/5] Building Docker image and starting bot..."
cd "$DEPLOY_DIR"
docker compose build
docker compose up -d
echo "  Bot started. Check logs: docker logs -f alpha-scalp-bot"

# ── Step 5: Install cron auto-update job ─────────────────────────────────────
echo "[5/5] Installing auto-update cron job (every 5 minutes)..."
touch "$LOG_FILE"
chmod 644 "$LOG_FILE"

CRON_JOB="$CRON_INTERVAL $DEPLOY_DIR/scripts/auto_update.sh >> $LOG_FILE 2>&1"
# Add cron only if not already present
if crontab -l 2>/dev/null | grep -qF "auto_update.sh"; then
    echo "  Cron job already installed."
else
    (crontab -l 2>/dev/null; echo "$CRON_JOB") | crontab -
    echo "  Cron job installed: $CRON_JOB"
fi

echo ""
echo "═══════════════════════════════════════════════════════"
echo "  Setup complete!"
echo ""
echo "  Bot directory : $DEPLOY_DIR"
echo "  Update log    : $LOG_FILE"
echo "  Live logs     : docker logs -f alpha-scalp-bot"
echo "  Status        : docker ps"
echo "  Health        : docker inspect --format='{{.State.Health.Status}}' alpha-scalp-bot"
echo ""
echo "  Auto-update   : every 5 min via cron"
echo "  To update now : $DEPLOY_DIR/scripts/auto_update.sh"
echo "═══════════════════════════════════════════════════════"
