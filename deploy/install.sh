#!/usr/bin/env bash
# One-shot installer for the P2P desk bot.
#
#   git clone https://github.com/Naveenkisho/p2p-bot /opt/p2p-bot
#   cd /opt/p2p-bot
#   sudo bash deploy/install.sh
#
# Idempotent: safe to re-run to update (git pull + reinstall + restart).
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Please run with sudo:  sudo bash deploy/install.sh" >&2
  exit 1
fi

# Repo root = parent of this script's dir
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="${SUDO_USER:-root}"
SERVICE=/etc/systemd/system/p2p-bot.service

echo "==> Installing P2P bot in $APP_DIR (service user: $RUN_USER)"

command -v python3 >/dev/null || { echo "python3 not found — install it first." >&2; exit 1; }

echo "==> Python virtualenv + dependencies"
if [[ ! -d "$APP_DIR/.venv" ]]; then
  python3 -m venv "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

# ---- .env ----
if [[ ! -f "$APP_DIR/.env" ]]; then
  echo "==> First-time setup — a few values (leave panel password blank to skip the web panel)"
  read -rp "  Bot token (from @BotFather): " BOT_TOKEN
  read -rp "  Admin Telegram IDs (space/comma separated): " ADMIN_IDS
  read -rp "  Web panel password (blank = panel off): " PANEL_PW
  read -rp "  TronGrid API key (optional, blank = none): " TRON_KEY
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  # fill values (using | as sed delimiter since tokens contain no |)
  sed -i "s|^P2P_BOT_TOKEN=.*|P2P_BOT_TOKEN=${BOT_TOKEN}|" "$APP_DIR/.env"
  sed -i "s|^P2P_ADMIN_IDS=.*|P2P_ADMIN_IDS=${ADMIN_IDS}|" "$APP_DIR/.env"
  sed -i "s|^P2P_PANEL_PASSWORD=.*|P2P_PANEL_PASSWORD=${PANEL_PW}|" "$APP_DIR/.env"
  [[ -n "$TRON_KEY" ]] && sed -i "s|^#\?P2P_TRONGRID_KEY=.*|P2P_TRONGRID_KEY=${TRON_KEY}|" "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
  echo "  Wrote $APP_DIR/.env (chmod 600)"
else
  echo "==> Keeping existing $APP_DIR/.env"
fi

chown -R "$RUN_USER":"$RUN_USER" "$APP_DIR"

# ---- systemd service ----
echo "==> Installing systemd service"
sed -e "s|^User=.*|User=${RUN_USER}|" \
    -e "s|^WorkingDirectory=.*|WorkingDirectory=${APP_DIR}|" \
    -e "s|^EnvironmentFile=.*|EnvironmentFile=${APP_DIR}/.env|" \
    -e "s|^ExecStart=.*|ExecStart=${APP_DIR}/.venv/bin/python -m bot.main|" \
    "$APP_DIR/deploy/p2p-bot.service" > "$SERVICE"

systemctl daemon-reload
systemctl enable p2p-bot >/dev/null 2>&1 || true
systemctl restart p2p-bot

sleep 2
echo "==> Status:"
systemctl --no-pager --lines=0 status p2p-bot || true
echo ""
echo "✅ Done. Watch logs with:  journalctl -u p2p-bot -f"
echo "   Then in Telegram, message your bot /start, and as an admin run:"
echo "     /setaddress T...      (your TRC20 deposit address)"
echo "     /setrate CDM 91       (a rate per service)"
echo "     /setsupport @help     (support contact)"
echo "   The web panel (if you set a password) is on 127.0.0.1:8088 — put nginx+TLS"
echo "   in front (see DEPLOY.md) and restrict it to your IP before exposing it."
