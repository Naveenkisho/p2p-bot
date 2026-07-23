# Deploy

The bot is one lightweight process (~150 MB RAM, near-zero CPU). It uses long
polling, so it needs **no inbound port** — the only thing that ever listens is
the optional web panel, and that stays on localhost behind nginx.

## 1. Put the code on the server

```bash
sudo useradd -r -m -d /opt/p2p-bot p2pbot        # optional dedicated user
sudo -u p2pbot git clone https://github.com/Naveenkisho/p2p-bot /opt/p2p-bot
cd /opt/p2p-bot
sudo -u p2pbot python3 -m venv .venv
sudo -u p2pbot ./.venv/bin/pip install -r requirements.txt
sudo -u p2pbot cp .env.example .env
sudo -u p2pbot nano .env      # fill in the values below
```

Minimum `.env`: `P2P_BOT_TOKEN`, `P2P_ADMIN_IDS`. For the web panel also set a
strong `P2P_PANEL_PASSWORD`. A free `P2P_TRONGRID_KEY` (from trongrid.io) is
recommended so the 10-second polling never hits the anonymous rate limit.

## 2. Run it as a service

```bash
sudo cp deploy/p2p-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now p2p-bot
sudo journalctl -u p2p-bot -f      # watch it start
```

`Restart=always` keeps it alive across crashes and reboots — and it's what
makes the panel's "change bot token" work: saving a new token restarts the
process, which reads the new token from the database on boot.

## 3. First-run config (from Telegram or the panel)

- `/setaddress T…` — your TRC20 deposit address (required; the desk stays
  closed until it's set).
- `/setrate CDM 91` etc. — one rate per service you offer.
- `/setsupport @help1 @help2` — support contacts shown to users.
- `/setchannel @yourchannel` — optional public proof channel (add the bot as
  a channel admin first).

## 4. Web panel (optional)

Set `P2P_PANEL_PASSWORD` in `.env` and restart. The panel listens on
`127.0.0.1:8088`. Expose it **only** through nginx + HTTPS:

```bash
sudo cp deploy/nginx-panel.conf /etc/nginx/sites-available/p2p-panel
# edit the server_name + cert paths, then:
sudo ln -s /etc/nginx/sites-available/p2p-panel /etc/nginx/sites-enabled/
sudo certbot --nginx -d panel.yourdomain.com
sudo nginx -t && sudo systemctl reload nginx
```

**Treat the panel like a bank login.** It can change the bot token and the
admin list. Use a long password, and — strongly recommended — restrict it to
your own IP with the `allow/deny` lines in the nginx config.

## 5. Back up the database

Everything lives in one SQLite file (`P2P_DB_PATH`, default
`/opt/p2p-bot/p2p.sqlite3`): orders, refund addresses, rates, the deposit
address, and (if changed via the panel) the bot token. Back it up:

```bash
# nightly cron
sqlite3 /opt/p2p-bot/p2p.sqlite3 ".backup /opt/p2p-bot/backups/p2p-$(date +\%F).sqlite3"
```

## Running alongside another app (e.g. ReelCaps)

Fully compatible. Different process, different database, its own systemd
service, and no inbound port for the bot itself — so it doesn't touch the other
app's files, ports, or nginx. If you also run the panel, just give it its own
nginx server block (a different `server_name` or port) from the other app.
