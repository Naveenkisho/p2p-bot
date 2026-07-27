#!/usr/bin/env bash
# One-shot setup for the public customer website behind Cloudflare.
#
#   sudo bash deploy/setup-site.sh yourdomain.com
#
# Installs nginx, writes both config files with your domain filled in, takes the
# Cloudflare origin certificate, locks the origin to Cloudflare, and verifies the
# result. Safe to re-run: it backs up anything it replaces and never touches the
# bot, its database, or the admin panel.
set -euo pipefail

APP_PORT=8090
PANEL_PORT=8088
SITE_AVAIL=/etc/nginx/sites-available/p2p-site
SITE_LINK=/etc/nginx/sites-enabled/p2p-site
CF_CONF=/etc/nginx/conf.d/cloudflare.conf
CERT_DIR=/etc/ssl/cloudflare
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STAMP="$(date +%Y%m%d-%H%M%S)"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\n\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

ask() {   # ask "question" -> 0 if yes
    local reply
    read -rp "  $1 [y/N]: " reply </dev/tty
    [[ "$reply" =~ ^[Yy]$ ]]
}

backup() { [[ -e "$1" ]] && cp -a "$1" "$1.bak-$STAMP" && warn "backed up $1 → $1.bak-$STAMP"; return 0; }

# ── 0. sanity ────────────────────────────────────────────────────────────────
[[ $EUID -eq 0 ]] || die "Run with sudo: sudo bash deploy/setup-site.sh yourdomain.com"

DOMAIN="${1:-}"
[[ -n "$DOMAIN" ]] || read -rp "  Your domain (e.g. example.com, no https://): " DOMAIN </dev/tty
DOMAIN="${DOMAIN#http://}"; DOMAIN="${DOMAIN#https://}"; DOMAIN="${DOMAIN%%/*}"
[[ "$DOMAIN" =~ ^[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?)+$ ]] \
    || die "'$DOMAIN' doesn't look like a domain name."

[[ -f "$HERE/nginx-site.conf" && -f "$HERE/cloudflare-realip.conf" ]] \
    || die "Run this from the repo (deploy/nginx-site.conf not found next to the script)."

bold "Setting up https://$DOMAIN"
echo

# ── 1. is the site actually running? ─────────────────────────────────────────
bold "1. Checking the app"
if ! systemctl is-active --quiet p2p-bot; then
    warn "the p2p-bot service is not active — start it first: systemctl start p2p-bot"
    ask "Continue anyway?" || die "Stopped. Nothing was changed."
elif curl -fsS --max-time 5 -o /dev/null "http://127.0.0.1:$APP_PORT/"; then
    ok "website answering on 127.0.0.1:$APP_PORT"
else
    warn "nothing answering on 127.0.0.1:$APP_PORT."
    echo "     The site starts with the bot. If you just merged, deploy first:"
    echo "       cd /opt/p2p-bot && git pull && systemctl restart p2p-bot"
    echo "     Check for errors with: journalctl -u p2p-bot -n 30"
    ask "Continue anyway (nginx will 502 until the app is up)?" \
        || die "Stopped. Nothing was changed."
fi

# QR libraries — without them the order page shows no QR to scan
if [[ -x /opt/p2p-bot/.venv/bin/python ]] \
   && ! /opt/p2p-bot/.venv/bin/python -c 'import qrcode' 2>/dev/null; then
    warn "the QR library isn't installed — customers would see no QR code."
    if ask "Install it now (pip install -r requirements.txt)?"; then
        (cd /opt/p2p-bot && sudo -u "$(stat -c %U /opt/p2p-bot)" \
            ./.venv/bin/pip install -q -r requirements.txt) \
            && systemctl restart p2p-bot && ok "QR library installed, service restarted"
    fi
fi
echo

# ── 2. nginx ─────────────────────────────────────────────────────────────────
bold "2. nginx"
if ! command -v nginx >/dev/null; then
    apt-get update -qq && apt-get install -y -qq nginx
fi
ok "nginx $(nginx -v 2>&1 | sed 's|.*/||')"

# Cloudflare real-IP + visitor-scheme, at HTTP level so the panel vhost gets it too
backup "$CF_CONF"
cf_ranges() {
    # One curl per list with a forced newline after each: the endpoints return
    # no trailing newline, so fetching both in one call glues the last v4 range
    # to the first v6 range ("131.0.72.0/222400:cb00::/32") and nginx -t dies.
    { curl -fsS --max-time 10 https://www.cloudflare.com/ips-v4/ && echo
      curl -fsS --max-time 10 https://www.cloudflare.com/ips-v6/ && echo
    } 2>/dev/null | sed '/^[[:space:]]*$/d'
}
if RANGES="$(cf_ranges)" \
   && [[ -n "$RANGES" ]]; then
    { sed 's|^|set_real_ip_from |; s|$|;|' <<<"$RANGES"
      echo 'real_ip_header CF-Connecting-IP;'
      sed -n '/^map \$http_cf_visitor/,$p' "$HERE/cloudflare-realip.conf"
    } > "$CF_CONF"
    ok "Cloudflare ranges fetched live ($(wc -l <<<"$RANGES") ranges)"
else
    cp "$HERE/cloudflare-realip.conf" "$CF_CONF"
    warn "couldn't reach cloudflare.com — used the bundled range list (may be stale)"
fi

# the site vhost, with the domain substituted in
backup "$SITE_AVAIL"
sed -e "s|server_name example\.com;|server_name $DOMAIN;|" \
    "$HERE/nginx-site.conf" > "$SITE_AVAIL"
ln -sfn "$SITE_AVAIL" "$SITE_LINK"
ok "wrote $SITE_AVAIL for $DOMAIN"

if [[ -e /etc/nginx/sites-enabled/default ]]; then
    rm -f /etc/nginx/sites-enabled/default
    ok "removed nginx's default site (it would answer on the bare IP)"
fi
echo

# ── 3. certificate ───────────────────────────────────────────────────────────
bold "3. Cloudflare origin certificate"
mkdir -p "$CERT_DIR"; chmod 700 "$CERT_DIR"

read_pem() {  # read_pem <outfile> <END marker>
    local out="$1" end="$2" line
    : > "$out"
    while IFS= read -r line </dev/tty; do
        printf '%s\n' "$line" >> "$out"
        [[ "$line" == *"$end"* ]] && break
    done
}

if [[ -s "$CERT_DIR/origin.pem" && -s "$CERT_DIR/origin.key" ]]; then
    ok "certificate already present in $CERT_DIR"
else
    echo "  In Cloudflare: SSL/TLS → Origin Server → Create Certificate → Create."
    echo "  You get two blocks. Paste each one INCLUDING its BEGIN and END lines."
    echo
    echo "  Paste the ORIGIN CERTIFICATE now:"
    read_pem "$CERT_DIR/origin.pem" "END CERTIFICATE"
    echo "  Paste the PRIVATE KEY now:"
    read_pem "$CERT_DIR/origin.key" "END PRIVATE KEY"
    chmod 644 "$CERT_DIR/origin.pem"; chmod 600 "$CERT_DIR/origin.key"
    openssl x509 -in "$CERT_DIR/origin.pem" -noout >/dev/null 2>&1 \
        || die "that doesn't parse as a certificate — re-run and paste it again."
    openssl pkey -in "$CERT_DIR/origin.key" -noout >/dev/null 2>&1 \
        || die "that doesn't parse as a private key — re-run and paste it again."
    ok "certificate and key stored in $CERT_DIR"
fi
echo

# ── 4. test + reload ─────────────────────────────────────────────────────────
bold "4. Applying the config"
if ! nginx -t 2>/tmp/nginx-t.$$; then
    cat /tmp/nginx-t.$$ >&2; rm -f /tmp/nginx-t.$$
    rm -f "$SITE_LINK"
    die "nginx rejected the config — the site was NOT enabled, nginx untouched."
fi
rm -f /tmp/nginx-t.$$
systemctl reload nginx 2>/dev/null || systemctl start nginx
ok "nginx reloaded"
echo

# ── 5. firewall ──────────────────────────────────────────────────────────────
bold "5. Firewall — lock the origin to Cloudflare"
echo "  Without this, anyone who finds your server's IP bypasses Cloudflare"
echo "  entirely: no WAF, no rate limiting, no DDoS protection."
if ! command -v ufw >/dev/null; then
    warn "ufw isn't installed — skipping. Restrict ports 80/443 to Cloudflare yourself."
elif ask "Apply firewall rules now?"; then
    # keep the CURRENT ssh port open, not just 22 — a non-standard port would
    # otherwise lock you out of your own server the moment ufw comes up
    SSH_PORT="$(awk '{print $4}' <<<"${SSH_CONNECTION:-}")"
    [[ "$SSH_PORT" =~ ^[0-9]+$ ]] || SSH_PORT=22
    ufw allow "$SSH_PORT"/tcp >/dev/null
    ok "SSH kept open on port $SSH_PORT"

    if CFIPS="$(cf_ranges)" && [[ -n "$CFIPS" ]]; then
        while read -r ip; do
            [[ -n "$ip" ]] && ufw allow from "$ip" to any port 443 proto tcp >/dev/null
        done <<<"$CFIPS"
        ok "port 443 opened to Cloudflare only"
    else
        ufw allow 443/tcp >/dev/null
        warn "couldn't fetch Cloudflare ranges — opened 443 to everyone instead"
    fi

    ufw deny "$APP_PORT"/tcp  >/dev/null
    ufw deny "$PANEL_PORT"/tcp >/dev/null
    ok "ports $APP_PORT (site) and $PANEL_PORT (admin panel) closed to the internet"

    ufw default deny incoming >/dev/null
    ufw --force enable >/dev/null
    ok "firewall active"
else
    warn "skipped — your origin IP is reachable directly until you do this."
fi
echo

# ── 6. what's left ───────────────────────────────────────────────────────────
bold "Server side: done."
echo
bold "Now in the Cloudflare dashboard for $DOMAIN:"
cat <<EOF
  DNS            A record, name @, content $(curl -fsS --max-time 5 -4 ifconfig.me 2>/dev/null || echo '<this server IP>'), Proxied (orange cloud)
  SSL/TLS        Overview → Full (strict)          ← "Flexible" breaks sign-ups
  SSL/TLS        Edge Certificates → Always Use HTTPS → On
  Speed          Optimization → Rocket Loader → OFF ← it breaks the page buttons
  Security       Bot Fight Mode → off, or exclude /o/*
  Caching        leave default — never "Cache Everything"

Then check it:
  curl -sI https://$DOMAIN/ | head -1
  curl -sI https://$DOMAIN/sell | grep -i set-cookie      # expect HttpOnly; Secure

Place one test order, then confirm the real visitor IP is arriving:
  journalctl -u p2p-bot | grep "web order"

  Your own IP  → correct.
  A Cloudflare IP, or the same IP on every order → $CF_CONF isn't loaded,
  and every customer is sharing one rate-limit bucket.
EOF
