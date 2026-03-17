#!/usr/bin/env bash
# Grand Prix Alpha-Scalp — Auto-update script
# Called by cron every 5 minutes on the VPS.
#
# What it does:
#   1. Checks if new commits exist on origin/main
#   2. If yes: checks for open positions (safety — never restart mid-trade)
#   3. If safe: git pull → restart (or rebuild if requirements.txt changed)
#   4. Logs every action with timestamp
#
# Cron entry (installed by setup_vps.sh):
#   */5 * * * * /opt/alpha-scalp/scripts/auto_update.sh >> /var/log/alpha-scalp-update.log 2>&1

set -euo pipefail

DEPLOY_DIR="/opt/alpha-scalp"
CONTAINER="alpha-scalp-bot"

# ─── Helpers ─────────────────────────────────────────────────────────────────
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

# ─── Check: is there anything new on origin/main? ────────────────────────────
cd "$DEPLOY_DIR"

git fetch origin main --quiet

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" = "$REMOTE" ]; then
    # Nothing to do — exit silently (don't spam the log)
    exit 0
fi

log "New commit detected: $LOCAL → $REMOTE"
log "Changed files:"
git diff --name-only "$LOCAL" "$REMOTE" | sed 's/^/  /'

# ─── Safety check: don't restart while a trade is open ───────────────────────
OPEN_POSITIONS=0
if docker ps --filter "name=$CONTAINER" --filter "status=running" -q | grep -q .; then
    # Read open positions from bot_state.json inside the container
    OPEN_POSITIONS=$(docker exec "$CONTAINER" python -c "
import json, os, sys
f = 'bot_state.json'
if not os.path.exists(f):
    sys.exit(0)
state = json.load(open(f))
positions = state.get('open_positions', {})
print(len(positions))
" 2>/dev/null || echo "0")
fi

if [ "$OPEN_POSITIONS" -gt 0 ]; then
    log "WARNING: $OPEN_POSITIONS open position(s) detected — deferring update to avoid mid-trade restart."
    log "Will retry in 5 minutes."
    exit 0
fi

# ─── Check if requirements.txt changed (needs full image rebuild) ─────────────
NEEDS_REBUILD=false
if git diff --name-only "$LOCAL" "$REMOTE" | grep -q "requirements.txt"; then
    log "requirements.txt changed — full image rebuild required."
    NEEDS_REBUILD=true
fi

# ─── Pull new code ────────────────────────────────────────────────────────────
log "Pulling latest code..."
git pull origin main --quiet
NEW_HASH=$(git rev-parse --short HEAD)
log "Updated to $NEW_HASH"

# ─── Restart (or rebuild + restart) ──────────────────────────────────────────
if [ "$NEEDS_REBUILD" = true ]; then
    log "Rebuilding Docker image (requirements changed)..."
    docker compose build --quiet
    log "Rebuild complete."
fi

log "Restarting container..."
docker compose up -d

# Give the container 10 seconds to start, then confirm it's running
sleep 10
if docker ps --filter "name=$CONTAINER" --filter "status=running" -q | grep -q .; then
    log "Bot restarted successfully — running on commit $NEW_HASH"
else
    log "ERROR: Container failed to start after update! Check: docker logs $CONTAINER"
    exit 1
fi
