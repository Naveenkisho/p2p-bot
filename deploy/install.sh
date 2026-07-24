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
# remove a broken/partial venv (e.g. created before python3-venv was installed)
if [[ -d "$APP_DIR/.venv" && ! -x "$APP_DIR/.venv/bin/pip" ]]; then
  echo "  (removing incomplete .venv)"
  rm -rf "$APP_DIR/.venv"
fi
if [[ ! -d "$APP_DIR/.venv" ]]; then
  if ! python3 -m venv "$APP_DIR/.venv" 2>/dev/null; then
    echo "  venv module missing — installing python3-venv + pip…"
    apt-get update -qq && apt-get install -y -qq python3-venv python3-pip || {
      echo "  Could not auto-install. Run: apt install -y python3-venv python3-pip" >&2
      exit 1; }
    rm -rf "$APP_DIR/.venv"
    python3 -m venv "$APP_DIR/.venv"
  fi
fi
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

# ---- .env ----
if [[ ! -f "$APP_DIR/.env" ]]; then
  echo "==> First-time setup — a few values (leave panel password blank to skip the web panel)"
  read -rp "  Bot token (from @BotFather): " BOT_TOKEN
  echo "  (You can leave the bot token and admin IDs blank and set them later"
  echo "   in the web panel — the panel will boot first.)"
  read -rp "  Bot token (blank = set it in the panel later): " BOT_TOKEN
  read -rp "  Admin Telegram IDs (space/comma separated, blank ok): " ADMIN_IDS
  read -rp "  Web panel password (blank = panel off): " PANEL_PW
  read -rp "  TronGrid API key (optional, blank = none): " TRON_KEY
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  # fill values (using | as sed delimiter since tokens contain no |)
  sed -i "s|^P2P_BOT_TOKEN=.*|P2P_BOT_TOKEN=${BOT_TOKEN}|" "$APP_DIR/.env"
  sed -i "s|^P2P_ADMIN_IDS=.*|P2P_ADMIN_IDS=${ADMIN_IDS}|" "$APP_DIR/.env"
  sed -i "s|^P2P_PANEL_PASSWORD=.*|P2P_PANEL_PASSWORD=${PANEL_PW}|" "$APP_DIR/.env"
  [[ -n "$TRON_KEY" ]] && sed -i "s|^#\?P2P_TRONGRID_KEY=.*|P2P_TRONGRID_KEY=${TRON_KEY}|" "$APP_DIR/.env"

  # Optionally expose the panel at the server IP over self-signed HTTPS
  if [[ -n "$PANEL_PW" ]]; then
    read -rp "  Open the web panel at https://<server-ip>:8088 ? [y/N]: " EXPOSE
    if [[ "$EXPOSE" =~ ^[Yy] ]]; then
      SRV_IP="$(curl -fsS https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')"
      if command -v openssl >/dev/null && [[ ! -f "$APP_DIR/panel.crt" ]]; then
        openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
          -keyout "$APP_DIR/panel.key" -out "$APP_DIR/panel.crt" \
          -subj "/CN=${SRV_IP:-panel}" >/dev/null 2>&1
      fi
      sed -i "s|^P2P_PANEL_HOST=.*|P2P_PANEL_HOST=0.0.0.0|" "$APP_DIR/.env"
      sed -i "s|^P2P_PANEL_TLS_CERT=.*|P2P_PANEL_TLS_CERT=${APP_DIR}/panel.crt|" "$APP_DIR/.env"
      sed -i "s|^P2P_PANEL_TLS_KEY=.*|P2P_PANEL_TLS_KEY=${APP_DIR}/panel.key|" "$APP_DIR/.env"
      EXPOSED=1; PANEL_URL="https://${SRV_IP:-<server-ip>}:8088"
    fi
  fi
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
if [[ "${EXPOSED:-0}" == "1" ]]; then
  echo ""
  echo "🔐 IMPORTANT — lock the panel port to YOUR ip so it isn't open to everyone."
  echo "   Find your ip at whatismyipaddress.com, then run (keeps SSH working):"
  echo "     ufw allow OpenSSH"
  echo "     ufw allow from <YOUR-IP> to any port 8088 proto tcp"
  echo "     ufw deny 8088"
  echo "     ufw --force enable"
  echo ""
  echo "🌐 Then open the panel:  ${PANEL_URL:-https://<server-ip>:8088}"
  echo "   (Your browser warns once about the self-signed cert — click Advanced →"
  echo "    proceed. The connection is still encrypted.)"
  echo "   Log in, open Settings, and set the bot token + admin IDs there."
else
  echo "   The web panel (if you set a password) is on 127.0.0.1:8088 — reach it via"
  echo "   an SSH tunnel or put nginx+TLS in front (see DEPLOY.md)."
fi
