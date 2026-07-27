"""Public customer website — the web interface to the same desk the Telegram
bot runs (one process, one database, one scanner, one admin panel).

Built for ad traffic: a fast landing page with live rates, a 3-step sell flow
(network → payout method + amount + bank → deposit screen), and a live order
page that polls status while the auto-scanner verifies the deposit on-chain.

Web customers have no Telegram account, so:
- they are stored as User rows with NEGATIVE ids (notify_user skips DMs for
  them — the order page is their live status feed);
- every order gets an unguessable `web_token`, and /o/<token> is the only way
  to reach it (plus a signed browser cookie for the "My orders" list).

Security: signed HttpOnly identity cookie, CSRF token on every form, per-IP
new-order throttle, all user data HTML-escaped, order pages only via token.
Bind 127.0.0.1 and put nginx + TLS in front (this is a public site).
"""

import hashlib
import hmac
import html
import logging
import secrets
import time
from collections import deque
from datetime import timezone
from functools import lru_cache

from aiohttp import web
from sqlalchemy import select

from . import texts
from .config import SERVICES, settings
from .db import (
    Session,
    bep20_active,
    desk_state,
    get_bep20_address,
    get_deposit_address,
    get_deposit_ttl,
    get_network_qr,
    get_rates,
    get_service_limits,
    get_setting,
    get_support,
    set_setting,
)
from .helpers import (
    TXID_RE,
    explorer_tx,
    norm_txid,
    queue_position,
    try_transition,
    txid_used_elsewhere,
)
from .models import BankCard, Order, OrderStatus, User
from .qr import qr_png

log = logging.getLogger(__name__)

COOKIE = "p2p_web"
COOKIE_TTL = 365 * 24 * 3600

# Statuses a customer may still submit a claim TXID for (mirrors the bot).
_CLAIMABLE = (OrderStatus.AWAITING_DEPOSIT.value, OrderStatus.EXPIRED.value,
              OrderStatus.CANCELLED.value)

# per-IP order-creation throttle: ip -> recent creation timestamps
_order_times: dict[str, deque] = {}


# ── identity / signing ────────────────────────────────────────────────────────

async def _secret() -> bytes:
    """Site-scoped signing secret, created once and kept in the DB."""
    async with Session() as s:
        val = await get_setting(s, "site_secret")
        if not val:
            val = secrets.token_hex(32)
            await set_setting(s, "site_secret", val)
    return val.encode()


async def _sign_uid(uid: int) -> str:
    mac = hmac.new(await _secret(), f"web:{uid}".encode(), hashlib.sha256).hexdigest()
    return f"{uid}.{mac}"


async def _uid_from_cookie(request: web.Request) -> int | None:
    raw = request.cookies.get(COOKIE, "")
    uid_s, _, _ = raw.partition(".")
    if not (uid_s.startswith("-") and uid_s[1:].isdigit()):
        return None
    uid = int(uid_s)
    if hmac.compare_digest(raw, await _sign_uid(uid)):
        return uid
    return None


async def _ensure_uid(request: web.Request) -> tuple[int, bool]:
    """(uid, is_new). A fresh uid is negative and random — set the cookie on
    the response whenever is_new is True."""
    uid = await _uid_from_cookie(request)
    if uid is not None:
        return uid, False
    return -(secrets.randbits(47) | (1 << 46)), True


def _is_https(request: web.Request) -> bool:
    return (request.secure or
            request.headers.get("X-Forwarded-Proto", "").lower() == "https")


def _set_uid_cookie(resp, signed: str, secure: bool) -> None:
    # Secure only when the request is HTTPS, so the identity cookie can't be
    # sniffed or fixated over a downgraded http request (production runs behind
    # nginx+TLS; plain-http dev still works with secure=False).
    resp.set_cookie(COOKIE, signed, httponly=True, samesite="Lax",
                    secure=secure, max_age=COOKIE_TTL)


async def _csrf(scope: str) -> str:
    return hmac.new(await _secret(), f"csrf:{scope}".encode(),
                    hashlib.sha256).hexdigest()


def _client_ip(request: web.Request) -> str:
    """The real client IP for per-IP throttling. X-Forwarded-For is only trusted
    when the direct peer is our local reverse proxy, and then only its LAST hop
    (the value nginx appended) — never the client-controlled leftmost entry, which
    would otherwise let an attacker mint a fresh 'IP' per request and defeat the
    throttle. Direct connections (no proxy) use the real socket peer."""
    peer = request.remote or "?"
    if peer in ("127.0.0.1", "::1"):
        xff = request.headers.get("X-Forwarded-For", "")
        if xff:
            return xff.split(",")[-1].strip() or peer
        _warn_no_xff()
    return peer


_warned_no_xff = False


def _warn_no_xff() -> None:
    """A proxied request that carries no X-Forwarded-For means the proxy isn't
    passing the client on — every visitor on earth then shares one throttle
    bucket, and the site starts refusing real customers as if they were one
    abuser. That looks like a site bug, so say so loudly, once."""
    global _warned_no_xff
    if not _warned_no_xff:
        _warned_no_xff = True
        log.warning("proxied request with no X-Forwarded-For — every visitor is "
                    "sharing one rate-limit bucket. Add "
                    "'proxy_set_header X-Forwarded-For $remote_addr;' to the "
                    "site's nginx location block (see deploy/nginx-site.conf).")


def _prune(dq: deque, now: float, window: int = 3600) -> None:
    while dq and dq[0] < now - window:
        dq.popleft()


def _throttled(ip: str) -> bool:
    """Read-only check — never creates an entry (so it can't mint unbounded dict
    keys) and reaps a deque once it empties."""
    now = time.time()
    dq = _order_times.get(ip)
    if dq is None:
        return False
    _prune(dq, now)
    if not dq:
        del _order_times[ip]
        return False
    return len(dq) >= max(1, settings.site_orders_per_hour)


def _record_order(ip: str) -> None:
    now = time.time()
    dq = _order_times.setdefault(ip, deque())
    _prune(dq, now)
    dq.append(now)
    if len(_order_times) > 5000:          # hard cap: drop emptied deques
        for k in [k for k, v in _order_times.items() if not v]:
            _order_times.pop(k, None)


# claim-attempt throttle keyed on "<ip>:<token>" — bounds the outbound chain
# lookups an attacker can drive by looping random valid-format TXIDs on one order.
_claim_times: dict[str, deque] = {}
_CLAIM_MAX_PER_HOUR = 8
# per-IP throttle for the on-demand "I've sent it" re-scan (each drives a sweep).
_check_times: dict[str, deque] = {}
_CHECK_MAX_PER_MIN = 6


def _bucket_throttled(store: dict, key: str, limit: int, window: int) -> bool:
    now = time.time()
    dq = store.get(key)
    if dq is None:
        return False
    _prune(dq, now, window)
    if not dq:
        del store[key]
        return False
    return len(dq) >= limit


def _bucket_record(store: dict, key: str, window: int) -> None:
    now = time.time()
    dq = store.setdefault(key, deque())
    _prune(dq, now, window)
    dq.append(now)
    if len(store) > 5000:
        for k in [k for k, v in store.items() if not v]:
            store.pop(k, None)


# ── HTML shell ────────────────────────────────────────────────────────────────

def _esc(v) -> str:
    return html.escape("" if v is None else str(v))


@web.middleware
async def _sec_headers(request: web.Request, handler):
    """Security headers on every response (incl. redirects/404s) + HSTS on HTTPS."""
    try:
        resp = await handler(request)
    except web.HTTPException as exc:
        resp = exc          # HTTPException is itself a Response — decorate + return
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    if _is_https(request):
        resp.headers.setdefault("Strict-Transport-Security",
                                "max-age=63072000; includeSubDomains")
    return resp


@lru_cache(maxsize=512)
def _gen_qr(address: str) -> bytes | None:
    """Auto-generated QR bytes for an address — memoized (a pure function; the QR
    for a given address never changes), so repeated /qr.png hits don't re-render."""
    return qr_png(address)


_STYLE = """
*{box-sizing:border-box}
:root{
 --bg:#f2f4f8;--surface:#ffffff;--surface-2:#f7f9fc;--border:#e3e7ee;
 --text:#131722;--muted:#5b6577;--faint:#8b95a6;
 --accent:#1a7f4e;--accent-ink:#ffffff;--accent-soft:#e4f3eb;
 --gold:#b45309;--danger:#b42318;--danger-soft:#fce9e6;
 --ok:#15803d;--ok-soft:#e6f5ec;--warn:#b45309;--warn-soft:#fbefdd;
 --info:#1d4ed8;--info-soft:#e7eefe;
 --shadow:0 1px 2px rgba(16,24,40,.05),0 6px 18px rgba(16,24,40,.07);
 --radius:16px;color-scheme:light dark}
@media (prefers-color-scheme:dark){:root{
 --bg:#0b0e12;--surface:#151a21;--surface-2:#1b212a;--border:#28303b;
 --text:#e8ebf2;--muted:#98a1b2;--faint:#697487;
 --accent:#37c07c;--accent-ink:#07130c;--accent-soft:#12291c;
 --gold:#f5b544;--danger:#f6837a;--danger-soft:#2a1716;
 --ok:#57d98a;--ok-soft:#132419;--warn:#f5b544;--warn-soft:#2a2212;
 --info:#66a6ff;--info-soft:#122036;
 --shadow:0 1px 2px rgba(0,0,0,.5)}}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--text);line-height:1.55;
 font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
 font-feature-settings:"tnum" 1;-webkit-font-smoothing:antialiased}
.wrap{max-width:680px;margin:0 auto;padding:0 16px 56px}
a{color:var(--accent);text-decoration:none}
h1{font-size:1.65rem;font-weight:800;letter-spacing:-.02em;margin:20px 0 8px;text-wrap:balance}
h2{font-size:1.08rem;font-weight:700;margin:26px 0 10px}
.topbar{display:flex;align-items:center;gap:10px;padding:14px 0}
.topbar .brand{font-weight:800;font-size:1.05rem;letter-spacing:-.01em;color:var(--text);
 display:flex;align-items:center;gap:8px}
.topbar .dot{width:10px;height:10px;border-radius:50%;background:var(--accent);
 box-shadow:0 0 0 4px var(--accent-soft)}
.topbar .sp{flex:1}
.topbar a.nav{color:var(--muted);font-weight:600;font-size:.92rem;padding:6px 10px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
 padding:18px;margin:14px 0;box-shadow:var(--shadow);overflow-wrap:anywhere}
.muted{color:var(--muted)} .small{font-size:.88rem} .faint{color:var(--faint)}
.badge{display:inline-flex;align-items:center;gap:6px;font-size:.72rem;font-weight:700;
 letter-spacing:.04em;text-transform:uppercase;padding:4px 10px;border-radius:999px;
 background:var(--surface-2);color:var(--muted);border:1px solid var(--border)}
.badge.ok{background:var(--ok-soft);color:var(--ok);border-color:transparent}
.badge.warn{background:var(--warn-soft);color:var(--warn);border-color:transparent}
.badge.info{background:var(--info-soft);color:var(--info);border-color:transparent}
.badge.danger{background:var(--danger-soft);color:var(--danger);border-color:transparent}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;width:100%;
 padding:14px 18px;border:0;border-radius:12px;background:var(--accent);color:var(--accent-ink);
 font-size:1rem;font-weight:700;cursor:pointer;font-family:inherit;text-align:center}
.btn:hover{filter:brightness(1.05)}
.btn.ghost{background:var(--surface-2);color:var(--text);border:1px solid var(--border)}
.btn.danger{background:var(--danger);color:#fff}
.btn+.btn{margin-top:10px}
label{display:block;font-size:.88rem;font-weight:600;color:var(--muted);margin:14px 0 5px}
input,select,textarea{width:100%;padding:13px 14px;font-size:1rem;border-radius:11px;
 border:1px solid var(--border);background:var(--surface-2);color:var(--text);font-family:inherit}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--accent);
 box-shadow:0 0 0 3px var(--accent-soft)}
.rates{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
.rates td{padding:11px 4px;border-bottom:1px solid var(--border)}
.rates tr:last-child td{border-bottom:0}
.rates .r{text-align:right;font-weight:800;font-size:1.05rem}
.hero-badges{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0 4px}
.step{display:flex;gap:12px;margin:14px 0}
.step .n{flex:0 0 30px;height:30px;border-radius:50%;background:var(--accent-soft);
 color:var(--accent);font-weight:800;display:flex;align-items:center;justify-content:center}
.amtbox{border:2px solid var(--accent);border-radius:14px;background:var(--accent-soft);
 text-align:center;padding:14px 10px;margin:12px 0}
.amtbox .v{font-size:1.7rem;font-weight:800;letter-spacing:-.01em}
.amtbox .l{font-size:.78rem;font-weight:700;letter-spacing:.06em;color:var(--muted);
 text-transform:uppercase}
.addr{display:block;background:var(--surface-2);border:1.5px dashed var(--accent);
 border-radius:12px;padding:13px 14px;margin:10px 0;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
 font-size:.92rem;word-break:break-all;color:var(--text)}
.qrimg{display:block;margin:14px auto;width:190px;height:190px;border-radius:14px;
 background:#fff;padding:10px;border:1px solid var(--border)}
.count{font-variant-numeric:tabular-nums;font-weight:800}
.netpick{display:flex;gap:10px}
.netpick label{flex:1;margin:0;border:1.5px solid var(--border);border-radius:12px;
 padding:14px;text-align:center;font-weight:700;color:var(--text);cursor:pointer;background:var(--surface-2)}
.netpick input{display:none}
.netpick input:checked+span{color:var(--accent)}
.netpick label:has(input:checked){border-color:var(--accent);background:var(--accent-soft)}
.banner{border:1px solid var(--border);border-left:4px solid var(--muted);background:var(--surface);
 border-radius:12px;padding:12px 14px;margin:12px 0}
.banner.ok{border-left-color:var(--ok)} .banner.warn{border-left-color:var(--warn)}
.banner.danger{border-left-color:var(--danger)}
.err{color:var(--danger);font-weight:600;margin:10px 0}
details{margin:12px 0}
details summary{cursor:pointer;color:var(--muted);font-weight:600}
.footer{margin-top:34px;color:var(--faint);font-size:.82rem;text-align:center}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
"""


def _page(title: str, body: str, desc: str = "") -> web.Response:
    doc = f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<meta name=description content="{_esc(desc or 'Sell USDT for INR — instant bank payout, on-chain verified.')}">
<title>{_esc(title)}</title><style>{_STYLE}</style></head><body>
<div class=wrap>
<div class=topbar><a href="/" class=brand><span class=dot></span>P2P Desk</a>
<span class=sp></span><a class=nav href="/my">My orders</a>
<a class=nav href="/sell">Sell USDT</a></div>
{body}
<p class=footer>Deposits verified on-chain · every payout handled by our admins</p>
</div></body></html>"""
    return web.Response(text=doc, content_type="text/html", headers={
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
    })


def _support_html(support: str) -> str:
    links = " · ".join(
        f"<a href='https://t.me/{_esc(h.lstrip('@'))}' target=_blank rel=noopener>{_esc(h)}</a>"
        for h in support.split() if h.strip())
    return links or _esc(support)


# ── landing ───────────────────────────────────────────────────────────────────

async def home(request: web.Request):
    async with Session() as s:
        rates = await get_rates(s)
        is_open, _ = await desk_state(s)
        support = await get_support(s)
    rows = "".join(
        f"<tr><td>{_esc(SERVICES.get(k, k))}</td>"
        f"<td class=r>₹{v:g}<span class='muted small'> /$</span></td></tr>"
        for k, v in rates.items())
    open_badge = ("<span class='badge ok'>● Desk open now</span>" if is_open
                  else "<span class='badge danger'>● Desk closed — check back soon</span>")
    cta = ("<a class=btn href='/sell'>💵 Sell USDT now</a>" if is_open
           else "<button class=btn disabled style='opacity:.6'>Desk closed</button>")
    body = f"""
<h1>Sell USDT. Get INR in your bank.</h1>
<p class=muted>Send USDT, we verify it <b>on-chain automatically</b>, and our admins
pay your bank — UPI, IMPS, CDM or cheque. The same desk thousands trade on Telegram,
now on the web.</p>
<div class=hero-badges>{open_badge}
<span class=badge>🛡 100% clean funds</span>
<span class=badge>⚡ Auto-verified deposits</span>
<span class=badge>📸 Proof on every deal</span></div>
<div class=card><h2 style="margin-top:0">📈 Live rates</h2>
<table class=rates>{rows or "<tr><td class=muted>No rates live right now.</td></tr>"}</table></div>
{cta}
<h2>How it works</h2>
<div class=card>
<div class=step><div class=n>1</div><div><b>Choose method &amp; amount</b><br>
<span class='muted small'>Pick your payout method and how much USDT you're selling.</span></div></div>
<div class=step><div class=n>2</div><div><b>Send the exact USDT amount</b><br>
<span class='muted small'>We show a deposit address + QR. Send the exact amount — our
scanner verifies it on-chain in seconds.</span></div></div>
<div class=step><div class=n>3</div><div><b>Get paid in INR</b><br>
<span class='muted small'>Verified deposits enter the payout queue and our admins pay
your bank directly. Proof shared on every deal.</span></div></div></div>
<div class=card><h2 style="margin-top:0">🛡 100% Clean Funds — our guarantee</h2>
<p class='muted small' style="margin:0">Every rupee we pay out comes from verified,
legitimate sources — mutual &amp; stock-market funds, cash deposits, credit-card and
payment-gateway funds. Your account is never at risk of a freeze or hold.</p></div>
<div class=card><b>🆘 Support</b><br><span class=small>{_support_html(support)}
<span class=muted>— mention your order ID (#ORD…)</span></span></div>"""
    return _page("Sell USDT for INR — P2P Desk", body,
                 "Sell USDT for INR at live rates. On-chain verified deposits, "
                 "instant bank payout via UPI/IMPS/CDM. 100% clean funds.")


# ── sell flow ─────────────────────────────────────────────────────────────────

async def _sell_form(request: web.Request, error: str = "",
                     prefill: dict | None = None) -> web.Response:
    p = prefill or {}
    async with Session() as s:
        rates = await get_rates(s)
        is_open, reason = await desk_state(s)
        two_chains = await bep20_active(s)
        support = await get_support(s)
    uid, is_new = await _ensure_uid(request)
    csrf = await _csrf(f"sell:{uid}")
    if not is_open:
        resp = _page("Desk closed", f"<h1>Desk closed</h1><div class='banner danger'>"
                     f"The desk isn't taking orders right now ({_esc(reason)}). "
                     f"Check back soon or message support: {_support_html(support)}</div>")
    else:
        limits = {}
        async with Session() as s:
            for k in rates:
                limits[k] = await get_service_limits(s, k)
        opts = "".join(
            f"<option value='{_esc(k)}' {'selected' if p.get('service') == k else ''}>"
            f"{_esc(SERVICES.get(k, k))} — ₹{v:g}/$"
            f" (min {limits[k][0]:g}$, max {limits[k][1]:g}$)</option>"
            for k, v in rates.items())
        net_html = ""
        if two_chains:
            trc_sel = "checked" if p.get("network", "TRC20") == "TRC20" else ""
            bep_sel = "checked" if p.get("network") == "BEP20" else ""
            net_html = (
                "<label>Network you'll send USDT on</label><div class=netpick>"
                f"<label><input type=radio name=network value=TRC20 {trc_sel}>"
                "<span>🔷 TRC20<br><span class='muted small'>TRON</span></span></label>"
                f"<label><input type=radio name=network value=BEP20 {bep_sel}>"
                "<span>🟡 BEP20<br><span class='muted small'>BSC</span></span></label></div>")
        async with Session() as s:
            ttl = await get_deposit_ttl(s)
        err = f"<p class=err>{_esc(error)}</p>" if error else ""
        body = f"""
<h1>Sell USDT</h1>
<p class='muted small'>Fill this once — your deposit address and exact amount come next.
The quote stays live for {ttl} minutes after you submit.</p>
{err}
<form method=post action=/sell><div class=card>
<input type=hidden name=csrf value='{csrf}'>
{net_html}
<label>Payout method</label><select name=service>{opts}</select>
<label>Amount to sell (USD $)</label>
<input name=usd inputmode=decimal placeholder="e.g. 100" value="{_esc(p.get('usd', ''))}" required>
<h2>Bank for your INR payout</h2>
<label>Account holder name</label>
<input name=holder value="{_esc(p.get('holder', ''))}" required>
<label>Bank name</label>
<input name=bank value="{_esc(p.get('bank', ''))}" required>
<label>Account number</label>
<input name=account inputmode=numeric value="{_esc(p.get('account', ''))}" required>
<label>IFSC</label>
<input name=ifsc value="{_esc(p.get('ifsc', ''))}" required>
<div style="margin-top:18px"><button class=btn>Get my deposit address →</button></div>
</div></form>
<p class='muted small'>🆘 Questions? {_support_html(support)}</p>"""
        resp = _page("Sell USDT — P2P Desk", body)
    if is_new:
        _set_uid_cookie(resp, await _sign_uid(uid), _is_https(request))
    return resp


async def sell_get(request: web.Request):
    return await _sell_form(request)


async def sell_post(request: web.Request):
    data = await request.post()
    uid = await _uid_from_cookie(request)
    if uid is None:
        return await _sell_form(request, "Please enable cookies and try again.")
    if not hmac.compare_digest(str(data.get("csrf", "")), await _csrf(f"sell:{uid}")):
        return await _sell_form(request, "That form expired — please try again.")
    prefill = {k: str(data.get(k, "")).strip()
               for k in ("service", "usd", "holder", "bank", "account", "ifsc", "network")}
    ip = _client_ip(request)
    if _throttled(ip):
        return await _sell_form(request, "Too many orders from this connection — "
                                "please wait a while or contact support.", prefill)

    from .handlers.sell import _tag_amount
    from .handlers.start import bank_details_error, make_bank_label

    service = prefill["service"]
    try:
        usd = round(float(prefill["usd"].replace(",", "").lstrip("$")), 2)
    except ValueError:
        return await _sell_form(request, "Amount must be a number.", prefill)
    # bank name first — make_bank_label derives "<Bank> ••1234" from line 0
    details = (f"{prefill['bank']}\nA/c holder: {prefill['holder']}\n"
               f"A/C {prefill['account']}\nIFSC {prefill['ifsc']}")
    if not (prefill["holder"] and prefill["bank"] and prefill["ifsc"]
            and prefill["account"].isdigit() and len(prefill["account"]) >= 6):
        return await _sell_form(request, "Please check the bank details — the account "
                                "number should be digits only (6+).", prefill)
    bank_err = bank_details_error(details)
    if bank_err:
        return await _sell_form(request, bank_err, prefill)
    if len(prefill["holder"]) > 80 or len(prefill["bank"]) > 60 or len(prefill["ifsc"]) > 20:
        return await _sell_form(request, "Those bank details look too long.", prefill)

    async with Session() as s:
        is_open, reason = await desk_state(s)
        rates = await get_rates(s)
        address = await get_deposit_address(s)
        if not is_open or not address or service not in rates:
            return await _sell_form(request, f"The desk can't take this order right now "
                                    f"({_esc(reason)}).", prefill)
        lo, hi = await get_service_limits(s, service)
        if not (lo <= usd <= hi):
            return await _sell_form(request, f"Amount must be between {lo:g}$ and {hi:g}$ "
                                    f"for {SERVICES.get(service, service)}.", prefill)
        rate = rates[service]
        bep20 = await get_bep20_address(s) if await bep20_active(s) else ""
        if prefill.get("network") == "BEP20" and bep20:
            show_addr, net_key = bep20, "BEP20"
        else:
            show_addr, net_key = address, "TRC20"

        # ensure the web user exists (negative id — never collides with Telegram)
        user = await s.get(User, uid)
        if user is None:
            user = User(id=uid, username="web", first_name=prefill["holder"][:60] or "Web")
            s.add(user)
            await s.flush()
        if user.banned:
            return await _sell_form(request, "This account can't trade — contact support.")

        # cap in-flight settlements; a new quote instantly voids earlier unpaid ones
        inflight = len((await s.scalars(select(Order.id).where(
            Order.user_id == uid,
            Order.status.in_((OrderStatus.DEPOSIT_RECEIVED.value,
                              OrderStatus.PENDING_PAYOUT.value))))).all())
        if inflight >= settings.open_orders_max:
            return await _sell_form(request, "You already have orders being paid out — "
                                    "please wait for those to finish.", prefill)
        for prev in (await s.scalars(select(Order).where(
                Order.user_id == uid,
                Order.status == OrderStatus.AWAITING_DEPOSIT.value))).all():
            await try_transition(s, prev.id, (OrderStatus.AWAITING_DEPOSIT,),
                                 OrderStatus.EXPIRED)

        card = BankCard(user_id=uid, label=make_bank_label(details), details=details)
        s.add(card)
        await s.flush()
        order = Order(
            user_id=uid, side="sell", service=service, usd_amount=usd,
            rate_inr=rate, inr_amount=usd * rate, bank_card_id=card.id,
            deposit_address=address,           # TRC20 desk address — the scanner's anchor
            network=net_key, display_address=show_addr,
            web_token=secrets.token_urlsafe(24),
        )
        s.add(order)
        await s.flush()
        order.usd_amount = _tag_amount(order.id, usd)
        order.inr_amount = order.usd_amount * rate
        await s.commit()
        token = order.web_token
        order_id = order.id

    # The client IP is otherwise invisible (the site runs with access logging
    # off so order tokens stay out of the logs), and it is the one thing that
    # proves the reverse proxy is forwarding visitors correctly: if every order
    # here shows the SAME address, the per-IP limits are all sharing one bucket
    # and real customers will start being refused. Also the first thing you want
    # when investigating abuse.
    log.info("web order #%s created from %s (%s %.2f USDT)",
             order_id, ip, service, usd)
    _record_order(ip)
    resp = web.HTTPFound(f"/o/{token}")
    _set_uid_cookie(resp, await _sign_uid(uid), _is_https(request))
    return resp


# ── order page ────────────────────────────────────────────────────────────────

async def _order_by_token(token: str) -> Order | None:
    if not token or len(token) > 48:
        return None
    async with Session() as s:
        return await s.scalar(select(Order).where(Order.web_token == token))


def _epoch_ms(dt) -> int:
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000) if dt else 0


async def order_page(request: web.Request):
    token = request.match_info["token"]
    order = await _order_by_token(token)
    if order is None:
        raise web.HTTPNotFound(text="Order not found")
    async with Session() as s:
        support = await get_support(s)
        ttl = await get_deposit_ttl(s)
        pos = (await queue_position(s, order.id)
               if order.status == OrderStatus.PENDING_PAYOUT.value else 0)
        card = (await s.get(BankCard, order.bank_card_id)
                if order.bank_card_id else None)
    bank_label = (card.label if card
                  else SERVICES.get(order.service, order.service))
    csrf = await _csrf(f"o:{token}")
    st = order.status
    amt = texts.usd_str(order.usd_amount)
    dec = f".{amt.split('.')[1]}" if "." in amt else ""
    net_label = "BEP20 (BSC)" if order.network == "BEP20" else "TRC20 (TRON)"
    net_emoji = "🟡" if order.network == "BEP20" else "🔷"
    show_addr = order.display_address or order.deposit_address
    tagline = f"<code>{texts.tag(order.id)}</code>"
    claim_form = f"""
<details><summary>Already sent the USDT? Submit your TXID</summary>
<form method=post action="/o/{_esc(token)}/claim"><div class=card>
<input type=hidden name=csrf value='{csrf}'>
<p class='muted small' style="margin-top:0">Paste the <b>transaction hash (TXID)</b> from your
wallet — it starts with <code>0x</code> on BEP20. We verify it on-chain; only a real payment
to our address is accepted.</p>
<input name=txid placeholder="TXID / transaction hash" required>
<div style="margin-top:12px"><button class=btn>Verify my payment</button></div>
</div></form></details>"""

    if st == OrderStatus.AWAITING_DEPOSIT.value:
        deadline = _epoch_ms(order.created_at) + ttl * 60_000
        warn = (f"❗ <b>Include the {dec}</b> — send the EXACT amount, decimals and all. "
                f"A wrong amount may not auto-detect." if dec else
                "❗ <b>Send the exact amount.</b>")
        body = f"""
<h1>Send your USDT {tagline}</h1>
<div class=banner><b>💵 You'll receive ₹{order.inr_amount:,.2f}</b>
<span class=muted>→ {_esc(bank_label)}</span><br>
<span class='muted small'>⏳ Quote expires in <span id=cd class=count>--:--</span>
· auto-verified in seconds after it confirms</span></div>
<div class=card>
<b>{net_emoji} On {_esc(net_label)} — copy the address</b>
<span class=addr id=addr>{_esc(show_addr)}</span>
<button class="btn ghost" onclick="copyAddr(this)">📋 Copy address</button>
<img class=qrimg src="/o/{_esc(token)}/qr.png" alt="Deposit QR"
 onerror="this.remove()">
<b>💸 Then send exactly</b>
<div class=amtbox><div class=l>send exactly</div><div class=v>{_esc(amt)} USDT</div></div>
<p class='muted small' style="margin:6px 0 0">{warn}</p>
</div>
<button class=btn id=checkbtn onclick="checkNow()">✅ I've sent it — check it</button>
<div id=checking class="banner warn" style="display:none">🔍 Checking the blockchain…
a fresh transfer takes ~a minute to confirm. This page updates automatically.</div>
{claim_form}
<form method=post action="/o/{_esc(token)}/cancel"
 onsubmit="return confirm('Cancel this order? Only do this if you have NOT paid.')">
<input type=hidden name=csrf value='{csrf}'>
<button class="btn ghost">🚫 No, I'm not paid — cancel</button></form>
<p class='muted small'>🆘 Need help? {_support_html(support)} — mention {tagline}</p>
<script>
var deadline={deadline};
function tick(){{var s=Math.max(0,Math.floor((deadline-Date.now())/1000));
document.getElementById('cd').textContent=Math.floor(s/60)+':'+String(s%60).padStart(2,'0');
if(s<=0)setTimeout(function(){{location.reload()}},4000);}}
tick();setInterval(tick,1000);
function copyAddr(b){{navigator.clipboard.writeText(document.getElementById('addr').textContent.trim())
.then(function(){{b.textContent='✅ Copied';setTimeout(function(){{b.textContent='📋 Copy address'}},1500)}});}}
function checkNow(){{var b=document.getElementById('checkbtn');b.disabled=true;b.style.opacity=.6;
document.getElementById('checking').style.display='block';
fetch('/o/{_esc(token)}/check',{{method:'POST',headers:{{'X-CSRF':'{csrf}'}}}});}}
setInterval(function(){{fetch('/o/{_esc(token)}/status.json').then(r=>r.json())
.then(function(j){{if(j.status!=='{st}')location.reload();}}).catch(function(){{}});}},6000);
</script>"""
        return _page(f"Order {texts.tag(order.id)} — send USDT", body)

    if st in (OrderStatus.DEPOSIT_RECEIVED.value, OrderStatus.PENDING_PAYOUT.value):
        qtxt = (f"You're <b>#{pos}</b> in the payout queue." if pos
                else "Finalizing your payout…")
        body = f"""
<h1>✅ Deposit verified {tagline}</h1>
<div class="banner ok"><b>{texts.usd_str(order.usd_amount)} USDT received &amp; verified on-chain.</b><br>
<span class=small>💰 ₹{order.inr_amount:,.2f} is being paid to
{_esc(bank_label)}. {qtxt}</span></div>
<div class=card class=small><span class=muted>TX:</span>
<code>{_esc((order.txid or '')[:20])}…</code>
{f'<a href="{_esc(explorer_tx(order.txid))}" target=_blank rel=noopener>view on explorer</a>' if order.txid and order.txid != 'manual' else ''}</div>
<p class='muted small'>This page refreshes automatically — you can keep it open or come
back later from <a href="/my">My orders</a>. 🆘 {_support_html(support)}</p>
<script>setInterval(function(){{fetch('/o/{_esc(token)}/status.json').then(r=>r.json())
.then(function(j){{if(j.status!=='{st}')location.reload();}}).catch(function(){{}});}},8000);</script>"""
        return _page(f"Order {texts.tag(order.id)} — verified", body)

    if st == OrderStatus.COMPLETED.value:
        body = f"""
<h1>🎉 Paid! {tagline}</h1>
<div class="banner ok"><b>₹{order.inr_amount:,.2f} sent to {_esc(bank_label)}.</b><br>
<span class=small>Thanks for trading with us — proof is shared on every deal.</span></div>
<a class=btn href="/sell">💵 Sell more USDT</a>
<p class='muted small'>🆘 {_support_html(support)}</p>"""
        return _page(f"Order {texts.tag(order.id)} — paid", body)

    if st in (OrderStatus.EXPIRED.value, OrderStatus.CANCELLED.value):
        head = ("⌛ This quote expired" if st == OrderStatus.EXPIRED.value
                else "❌ Order cancelled")
        note = ("No deposit arrived in time — don't send anything to the old "
                "address/amount now." if st == OrderStatus.EXPIRED.value else
                "Nothing is pending on this order.")
        pending_claim = ""
        if order.claim_txid:
            pending_claim = ("<div class='banner warn'>🧾 Your TXID is <b>under review</b> — "
                             "our team verifies it and pays out if it checks out. "
                             "This page updates automatically.</div>")
        body = f"""
<h1>{head} {tagline}</h1>
<div class=banner>{note}</div>
{pending_claim}
<a class=btn href="/sell">💵 Start a fresh order</a>
{claim_form if not order.claim_txid else ''}
<p class='muted small'>🆘 {_support_html(support)} — mention {tagline}</p>
<script>setInterval(function(){{fetch('/o/{_esc(token)}/status.json').then(r=>r.json())
.then(function(j){{if(j.status!=='{st}')location.reload();}}).catch(function(){{}});}},8000);</script>"""
        return _page(f"Order {texts.tag(order.id)}", body)

    # refund / rejected / anything else — simple status card
    body = f"""
<h1>Order {tagline}</h1>
<div class=banner>Status: <b>{_esc(st.replace('_', ' '))}</b></div>
<p class='muted small'>🆘 {_support_html(support)} — mention {tagline}</p>"""
    return _page(f"Order {texts.tag(order.id)}", body)




async def order_status(request: web.Request):
    order = await _order_by_token(request.match_info["token"])
    if order is None:
        raise web.HTTPNotFound()
    return web.json_response({"status": order.status})


async def order_qr(request: web.Request):
    order = await _order_by_token(request.match_info["token"])
    if order is None or order.status != OrderStatus.AWAITING_DEPOSIT.value:
        raise web.HTTPNotFound()
    show_addr = order.display_address or order.deposit_address
    async with Session() as s:
        stored = await get_network_qr(s, order.network or "TRC20", show_addr)
    # get_network_qr returns bytes on this branch, or (bytes, mime) once the
    # QR-upload PR lands — accept either so merge order doesn't matter.
    if isinstance(stored, tuple):
        img, mime, cache = stored[0], stored[1], "no-store"
    elif stored:
        img, mime, cache = stored, "image/png", "no-store"
    else:
        img, mime, cache = _gen_qr(show_addr), "image/png", "private, max-age=600"
    if not img:
        raise web.HTTPNotFound()
    return web.Response(body=img, content_type=mime,
                        headers={"Cache-Control": cache})


async def order_check(request: web.Request):
    """'I've sent it' — kick the same on-demand re-scan the bot uses. The page
    polls status.json to see the result."""
    token = request.match_info["token"]
    order = await _order_by_token(token)
    if order is None:
        raise web.HTTPNotFound()
    if not hmac.compare_digest(request.headers.get("X-CSRF", ""),
                               await _csrf(f"o:{token}")):
        raise web.HTTPForbidden(text="bad csrf")
    ip = _client_ip(request)
    if _bucket_throttled(_check_times, ip, _CHECK_MAX_PER_MIN, 60):
        return web.json_response({"checking": False, "wait": True}, status=429)
    _bucket_record(_check_times, ip, 60)
    bot = request.app.get("bot")
    if order.status == OrderStatus.AWAITING_DEPOSIT.value and bot is not None:
        # launch_order_check dedups per order_id, so repeated taps don't stack sweeps
        from .scanner import launch_order_check
        launch_order_check(bot, order.id)
    return web.json_response({"checking": True})


async def order_cancel(request: web.Request):
    token = request.match_info["token"]
    order = await _order_by_token(token)
    if order is None:
        raise web.HTTPNotFound()
    data = await request.post()
    if not hmac.compare_digest(str(data.get("csrf", "")), await _csrf(f"o:{token}")):
        raise web.HTTPForbidden(text="bad csrf")
    async with Session() as s:
        await try_transition(s, order.id, (OrderStatus.AWAITING_DEPOSIT,),
                             OrderStatus.CANCELLED)
    raise web.HTTPFound(f"/o/{token}")


async def order_claim(request: web.Request):
    """Customer says they paid — verify the TXID on-chain with the same guards
    the bot uses (reuse + wrong-address/not-found/too-old/amount checks), then
    hand a high-confidence card to the admin."""
    token = request.match_info["token"]
    order = await _order_by_token(token)
    if order is None:
        raise web.HTTPNotFound()
    data = await request.post()
    if not hmac.compare_digest(str(data.get("csrf", "")), await _csrf(f"o:{token}")):
        raise web.HTTPForbidden(text="bad csrf")

    def back(msg: str) -> web.Response:
        return _page("Payment check", f"<h1>Order {texts.tag(order.id)}</h1>"
                     f"<div class='banner danger'>{msg}</div>"
                     f"<a class=btn href='/o/{_esc(token)}'>← Back to the order</a>")

    if order.status not in _CLAIMABLE or order.claim_txid:
        return back("This order can't take a TXID anymore — contact support.")
    txid = norm_txid(str(data.get("txid", "")))
    if not TXID_RE.fullmatch(txid):
        return back("That doesn't look like a transaction hash — it's 64 characters "
                    "(with a 0x in front on BEP20). Check your wallet's history.")
    # Rate-limit the outbound on-chain lookup per (ip, order): a failed check
    # leaves the order claimable, so without this an attacker could loop random
    # hashes and burn the desk's TronGrid/BscScan quota.
    throttle_key = f"{_client_ip(request)}:{token}"
    if _bucket_throttled(_claim_times, throttle_key, _CLAIM_MAX_PER_HOUR, 3600):
        return back("Too many checks on this order — please wait a bit, or send your "
                    "payment screenshot + TXID to support.")
    _bucket_record(_claim_times, throttle_key, 3600)
    async with Session() as s:
        if await txid_used_elsewhere(s, txid, order.id):
            return back("That TXID is already used by another order.")
    from .handlers.sell import _post_claim_card, _verify_deposit_tx
    ok, reject, verify = await _verify_deposit_tx(txid, order.deposit_address, order)
    if not ok:
        import re as _re
        return back(_re.sub(r"<[^>]+>", "", reject)[:500] or
                    "We couldn't verify that TXID against this order.")
    if verify.get("error"):
        # transient chain-API error: on the bot this passes to a human, but on the
        # open web we must NOT accept it (it would post an admin card) — ask to retry.
        return back("We couldn't reach the blockchain just now — please try again in "
                    "a minute. If it keeps failing, send your TXID to support.")
    async with Session() as s:
        fresh = await s.get(Order, order.id)
        if fresh is None or fresh.status not in _CLAIMABLE or fresh.claim_txid:
            return back("This order can't take a TXID anymore — contact support.")
        fresh.claim_txid = txid
        await s.commit()
    bot = request.app.get("bot")
    if bot is not None:
        await _post_claim_card(bot, order.id, txid, verify)
    raise web.HTTPFound(f"/o/{token}")


# ── my orders ─────────────────────────────────────────────────────────────────

async def my_orders(request: web.Request):
    uid = await _uid_from_cookie(request)
    if uid is None:
        return _page("My orders", "<h1>My orders</h1><div class=banner>No orders on "
                     "this device yet — they appear here after your first order.</div>"
                     "<a class=btn href='/sell'>💵 Sell USDT</a>")
    async with Session() as s:
        orders = (await s.scalars(select(Order).where(Order.user_id == uid)
                                  .order_by(Order.id.desc()).limit(20))).all()
    if not orders:
        return _page("My orders", "<h1>My orders</h1><div class=banner>No orders yet."
                     "</div><a class=btn href='/sell'>💵 Sell USDT</a>")
    cls = {OrderStatus.COMPLETED.value: "ok", OrderStatus.PENDING_PAYOUT.value: "warn",
           OrderStatus.DEPOSIT_RECEIVED.value: "info",
           OrderStatus.AWAITING_DEPOSIT.value: "info",
           OrderStatus.CANCELLED.value: "danger", OrderStatus.EXPIRED.value: "danger"}
    rows = "".join(
        f"<a class=card style='display:block;color:inherit' href='/o/{_esc(o.web_token)}'>"
        f"<b>{texts.tag(o.id)}</b> <span class='badge {cls.get(o.status, '')}'>"
        f"{_esc(o.status.replace('_', ' '))}</span><br>"
        f"<span class=small>{texts.usd_str(o.usd_amount)} USDT → ₹{o.inr_amount:,.2f}"
        f" · {_esc(SERVICES.get(o.service, o.service))}</span></a>"
        for o in orders if o.web_token)
    return _page("My orders", f"<h1>My orders</h1>{rows}"
                 "<a class=btn href='/sell'>💵 New order</a>")


# ── app ───────────────────────────────────────────────────────────────────────

async def start_site(bot):
    """Start the public customer site (same process/DB); returns the AppRunner
    or None when disabled (site_port=0)."""
    if not settings.site_port:
        log.info("customer website disabled (P2P_SITE_PORT=0)")
        return None
    app = web.Application(middlewares=[_sec_headers])
    app["bot"] = bot
    app.add_routes([
        web.get("/", home),
        web.get("/sell", sell_get),
        web.post("/sell", sell_post),
        web.get("/my", my_orders),
        web.get("/o/{token}", order_page),
        web.get("/o/{token}/status.json", order_status),
        web.get("/o/{token}/qr.png", order_qr),
        web.post("/o/{token}/check", order_check),
        web.post("/o/{token}/cancel", order_cancel),
        web.post("/o/{token}/claim", order_claim),
    ])
    # access_log=None: request lines carry the per-order capability token in the
    # path (/o/<token>) — keep it out of the log sink.
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, settings.site_host, settings.site_port)
    await site.start()
    log.info("customer website on http://%s:%s (put nginx + TLS in front)",
             settings.site_host, settings.site_port)
    return runner
