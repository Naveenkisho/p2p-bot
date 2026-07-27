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

### Reaching the panel at the server IP:port (no domain)

If you'd rather open it at `https://<server-ip>:8088` without a domain/nginx,
serve it over a self-signed cert and lock the port to your own IP.

1. Generate a self-signed cert (once):

   ```bash
   openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
     -keyout /opt/p2p-bot/panel.key -out /opt/p2p-bot/panel.crt \
     -subj "/CN=<server-ip>"
   ```

2. In `.env`:

   ```
   P2P_PANEL_HOST=0.0.0.0
   P2P_PANEL_PORT=8088
   P2P_PANEL_TLS_CERT=/opt/p2p-bot/panel.crt
   P2P_PANEL_TLS_KEY=/opt/p2p-bot/panel.key
   ```

3. **Firewall — allow only your own IP** (find it at whatismyipaddress.com):

   ```bash
   ufw allow from <your-ip> to any port 8088 proto tcp
   ufw deny 8088
   ufw reload           # (ensure ufw is enabled: `ufw enable`)
   ```

4. `sudo systemctl restart p2p-bot`, then open `https://<server-ip>:8088`.
   Your browser will warn about the self-signed cert once — that's expected;
   click through (Advanced → proceed). The connection is still encrypted, so
   your password and the bot token are protected in transit.

Without the firewall rule, a token-changing panel would be exposed to the whole
internet — don't skip step 3.

## 5. Public customer website

The website is the same desk with a second face on it — same process, same
database, same admin panel. Nothing to install and no second service: it starts
with the bot on `127.0.0.1:8090` and stays invisible until you put a proxy in
front of it. Web customers appear in the panel tagged `🌐 web`, and the rates,
deposit address, QR and deposit window you set there govern both interfaces.

### The short way: one script

After 5.1 (ship the code) and 5.2 (point the domain), one script does sections
5.3–5.6 for you — installs nginx, writes both configs with your domain filled
in, takes the pasted Cloudflare origin certificate, locks the firewall to
Cloudflare, and prints the exact dashboard settings and verification commands:

```bash
cd /opt/p2p-bot && sudo bash deploy/setup-site.sh yourdomain.com
```

It backs up anything it replaces and is safe to re-run. The sections below are
the same steps by hand, if you prefer to see each one.

### 5.1 Ship the code

```bash
cd /opt/p2p-bot
sudo -u p2pbot git pull
sudo systemctl restart p2p-bot
sudo systemctl is-active p2p-bot          # must print "active"
curl -sI http://127.0.0.1:8090/ | head -1 # must print HTTP/1.1 200 OK
```

The database migrates itself on that restart — new columns and their index are
added in place and existing orders are untouched. Re-running is harmless.

**Do the QR step too**, unless you upload your own QR images in the panel:

```bash
sudo -u p2pbot /opt/p2p-bot/.venv/bin/pip install -r requirements.txt
sudo systemctl restart p2p-bot
```

Without `qrcode`/`Pillow` the site still works and still shows the deposit
address, but there is no QR picture to scan — on a page you are paying ads to
reach, that costs conversions.

### 5.2 Point a domain at it

One canonical hostname (`example.com` **or** `www.example.com`, not both — the
customer's session cookie is host-only and does not follow a redirect between
them). In Cloudflare DNS: an `A` record to the server's IP.

### 5.3 Behind Cloudflare (orange cloud)

Cloudflare makes the site faster and absorbs junk traffic, but three settings
are not optional. Skip them and the failures are silent and expensive.

**Install the Cloudflare support file first — HTTP level, not inside a vhost:**

```bash
sudo cp deploy/cloudflare-realip.conf /etc/nginx/conf.d/cloudflare.conf
```

It restores the real visitor IP (otherwise **every customer shares one
rate-limit bucket** and the site starts refusing real buyers as if they were one
abuser) and it tells the app which scheme the visitor actually used (otherwise
anyone arriving on `http://` gets an identity cookie the browser throws away,
and is stuck on "please enable cookies" forever).

**In the Cloudflare dashboard:**

| Setting | Value | Why |
|---|---|---|
| SSL/TLS → Overview | **Full (strict)** | "Flexible" strips the cookie's Secure flag and causes a redirect loop |
| SSL/TLS → Edge Certificates → Always Use HTTPS | **On** | this is the redirect that actually runs; the origin's is bypassed |
| SSL/TLS → Origin Server | **Create Certificate** | free, 15 years, no renewal cron — install at the paths in `nginx-site.conf` |
| Speed → Optimization → **Rocket Loader** | **Off** | it defers inline scripts and breaks the Copy-address and "I've sent it" buttons |
| Security → **Bot Fight Mode** | Off, or exclude `/o/*` | it challenges the order page's background status polling, which then silently stops updating |
| Caching | leave default | never add "Cache Everything" — it would serve one customer's order page, with their deposit amount, to another |

### 5.4 nginx

```bash
sudo apt install nginx
sudo cp deploy/nginx-site.conf /etc/nginx/sites-available/p2p-site
sudo nano /etc/nginx/sites-available/p2p-site      # server_name + cert paths
sudo ln -s /etc/nginx/sites-available/p2p-site /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

Edit the copy under `/etc/nginx`, never the one in the git tree — a modified
tracked file makes the next `git pull` refuse to merge, and you would restart
into the old code believing you had updated.

### 5.5 Lock the origin to Cloudflare

Otherwise anyone who finds the server's IP reaches the site directly and every
Cloudflare protection — WAF, rate limiting, DDoS — is bypassed.

```bash
sudo ufw allow OpenSSH
# one curl per list + echo: the endpoints have no trailing newline, and a
# combined fetch glues the last v4 range onto the first v6 range
for ip in $({ curl -fsS https://www.cloudflare.com/ips-v4/; echo; \
              curl -fsS https://www.cloudflare.com/ips-v6/; echo; }); do
  sudo ufw allow from $ip to any port 443 proto tcp
done
sudo ufw default deny incoming
sudo ufw enable
```

Ports **8090** (site) and **8088** (panel) must never be open to the internet —
both bind to localhost by default, and the firewall is your second lock.

### 5.6 Verify it actually works

```bash
# 1. the site answers over HTTPS
curl -sI https://example.com/ | head -1

# 2. security headers arrive through the proxy
curl -sI https://example.com/ | grep -iE 'x-frame|x-content|strict-transport'

# 3. the identity cookie is Secure — check /sell, NOT the landing page
#    (the landing page never issues a cookie, so it would "pass" while broken)
curl -sI https://example.com/sell | grep -i set-cookie
#    → expect: HttpOnly; Secure; SameSite=Lax

# 4. the panel is NOT public
curl -sI --max-time 5 http://<server-ip>:8088/   # must fail/time out
```

Then place one real test order and check the log — this is the only way to see
whether Cloudflare's real IP is arriving:

```bash
sudo journalctl -u p2p-bot | grep "web order"
```

If your test orders show **your own IP**, the setup is correct. If they show a
Cloudflare address (or every order shows the same one), `cloudflare.conf` is not
loaded and your customers are sharing one rate-limit bucket.

### 5.7 If something is wrong

```bash
sudo journalctl -u p2p-bot -n 50        # a config typo shows as a raw traceback
```

To take the site down without touching the bot, put `P2P_SITE_PORT=0` in `.env`
(literally `0` — leaving it blank crashes the process on boot) and restart.

## 6. Back up the database

Everything lives in one SQLite file (`P2P_DB_PATH`, default
`/opt/p2p-bot/p2p.sqlite3`): orders, refund addresses, rates, the deposit
address, and (if changed via the panel) the bot token. Back it up:

```bash
# nightly cron
sqlite3 /opt/p2p-bot/p2p.sqlite3 ".backup /opt/p2p-bot/backups/p2p-$(date +\%F).sqlite3"
```

## 7. Running alongside another app (e.g. ReelCaps)

Fully compatible. Different process, different database, its own systemd
service, and no inbound port for the bot itself — so it doesn't touch the other
app's files, ports, or nginx. If you also run the panel, just give it its own
nginx server block (a different `server_name` or port) from the other app.
