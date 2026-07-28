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
import json
import logging
import secrets
import time
from collections import deque
from datetime import timedelta, timezone
from functools import lru_cache

from aiohttp import web
from sqlalchemy import func, select

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
    get_whatsapp,
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
 --bg:#ffffff;--wash:#f4f7fa;--surface:#ffffff;--surface-2:#f4f7fa;--border:#e6eaf1;
 --text:#0e1330;--muted:#5a657d;--faint:#8b95a8;
 --accent:#00c26f;--accent-dark:#00a85f;--accent-ink:#062b1a;--accent-soft:#e1f9ee;
 --navy:#0e1330;--gold:#b45309;--danger:#c0271c;--danger-soft:#fcebe9;
 --ok:#0c8f56;--ok-soft:#e1f9ee;--warn:#b45309;--warn-soft:#fbefdd;
 --info:#2456d6;--info-soft:#e9effe;
 --shadow:0 2px 4px rgba(14,19,48,.04),0 12px 32px rgba(14,19,48,.08);
 --radius:20px;color-scheme:light}
html{-webkit-text-size-adjust:100%}
body{margin:0;color:var(--text);line-height:1.55;
 background:radial-gradient(90% 340px at 50% 0,#e4faee 0%,#ffffff 78%) no-repeat,var(--bg);
 font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
 font-feature-settings:"tnum" 1;-webkit-font-smoothing:antialiased}
.wrap{max-width:680px;margin:0 auto;padding:0 16px 56px}
a{color:var(--accent-dark);text-decoration:none;font-weight:600}
h1{font-size:2rem;font-weight:900;letter-spacing:-.035em;line-height:1.12;
 margin:26px 0 10px;text-wrap:balance}
h1 .g{color:var(--accent-dark)}
h2{font-size:1.12rem;font-weight:800;letter-spacing:-.01em;margin:28px 0 10px}
.topbar{display:flex;align-items:center;gap:2px;padding:14px 0;flex-wrap:wrap}
.topbar .brand{font-weight:900;font-size:1.08rem;letter-spacing:-.02em;color:var(--text);
 display:flex;align-items:center;gap:8px;margin-right:4px}
.topbar .dot{width:11px;height:11px;border-radius:50%;background:var(--accent);
 box-shadow:0 0 0 4px var(--accent-soft)}
.topbar .sp{flex:1}
.topbar a.nav{color:var(--muted);font-weight:700;font-size:.85rem;padding:8px 10px;
 border-radius:999px}
.topbar a.nav:hover{background:var(--surface-2);color:var(--text)}
.topbar a.nav.hot{background:var(--navy);color:#fff;padding:9px 16px;margin-left:2px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
 padding:20px;margin:14px 0;box-shadow:var(--shadow);overflow-wrap:anywhere;
 transition:transform .18s,box-shadow .18s}
.card:hover{transform:translateY(-3px);
 box-shadow:0 4px 8px rgba(14,19,48,.05),0 20px 44px rgba(14,19,48,.12)}
.muted{color:var(--muted)} .small{font-size:.88rem} .faint{color:var(--faint)}
.badge{display:inline-flex;align-items:center;gap:6px;font-size:.73rem;font-weight:800;
 letter-spacing:.03em;padding:6px 12px;border-radius:999px;
 background:var(--surface);color:var(--muted);border:1px solid var(--border);
 box-shadow:0 1px 2px rgba(14,19,48,.05)}
.badge.ok{background:var(--ok-soft);color:var(--ok);border-color:transparent}
.badge.warn{background:var(--warn-soft);color:var(--warn);border-color:transparent}
.badge.info{background:var(--info-soft);color:var(--info);border-color:transparent}
.badge.danger{background:var(--danger-soft);color:var(--danger);border-color:transparent}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;width:100%;
 padding:16px 22px;border:0;border-radius:999px;background:var(--accent);color:var(--accent-ink);
 font-size:1.02rem;font-weight:800;letter-spacing:-.01em;cursor:pointer;font-family:inherit;
 text-align:center;box-shadow:0 10px 24px rgba(0,194,111,.30);
 transition:transform .12s,box-shadow .12s,background .12s}
.btn:hover{background:var(--accent-dark);color:#fff;transform:translateY(-1px);
 box-shadow:0 14px 28px rgba(0,194,111,.34)}
.btn:disabled{transform:none}
.btn.ghost{background:var(--surface);color:var(--text);border:1.5px solid var(--border);
 box-shadow:0 1px 2px rgba(14,19,48,.05)}
.btn.ghost:hover{background:var(--surface-2);color:var(--text);transform:none;
 box-shadow:0 1px 2px rgba(14,19,48,.05)}
.btn.danger{background:var(--danger);color:#fff;box-shadow:0 10px 24px rgba(192,39,28,.25)}
.btn+.btn{margin-top:10px}
label{display:block;font-size:.85rem;font-weight:700;color:var(--muted);margin:16px 0 6px}
input,select,textarea{width:100%;padding:14px 16px;font-size:1rem;border-radius:14px;
 border:1.5px solid var(--border);background:var(--surface-2);color:var(--text);font-family:inherit}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--accent);
 background:#fff;box-shadow:0 0 0 4px var(--accent-soft)}
.swapbox{background:var(--surface-2);border:1.5px solid var(--border);border-radius:18px;
 padding:14px 16px;margin:10px 0;display:flex;align-items:center;gap:12px}
.swapbox:focus-within{border-color:var(--accent);background:#fff;
 box-shadow:0 0 0 4px var(--accent-soft)}
.swapbox .lab{font-size:.72rem;font-weight:800;letter-spacing:.06em;text-transform:uppercase;
 color:var(--faint);display:block;margin-bottom:2px}
.swapbox .grow{flex:1;min-width:0}
.swapbox input{border:0;background:transparent;padding:0;font-size:1.5rem;font-weight:800;
 letter-spacing:-.02em;width:100%}
.swapbox input:focus{box-shadow:none;background:transparent}
.swapbox .out{font-size:1.5rem;font-weight:800;letter-spacing:-.02em;color:var(--text)}
.chip{display:inline-flex;align-items:center;gap:7px;background:#fff;border:1.5px solid var(--border);
 border-radius:999px;padding:8px 14px;font-weight:800;font-size:.95rem;white-space:nowrap;
 box-shadow:0 1px 2px rgba(14,19,48,.06)}
.chip .ic{width:22px;height:22px;border-radius:50%;display:inline-flex;align-items:center;
 justify-content:center;font-size:.72rem;font-weight:900;color:#fff}
.chip .ic.usdt{background:#26a17b}
.chip .ic.inr{background:var(--navy)}
.swaparrow{width:38px;height:38px;border-radius:50%;background:#fff;border:1.5px solid var(--border);
 display:flex;align-items:center;justify-content:center;margin:-16px auto;position:relative;z-index:2;
 font-weight:900;color:var(--accent-dark);box-shadow:0 2px 6px rgba(14,19,48,.10)}
.rates{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
.rates td{padding:12px 4px;border-bottom:1px solid var(--border)}
.rates tr:last-child td{border-bottom:0}
.rates .r{text-align:right;font-weight:900;font-size:1.12rem;color:var(--accent-dark)}
.hero-badges{display:flex;gap:8px;flex-wrap:wrap;margin:16px 0 4px}
.brands{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0 2px}
.brands .b{display:inline-flex;align-items:center;gap:8px;background:var(--surface);
 border:1.5px solid var(--border);border-radius:999px;padding:7px 14px 7px 8px;
 font-weight:800;font-size:.9rem;box-shadow:0 1px 2px rgba(14,19,48,.06)}
.brands .ic{width:24px;height:24px;border-radius:50%;display:inline-flex;
 align-items:center;justify-content:center;color:#fff;font-size:.72rem;font-weight:900}
.d{display:inline-block;width:10px;height:10px;border-radius:50%;vertical-align:baseline}
.livewrap{display:inline-flex;align-items:center;gap:6px;margin-left:8px;
 font-size:.66rem;font-weight:900;letter-spacing:.1em;color:var(--accent-dark);
 vertical-align:middle}
.livedot{width:8px;height:8px;border-radius:50%;background:var(--accent);
 animation:pulse 1.6s ease-in-out infinite}
.livebars{display:inline-flex;align-items:flex-end;gap:2px;height:14px}
.livebars i{width:3px;border-radius:2px;background:var(--accent);animation:bars 1.1s ease-in-out infinite}
.livebars i:nth-child(1){height:6px}
.livebars i:nth-child(2){height:11px;animation-delay:.18s}
.livebars i:nth-child(3){height:8px;animation-delay:.36s}
@keyframes pulse{0%,100%{box-shadow:0 0 0 0 rgba(0,194,111,.45)}
 55%{box-shadow:0 0 0 6px rgba(0,194,111,0)}}
@keyframes bars{0%,100%{transform:scaleY(.55)}50%{transform:scaleY(1.15)}}
@keyframes floaty{0%,100%{transform:translateY(0)}50%{transform:translateY(-5px)}}
.step{display:flex;gap:12px;margin:14px 0}
.step .n{flex:0 0 32px;height:32px;border-radius:50%;background:var(--navy);
 color:#fff;font-weight:800;display:flex;align-items:center;justify-content:center}
.stats{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;margin:16px 0}
.stats .stat{background:var(--surface);border:1px solid var(--border);border-radius:16px;
 padding:14px 8px;text-align:center;box-shadow:var(--shadow)}
.stats .v{font-size:1.3rem;font-weight:900;letter-spacing:-.02em;color:var(--accent-dark)}
.stats .k{font-size:.7rem;font-weight:800;letter-spacing:.06em;text-transform:uppercase;
 color:var(--faint)}
.kv{display:flex;justify-content:space-between;gap:12px;padding:8px 0;
 border-bottom:1px solid var(--border);font-size:.92rem}
.kv:last-child{border-bottom:0}
.kv .k{color:var(--muted);flex-shrink:0}
.kv .v{text-align:right;font-weight:700;overflow-wrap:anywhere}
.hint{font-size:.86rem;color:var(--muted);margin:8px 2px 0;font-weight:600}
.hint.bad{color:var(--danger)}
.hint .inr{color:var(--accent-dark);font-weight:900}
.amtbox{border:2px solid var(--accent);border-radius:18px;background:var(--accent-soft);
 text-align:center;padding:16px 10px;margin:12px 0}
.amtbox .v{font-size:1.85rem;font-weight:900;letter-spacing:-.02em}
.amtbox .l{font-size:.72rem;font-weight:800;letter-spacing:.08em;color:var(--muted);
 text-transform:uppercase}
.addr{display:block;background:var(--surface-2);border:1.5px dashed var(--accent);
 border-radius:14px;padding:14px 15px;margin:10px 0;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
 font-size:.92rem;word-break:break-all;color:var(--text)}
.qrimg{display:block;margin:14px auto;width:190px;height:190px;border-radius:16px;
 background:#fff;padding:10px;border:1px solid var(--border)}
.count{font-variant-numeric:tabular-nums;font-weight:900}
.netpick{display:flex;gap:10px}
.netpick label{flex:1;margin:0;border:1.5px solid var(--border);border-radius:16px;
 padding:14px;text-align:center;font-weight:800;color:var(--text);cursor:pointer;background:var(--surface)}
.netpick input{display:none}
.netpick input:checked+span{color:var(--accent-dark)}
.netpick label:has(input:checked){border-color:var(--accent);background:var(--accent-soft);
 box-shadow:0 0 0 3px var(--accent-soft)}
.banner{border:1px solid var(--border);border-left:5px solid var(--muted);background:var(--surface);
 border-radius:14px;padding:13px 15px;margin:12px 0;box-shadow:var(--shadow)}
.banner.ok{border-left-color:var(--accent)} .banner.warn{border-left-color:var(--warn)}
.banner.danger{border-left-color:var(--danger)}
.err{color:var(--danger);font-weight:700;margin:10px 0}
details{margin:12px 0}
details summary{cursor:pointer;color:var(--text);font-weight:700;padding:4px 0}
details summary::marker{color:var(--accent-dark)}
.footer{margin-top:0;color:#8b95b8;font-size:.82rem;text-align:center}
.darkband{margin:18px 0;display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.darkband .cell{background:linear-gradient(160deg,#171d3d 0%,var(--navy) 60%);
 border-radius:18px;padding:20px 6px;text-align:center;
 border:1px solid rgba(255,255,255,.07);
 box-shadow:0 14px 30px rgba(14,19,48,.28),0 2px 6px rgba(14,19,48,.18);
 animation:floaty 5s ease-in-out infinite;
 transition:transform .18s,box-shadow .18s}
.darkband .cell:nth-child(2),.darkband .cell:nth-child(5){animation-delay:1.6s}
.darkband .cell:nth-child(3),.darkband .cell:nth-child(4){animation-delay:3.2s}
.darkband .cell:hover{transform:translateY(-6px);
 box-shadow:0 22px 44px rgba(14,19,48,.34),0 4px 10px rgba(14,19,48,.2)}
.darkband .k{color:#8b95b8;font-size:.68rem;font-weight:800;letter-spacing:.07em;
 text-transform:uppercase;margin-bottom:4px}
.darkband .v{color:#fff;font-size:1.45rem;font-weight:900;letter-spacing:-.02em;
 font-variant-numeric:tabular-nums}
.rv{opacity:0;transform:translateY(16px);
 transition:opacity .6s cubic-bezier(.2,.7,.3,1),transform .6s cubic-bezier(.2,.7,.3,1)}
.rv.in{opacity:1;transform:none}
@media (prefers-reduced-motion:reduce){.rv{opacity:1;transform:none;transition:none}}
.bigfoot{background:var(--navy);border-radius:24px 24px 0 0;margin:44px -16px 0;
 padding:30px 22px 24px;color:#aab3cd;font-size:.88rem}
.bigfoot h3{color:#fff;font-size:.8rem;font-weight:800;letter-spacing:.06em;
 text-transform:uppercase;margin:18px 0 8px}
.bigfoot a{display:block;color:#cdd5ea;padding:4px 0;font-weight:600}
.bigfoot a:hover{color:#fff}
.bigfoot .cols{display:grid;grid-template-columns:1fr 1fr;gap:0 18px}
.bigfoot .legal{color:#7d88a6;font-size:.78rem;line-height:1.6;margin-top:18px;
 border-top:1px solid rgba(255,255,255,.10);padding-top:16px}
.fabs{position:fixed;right:14px;bottom:14px;display:flex;flex-direction:column;
 gap:10px;z-index:60;align-items:flex-end}
.fab{display:inline-flex;align-items:center;gap:8px;border-radius:999px;
 padding:12px 18px;font-weight:800;font-size:.92rem;color:#fff;
 box-shadow:0 10px 26px rgba(14,19,48,.28)}
.fab:hover{filter:brightness(1.06);color:#fff}
.fab.wa{background:#25d366}
.fab.tg{background:#229ed9}
.cardpick{display:flex;flex-direction:column;gap:8px}
.cardpick label{margin:0;border:1.5px solid var(--border);border-radius:16px;
 padding:13px 15px;font-weight:800;color:var(--text);cursor:pointer;background:var(--surface);
 font-size:.98rem}
.cardpick input{display:none}
.cardpick label:has(input:checked){border-color:var(--accent);background:var(--accent-soft);
 box-shadow:0 0 0 3px var(--accent-soft)}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
"""


_TAIL = """<div class=bigfoot>
<div class=cols>
<div><h3>Trade</h3>
<a href="/sell">Sell USDT</a><a href="/my">My orders</a>
<a href="/#rates">Live rates</a><a href="/#faq">FAQ</a></div>
<div><h3>User rights &amp; disclosures</h3>
<a href="/legal/terms">Terms of Use</a>
<a href="/legal/privacy">Privacy &amp; Cookies Policy</a>
<a href="/legal/risks">Cryptoasset Risks</a>
<a href="/legal/transactions">Transaction &amp; Pricing Information</a>
<a href="/legal/aml">AML / Clean-Funds Policy</a>
<a href="/legal/transactions#complaints">Complaints</a></div>
</div>
<p class=legal>This is an independent over-the-counter desk for selling USDT for
Indian rupees. It is not a bank and not a deposit-taking institution; crypto
assets are not bank deposits, are not government-insured, and may lose value.
On-chain transfers are irreversible — always send the exact amount shown, on
the network shown. By using this site you agree to the
<a href="/legal/terms" style="display:inline;padding:0">Terms of Use</a>.</p>
<p class=footer>Deposits verified on-chain · every payout handled by our admins</p>
</div>
</div>
<script>
(function(){
 var els=document.querySelectorAll('.card,.stats,.darkband,.step,h2,.hero-badges');
 if(!('IntersectionObserver' in window)){return}
 var io=new IntersectionObserver(function(es){es.forEach(function(e){
   if(e.isIntersecting){e.target.classList.add('in');io.unobserve(e.target);
     e.target.querySelectorAll('[data-cu]').forEach(cu);}})},{threshold:.12});
 els.forEach(function(el){el.classList.add('rv');io.observe(el)});
 function cu(el){
   if(el.dataset.done)return;el.dataset.done=1;
   var n=parseFloat(el.dataset.cu),pre=el.dataset.pre||'',suf=el.dataset.suf||'',
       dec=parseInt(el.dataset.dec||'0'),t0=null;
   if(!isFinite(n))return;
   function fr(t){if(!t0)t0=t;var p=Math.min(1,(t-t0)/900);p=1-Math.pow(1-p,3);
     var v=n*p;
     el.textContent=pre+v.toLocaleString('en-IN',
       {minimumFractionDigits:dec,maximumFractionDigits:dec})+suf;
     if(p<1)requestAnimationFrame(fr);}
   requestAnimationFrame(fr);}
})();
</script>
"""


def _page(title: str, body: str, desc: str = "") -> web.Response:
    doc = f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<meta name=description content="{_esc(desc or 'Sell USDT for INR — instant bank payout, on-chain verified.')}">
<title>{_esc(title)}</title><style>{_STYLE}</style></head><body>
<div class=wrap>
<div class=topbar><a href="/" class=brand><span class=dot></span>P2P Desk</a>
<span class=sp></span>
<a class=nav href="/">Home</a>
<a class=nav href="/#rates">Rates</a>
<a class=nav href="/my">My orders</a>
<a class=nav href="/#support">Support</a>
<a class="nav hot" href="/sell">Sell USDT</a></div>
{body}
{_TAIL}
</body></html>"""
    return web.Response(text=doc, content_type="text/html", headers={
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
    })


def _fabs_html(support: str, whatsapp: str) -> str:
    """Floating Telegram / WhatsApp support buttons, bottom-right on every page.
    Rendered only for the channels that are actually configured."""
    fabs = ""
    if whatsapp:
        digits = "".join(ch for ch in whatsapp if ch.isdigit())
        if digits:
            fabs += (f"<a class='fab wa' href='https://wa.me/{digits}' "
                     "target=_blank rel=noopener>WhatsApp</a>")
    first = next((h for h in (support or "").split() if h.startswith("@")), "")
    if first:
        fabs += (f"<a class='fab tg' href='https://t.me/{_esc(first.lstrip('@'))}' "
                 "target=_blank rel=noopener>Telegram</a>")
    return f"<div class=fabs>{fabs}</div>" if fabs else ""


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
        whatsapp = await get_whatsapp(s)
        two_chains = await bep20_active(s)
        limits = {k: await get_service_limits(s, k) for k in rates}
        done_n = await s.scalar(
            select(func.count()).select_from(Order)
            .where(Order.status == OrderStatus.COMPLETED.value)) or 0
        paid_inr = await s.scalar(
            select(func.sum(Order.inr_amount))
            .where(Order.status == OrderStatus.COMPLETED.value)) or 0.0
        usdt_vol = await s.scalar(
            select(func.sum(Order.usd_amount))
            .where(Order.status == OrderStatus.COMPLETED.value)) or 0.0
        quotes_n = await s.scalar(
            select(func.count()).select_from(Order)) or 0
    # owner-declared lifetime history (Telegram-era) + what this system counted
    done_n += max(0, settings.stats_base_orders)
    paid_inr += max(0.0, settings.stats_base_inr)
    usdt_vol += max(0.0, settings.stats_base_usdt or
                    (settings.stats_base_inr / max(1.0, settings.stats_base_rate)))
    quotes_n += max(0, settings.stats_base_orders)
    plus = "+" if settings.stats_base_orders > 0 else ""
    rows = "".join(
        f"<tr><td><b>{_esc(SERVICES.get(k, k))}</b><br>"
        f"<span class='muted small'>{limits[k][0]:g}$ – {limits[k][1]:g}$ per order</span></td>"
        f"<td class=r>₹{v:g}<span class='muted small'> /$</span></td></tr>"
        for k, v in rates.items())
    open_badge = ("<span class='badge ok'>● Desk open now</span>" if is_open
                  else "<span class='badge danger'>● Desk closed — check back soon</span>")
    cta = ("<a class=btn href='/sell'>Sell USDT now</a>" if is_open
           else "<button class=btn disabled style='opacity:.6'>Desk closed</button>")
    def _big(n: float, pre: str = "", plus: str = "") -> str:
        """(cell html) — compact Indian-friendly figure with count-up targets."""
        if n >= 1e7:
            v, dec, suf = n / 1e7, (1 if n < 1e8 else 0), " Cr" + plus
        elif n >= 1e5:
            v, dec, suf = n / 1e5, (1 if n < 1e6 else 0), " L" + plus
        else:
            v, dec, suf = n, 0, plus
        disp = f"{v:,.{dec}f}"
        return (f"<div class=v data-cu={v:.{dec}f} data-dec={dec} "
                f"data-pre='{pre}' data-suf='{suf}'>{pre}{disp}{suf}</div>")

    stats = ""
    if done_n >= 5:
        stats = f"""
<div class=darkband>
<div class=cell><div class=k>Orders paid</div>{_big(done_n, "", plus)}</div>
<div class=cell><div class=k>Paid out</div>{_big(paid_inr, "₹", plus)}</div>
<div class=cell><div class=k>USDT settled</div>{_big(usdt_vol, "", plus)}</div>
<div class=cell><div class=k>Quotes serviced</div>{_big(quotes_n, "", plus)}</div>
<div class=cell><div class=k>Payout time</div>
<div class=v>~{_esc(settings.eta_text)}</div></div>
<div class=cell><div class=k>Verification</div>
<div class=v>On-chain</div></div>
</div>"""
    nets = "TRC20 (TRON) and BEP20 (BSC)" if two_chains else "TRC20 (TRON)"
    body = f"""
<h1>Sell USDT.<br><span class=g>Get INR in your bank.</span></h1>
<p class=muted>Send USDT, we verify it <b>on-chain automatically</b>, and our admins
pay your bank — UPI, IMPS, CDM or cheque. The same desk thousands trade on Telegram,
now on the web.</p>
<div class=hero-badges>{open_badge}
<span class=badge>100% clean funds</span>
<span class=badge>Auto-verified deposits</span>
<span class=badge>Proof on every deal</span></div>
{stats}
<div class=card id=rates><h2 style="margin-top:0">Live rates <span class=livewrap><span class=livedot></span>LIVE<span class=livebars><i></i><i></i><i></i></span></span></h2>
<table class=rates>{rows or "<tr><td class=muted>No rates live right now.</td></tr>"}</table>
<p class='muted small' style="margin:10px 0 0">Rates are live — the rate you see when you
order is the rate you're paid at. Networks accepted: <b>{nets}</b>.</p></div>
{cta}
<h2>How it works</h2>
<div class=card>
<div class=step><div class=n>1</div><div><b>Choose method &amp; amount</b><br>
<span class='muted small'>Pick your payout method, enter the USDT amount (each method
shows its min/max), and your bank details for the INR payout.</span></div></div>
<div class=step><div class=n>2</div><div><b>Send the exact USDT amount</b><br>
<span class='muted small'>We show a deposit address + QR. Send the exact amount — our
scanner verifies it on-chain in seconds, no screenshots needed.</span></div></div>
<div class=step><div class=n>3</div><div><b>Get paid in INR</b><br>
<span class='muted small'>Verified deposits enter the payout queue and our admins pay
your bank directly — typically {_esc(settings.eta_text)}. Proof shared on every deal.</span></div></div></div>
<div class=card><h2 style="margin-top:0">Sell from any wallet or exchange</h2>
<p class='muted small' style="margin:0 0 4px">Withdraw USDT from wherever you hold it and
send it to your order address — it works the same from every app:</p>
<div class=brands><span class=b><span class=ic style='background:#f0b90b'>◆</span>Binance</span><span class=b><span class=ic style='background:#3375bb'>T</span>Trust Wallet</span><span class=b><span class=ic style='background:#0e1330'>OK</span>OKX</span><span class=b><span class=ic style='background:#f7a600'>B</span>Bybit</span><span class=b><span class=ic style='background:#24ae8f'>K</span>KuCoin</span><span class=b><span class=ic style='background:#2980fe'>TP</span>TokenPocket</span><span class=b><span class=ic style='background:#3067f0'>W</span>WazirX</span><span class=b><span class=ic style='background:#4a24ae'>D</span>CoinDCX</span></div>
<p class='muted small' style="margin:8px 0 0">…and any other wallet or exchange that can
send USDT on your chosen network. Pick the network on the sell form and match it when
you withdraw.</p></div>
<div class=card><h2 style="margin-top:0">Fast and safe — across all of India</h2>
<div class=step><div class=n>⚑</div><div><b>Built to be India's fastest desk</b><br>
<span class='muted small'>Deposits verify on-chain in seconds — no screenshots, no
waiting for a human to check. Verified orders enter the payout queue and admins pay
over UPI and IMPS round the clock, typically {_esc(settings.eta_text)}.</span></div></div>
<div class=step><div class=n>✓</div><div><b>Engineered to be the safest</b><br>
<span class='muted small'>Every payout comes from verified clean sources, every deposit
is matched on a public blockchain, and proof is shared on every completed deal — a
record you can check, not a promise.</span></div></div>
<div class=step><div class=n>⌂</div><div><b>Every bank, everywhere in India</b><br>
<span class='muted small'>UPI, IMPS, CDM or cheque — payouts reach any Indian bank,
in any state, on bank holidays too.</span></div></div></div>
<div class=card><h2 style="margin-top:0">100% Clean Funds — our guarantee</h2>
<p class='muted small' style="margin:0">Every rupee we pay out comes from verified,
legitimate sources — mutual &amp; stock-market funds, cash deposits, credit-card and
payment-gateway funds. Your account is never at risk of a freeze or hold.</p></div>
<h2 id=faq>Frequently asked</h2>
<div class=card>
<details><summary>How fast do I get paid?</summary><p class='muted small'>Your deposit is
verified on-chain within seconds of confirming. Payout to your bank is typically
{_esc(settings.eta_text)} after verification, handled by our admins in queue order.</p></details>
<details><summary>Which networks can I send USDT on?</summary><p class='muted small'>
{nets}. Pick the network on the sell form — the address and QR we show match your
choice. Send only USDT, only on the network you picked.</p></details>
<details><summary>Why must the amount be exact?</summary><p class='muted small'>Each order
gets a unique amount (unique paise). That's how our scanner matches YOUR deposit to YOUR
order automatically — a different amount may not auto-detect and needs support.</p></details>
<details><summary>I paid but the page expired — is my money lost?</summary>
<p class='muted small'>No. Open the order from <a href="/my">My orders</a> and submit your
transaction hash (TXID) — we verify it on-chain and pay out if it checks out.</p></details>
<details><summary>Can I sell straight from Binance or another exchange?</summary>
<p class='muted small'>Yes. Place your order here, then in your exchange or wallet
withdraw the exact USDT amount to the address we show — choosing the same network
(TRC20 or BEP20) you picked on the order. No transfer to any special wallet needed
first.</p></details>
<details><summary>Do I need an account?</summary><p class='muted small'>No signup. Your
orders are tied to this browser automatically — find them any time under
<a href="/my">My orders</a>.</p></details>
</div>
<div class=card id=support><b>Support</b><br><span class=small>{_support_html(support)}
<span class=muted>— mention your order ID (#ORD…)</span></span></div>
{cta if rows else ""}
{_fabs_html(support, whatsapp)}"""
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
        whatsapp = await get_whatsapp(s)
    uid, is_new = await _ensure_uid(request)
    saved_cards = []
    if not is_new:
        async with Session() as s:
            rows_ = (await s.scalars(
                select(BankCard).where(BankCard.user_id == uid)
                .order_by(BankCard.id.desc()).limit(12))).all()
        seen = set()
        for c in rows_:                      # newest first, dedupe by content
            key = c.details.strip()
            if key not in seen:
                seen.add(key)
                saved_cards.append(c)
            if len(saved_cards) >= 4:
                break
    csrf = await _csrf(f"sell:{uid}")
    if not is_open:
        resp = _page("Desk closed", f"<h1>Desk closed</h1><div class='banner danger'>"
                     f"The desk isn't taking orders right now ({_esc(reason)}). "
                     f"Check back soon or message support: {_support_html(support)}</div>"
                     + _fabs_html(support, whatsapp))
    else:
        limits = {}
        async with Session() as s:
            for k in rates:
                limits[k] = await get_service_limits(s, k)
        opts = "".join(
            f"<option value='{_esc(k)}' {'selected' if p.get('service') == k else ''}>"
            f"{_esc(SERVICES.get(k, k))} — ₹{v:g}/$</option>"
            for k, v in rates.items())
        # per-method limits + rates for the live hint under the amount box
        meta_js = json.dumps({k: {"lo": limits[k][0], "hi": limits[k][1],
                                  "rate": rates[k],
                                  "name": SERVICES.get(k, k)} for k in rates})
        net_html = ""
        if two_chains:
            trc_sel = "checked" if p.get("network", "TRC20") == "TRC20" else ""
            bep_sel = "checked" if p.get("network") == "BEP20" else ""
            net_html = (
                "<label>Network you'll send USDT on</label><div class=netpick>"
                f"<label><input type=radio name=network value=TRC20 {trc_sel}>"
                "<span><span class=d style='background:#2470ff'></span> TRC20<br>"
                "<span class='muted small'>TRON</span></span></label>"
                f"<label><input type=radio name=network value=BEP20 {bep_sel}>"
                "<span><span class=d style='background:#f0b90b'></span> BEP20<br>"
                "<span class='muted small'>BSC</span></span></label></div>")
        async with Session() as s:
            ttl = await get_deposit_ttl(s)
        err = f"<p class=err>{_esc(error)}</p>" if error else ""
        picked = p.get("card_id", "")
        if saved_cards:
            if picked not in ("new",) and picked not in {str(c.id) for c in saved_cards}:
                # preselect the newest saved bank so a repeat seller taps nothing
                picked = str(saved_cards[0].id)
            copts = "".join(
                f"<label><input type=radio name=card_id value={c.id} "
                f"{'checked' if str(c.id) == picked else ''}>"
                f"{_esc(c.label)}</label>"
                for c in saved_cards)
            copts += (f"<label><input type=radio name=card_id value=new "
                      f"{'checked' if picked == 'new' else ''}>"
                      "+ Add a new bank</label>")
            card_pick = f"<div class=cardpick id=cardpick>{copts}</div>"
            nb_style = " style=display:none" if picked != "new" else ""
        else:
            card_pick = ""
            nb_style = ""
        body = f"""
<h1>Sell USDT</h1>
<p class='muted small'>Fill this once — your deposit address and exact amount come next.
The quote stays live for {ttl} minutes after you submit.</p>
{err}
<form method=post action=/sell><div class=card>
<input type=hidden name=csrf value='{csrf}'>
{net_html}
<label>Payout method</label><select name=service id=svc>{opts}</select>
<label>Amount to sell</label>
<div class=swapbox><div class=grow><span class=lab>You send</span>
<input name=usd id=usd inputmode=decimal placeholder="0.00"
 value="{_esc(p.get('usd', ''))}" required aria-label="Amount in USDT"></div>
<span class=chip><span class="ic usdt">₮</span>USDT</span></div>
<div class=swaparrow>↓</div>
<div class=swapbox><div class=grow><span class=lab>You receive (approx)</span>
<div class=out id=recv>₹ —</div></div>
<span class=chip><span class="ic inr">₹</span>INR</span></div>
<p class=hint id=amthint></p>
<h2>Bank for your INR payout</h2>
{card_pick}
<div id=newbank{nb_style}>
<label>Account holder name</label>
<input name=holder value="{_esc(p.get('holder', ''))}">
<label>Bank name</label>
<input name=bank value="{_esc(p.get('bank', ''))}">
<label>Account number</label>
<input name=account inputmode=numeric value="{_esc(p.get('account', ''))}">
<label>IFSC</label>
<input name=ifsc value="{_esc(p.get('ifsc', ''))}">
</div>
<div style="margin-top:18px"><button class=btn id=go>Get my deposit address →</button></div>
</div></form>
<p class='muted small'>Questions? {_support_html(support)}</p>"""
        # the live limits/preview script is a plain string (no f-string) so the
        # JS braces stay readable; META carries lo/hi/rate/name per method
        body += ("<script>var META=" + meta_js + """;
var svc=document.getElementById('svc'),usd=document.getElementById('usd'),
    hint=document.getElementById('amthint'),go=document.getElementById('go'),
    recv=document.getElementById('recv');
function inr(n){return '\\u20b9'+n.toLocaleString('en-IN',{maximumFractionDigits:0})}
function upd(){
  var m=META[svc.value];if(!m){hint.textContent='';return}
  var raw=(usd.value||'').replace(/[,$\\s]/g,''),v=parseFloat(raw);
  usd.min=m.lo;usd.max=m.hi;
  var base=m.name+': min '+m.lo+'$ \\u2013 max '+m.hi+'$ \\u00b7 \\u20b9'+m.rate+'/$';
  if(!raw||isNaN(v)){hint.className='hint';hint.textContent=base;
    recv.textContent='\\u20b9 \\u2014';go.disabled=false;go.style.opacity=1;return}
  if(v<m.lo){hint.className='hint bad';
    hint.textContent='\Minimum for '+m.name+' is '+m.lo+'$ \\u2014 enter '+m.lo+'$ or more.';
    recv.textContent='\\u20b9 \\u2014';go.disabled=true;go.style.opacity=.55;return}
  if(v>m.hi){hint.className='hint bad';
    hint.textContent='\Maximum for '+m.name+' is '+m.hi+'$ \\u2014 enter '+m.hi+'$ or less.';
    recv.textContent='\\u20b9 \\u2014';go.disabled=true;go.style.opacity=.55;return}
  hint.className='hint';hint.textContent=base;
  recv.textContent=inr(v*m.rate);
  go.disabled=false;go.style.opacity=1}
svc.addEventListener('change',upd);usd.addEventListener('input',upd);upd();
var pick=document.getElementById('cardpick'),nb=document.getElementById('newbank');
function bankReq(on){['holder','bank','account','ifsc'].forEach(function(n){
  var el=document.getElementsByName(n)[0];if(el)el.required=on});}
function updBank(){
  if(!pick){bankReq(true);return}
  var sel=pick.querySelector('input:checked');
  var isNew=!sel||sel.value==='new';
  nb.style.display=isNew?'':'none';bankReq(isNew);}
if(pick)pick.addEventListener('change',updBank);updBank();
</script>""")
        body += _fabs_html(support, whatsapp)
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
               for k in ("service", "usd", "holder", "bank", "account", "ifsc",
                         "network", "card_id")}
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
    # a digit card_id means "use my saved bank" — its ownership is verified in
    # the session below; anything else is a new-bank submission and validates here
    reuse_card_id = int(prefill["card_id"]) if prefill["card_id"].isdigit() else None
    details = ""
    if reuse_card_id is None:
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

        if reuse_card_id is not None:
            card = await s.get(BankCard, reuse_card_id)
            if card is None or card.user_id != uid:
                # not theirs (or gone) — never pay out to a guessed card id
                return await _sell_form(request, "Please pick your bank again.",
                                        {**prefill, "card_id": ""})
        else:
            card = await s.scalar(select(BankCard)
                                  .where(BankCard.user_id == uid,
                                         BankCard.details == details)
                                  .order_by(BankCard.id.desc()))
            if card is None:
                card = BankCard(user_id=uid, label=make_bank_label(details),
                                details=details)
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
        whatsapp = await get_whatsapp(s)
        ttl = await get_deposit_ttl(s)
        pos = (await queue_position(s, order.id)
               if order.status == OrderStatus.PENDING_PAYOUT.value else 0)
        card = (await s.get(BankCard, order.bank_card_id)
                if order.bank_card_id else None)
    bank_label = (card.label if card
                  else SERVICES.get(order.service, order.service))
    fabs = _fabs_html(support, whatsapp)
    csrf = await _csrf(f"o:{token}")
    st = order.status
    amt = texts.usd_str(order.usd_amount)
    dec = f".{amt.split('.')[1]}" if "." in amt else ""
    net_label = "BEP20 (BSC)" if order.network == "BEP20" else "TRC20 (TRON)"
    net_dot = ("<span class=d style='background:#f0b90b'></span>"
               if order.network == "BEP20" else
               "<span class=d style='background:#2470ff'></span>")
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
        warn = (f"<b>Include the {dec}</b> — send the EXACT amount, decimals and all. "
                f"A wrong amount may not auto-detect." if dec else
                "<b>Send the exact amount.</b>")
        body = f"""
<h1>Send your USDT {tagline}</h1>
<div class=banner><b>You'll receive ₹{order.inr_amount:,.2f}</b>
<span class=muted>→ {_esc(bank_label)}</span><br>
<span class='muted small'>⏳ Quote expires in <span id=cd class=count>--:--</span>
· auto-verified in seconds after it confirms</span></div>
<div class=card>
<b>{net_dot} On {_esc(net_label)} — copy the address</b>
<span class=addr id=addr>{_esc(show_addr)}</span>
<button class="btn ghost" onclick="copyAddr(this)">Copy address</button>
<img class=qrimg src="/o/{_esc(token)}/qr.png" alt="Deposit QR"
 onerror="this.remove()">
<b>Then send exactly</b>
<div class=amtbox><div class=l>send exactly</div><div class=v>{_esc(amt)} USDT</div></div>
<p class='muted small' style="margin:6px 0 0">{warn}</p>
</div>
<button class=btn id=checkbtn onclick="checkNow()">I've sent it — check it</button>
<div id=checking class="banner warn" style="display:none">Checking the blockchain…
a fresh transfer takes ~a minute to confirm. This page updates automatically.</div>
{claim_form}
<form method=post action="/o/{_esc(token)}/cancel"
 onsubmit="return confirm('Cancel this order? Only do this if you have NOT paid.')">
<input type=hidden name=csrf value='{csrf}'>
<button class="btn ghost">No, I'm not paid — cancel</button></form>
<p class='muted small'>Need help? {_support_html(support)} — mention {tagline}</p>
<script>
var deadline={deadline};
function tick(){{var s=Math.max(0,Math.floor((deadline-Date.now())/1000));
document.getElementById('cd').textContent=Math.floor(s/60)+':'+String(s%60).padStart(2,'0');
if(s<=0)setTimeout(function(){{location.reload()}},4000);}}
tick();setInterval(tick,1000);
function copyAddr(b){{navigator.clipboard.writeText(document.getElementById('addr').textContent.trim())
.then(function(){{b.textContent='Copied';setTimeout(function(){{b.textContent='Copy address'}},1500)}});}}
function checkNow(){{var b=document.getElementById('checkbtn');b.disabled=true;b.style.opacity=.6;
document.getElementById('checking').style.display='block';
fetch('/o/{_esc(token)}/check',{{method:'POST',headers:{{'X-CSRF':'{csrf}'}}}});}}
setInterval(function(){{fetch('/o/{_esc(token)}/status.json').then(r=>r.json())
.then(function(j){{if(j.status!=='{st}')location.reload();}}).catch(function(){{}});}},6000);
</script>"""
        return _page(f"Order {texts.tag(order.id)} — send USDT", body + fabs)

    if st in (OrderStatus.DEPOSIT_RECEIVED.value, OrderStatus.PENDING_PAYOUT.value):
        qtxt = (f"You're <b>#{pos}</b> in the payout queue." if pos
                else "Finalizing your payout…")
        body = f"""
<h1>Deposit verified {tagline}</h1>
<div class="banner ok"><b>{texts.usd_str(order.usd_amount)} USDT received &amp; verified on-chain.</b><br>
<span class=small>₹{order.inr_amount:,.2f} is being paid to
{_esc(bank_label)}. {qtxt}</span></div>
<div class=card class=small><span class=muted>TX:</span>
<code>{_esc((order.txid or '')[:20])}…</code>
{f'<a href="{_esc(explorer_tx(order.txid))}" target=_blank rel=noopener>view on explorer</a>' if order.txid and order.txid != 'manual' else ''}</div>
<p class='muted small'>This page refreshes automatically — you can keep it open or come
back later from <a href="/my">My orders</a>. {_support_html(support)}</p>
<script>setInterval(function(){{fetch('/o/{_esc(token)}/status.json').then(r=>r.json())
.then(function(j){{if(j.status!=='{st}')location.reload();}}).catch(function(){{}});}},8000);</script>"""
        return _page(f"Order {texts.tag(order.id)} — verified", body + fabs)

    if st == OrderStatus.COMPLETED.value:
        body = f"""
<h1>Paid! {tagline}</h1>
<div class="banner ok"><b>₹{order.inr_amount:,.2f} sent to {_esc(bank_label)}.</b><br>
<span class=small>Thanks for trading with us — proof is shared on every deal.</span></div>
<a class=btn href="/sell">Sell more USDT</a>
<a class="btn ghost" href="/my">All my orders</a>
<p class='muted small'>{_support_html(support)}</p>"""
        return _page(f"Order {texts.tag(order.id)} — paid", body + fabs)

    if st in (OrderStatus.EXPIRED.value, OrderStatus.CANCELLED.value):
        head = ("This quote expired" if st == OrderStatus.EXPIRED.value
                else "Order cancelled")
        note = ("No deposit arrived in time — don't send anything to the old "
                "address/amount now." if st == OrderStatus.EXPIRED.value else
                "Nothing is pending on this order.")
        pending_claim = ""
        if order.claim_txid:
            pending_claim = ("<div class='banner warn'>Your TXID is <b>under review</b> — "
                             "our team verifies it and pays out if it checks out. "
                             "This page updates automatically.</div>")
        body = f"""
<h1>{head} {tagline}</h1>
<div class=banner>{note}</div>
{pending_claim}
<a class=btn href="/sell">Start a fresh order</a>
{claim_form if not order.claim_txid else ''}
<p class='muted small'>{_support_html(support)} — mention {tagline}</p>
<script>setInterval(function(){{fetch('/o/{_esc(token)}/status.json').then(r=>r.json())
.then(function(j){{if(j.status!=='{st}')location.reload();}}).catch(function(){{}});}},8000);</script>"""
        return _page(f"Order {texts.tag(order.id)}", body + fabs)

    # refund / rejected / anything else — simple status card
    body = f"""
<h1>Order {tagline}</h1>
<div class=banner>Status: <b>{_esc(st.replace('_', ' '))}</b></div>
<p class='muted small'>{_support_html(support)} — mention {tagline}</p>"""
    return _page(f"Order {texts.tag(order.id)}", body + fabs)




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

def _ist(dt) -> str:
    if not dt:
        return "—"
    ist = dt.replace(tzinfo=timezone.utc) + timedelta(hours=5, minutes=30)
    return ist.strftime("%d %b %Y, %I:%M %p") + " IST"


def _short_tx(txid: str | None) -> str:
    if not txid or txid == "manual":
        return ""
    return f"{txid[:10]}…{txid[-6:]}"


async def my_orders(request: web.Request):
    uid = await _uid_from_cookie(request)
    if uid is None:
        return _page("My orders", "<h1>My orders</h1><div class=banner>No orders on "
                     "this device yet — they appear here after your first order.</div>"
                     "<a class=btn href='/sell'>Sell USDT</a>")
    async with Session() as s:
        orders = (await s.scalars(select(Order).where(Order.user_id == uid)
                                  .order_by(Order.id.desc()).limit(20))).all()
        support = await get_support(s)
        whatsapp = await get_whatsapp(s)
        card_ids = [o.bank_card_id for o in orders if o.bank_card_id]
        cards = {c.id: c for c in (await s.scalars(
            select(BankCard).where(BankCard.id.in_(card_ids)))).all()} if card_ids else {}
    if not orders:
        return _page("My orders", "<h1>My orders</h1><div class=banner>No orders yet."
                     "</div><a class=btn href='/sell'>Sell USDT</a>")
    cls = {OrderStatus.COMPLETED.value: "ok", OrderStatus.PENDING_PAYOUT.value: "warn",
           OrderStatus.DEPOSIT_RECEIVED.value: "info",
           OrderStatus.AWAITING_DEPOSIT.value: "info",
           OrderStatus.CANCELLED.value: "danger", OrderStatus.EXPIRED.value: "danger"}
    nice = {OrderStatus.AWAITING_DEPOSIT.value: "waiting for your USDT",
            OrderStatus.DEPOSIT_RECEIVED.value: "deposit verified",
            OrderStatus.PENDING_PAYOUT.value: "payout in progress",
            OrderStatus.COMPLETED.value: "paid",
            OrderStatus.EXPIRED.value: "expired",
            OrderStatus.CANCELLED.value: "cancelled",
            OrderStatus.REFUND_REQUESTED.value: "refund requested",
            OrderStatus.REFUNDED.value: "refunded"}
    blocks = []
    for o in orders:
        if not o.web_token:
            continue
        card = cards.get(o.bank_card_id)
        net_label = "BEP20 (BSC)" if o.network == "BEP20" else "TRC20 (TRON)"
        addr = o.display_address or o.deposit_address or ""
        kv = [
            ("Date", _ist(o.created_at)),
            ("You sent", f"{texts.usd_str(o.usd_amount)} USDT · {_esc(net_label)}"),
            ("Rate", f"₹{o.rate_inr:g}/$"),
            ("You receive", f"₹{o.inr_amount:,.2f} via "
                            f"{_esc(SERVICES.get(o.service, o.service))}"),
        ]
        if addr:
            kv.append(("Deposit address", f"<code>{_esc(addr[:14])}…{_esc(addr[-6:])}</code>"))
        tx = _short_tx(o.txid)
        if tx:
            link = (f" · <a href='{_esc(explorer_tx(o.txid))}' target=_blank "
                    f"rel=noopener>explorer</a>")
            kv.append(("Deposit TX", f"<code>{_esc(tx)}</code>{link}"))
        if o.claim_txid:
            kv.append(("Claim TX", f"<code>{_esc(_short_tx(o.claim_txid))}</code> "
                                   "<span class='muted small'>(under review)</span>"))
        rows = "".join(f"<div class=kv><span class=k>{k}</span>"
                       f"<span class=v>{v}</span></div>" for k, v in kv)
        bank = ""
        if card:
            det = "".join(
                f"<div class=kv><span class=v style='text-align:left;font-weight:500'>"
                f"{_esc(line.strip())}</span></div>"
                for line in card.details.splitlines() if line.strip())
            bank = (f"<details><summary>Payout bank — {_esc(card.label)}</summary>"
                    f"<div style='margin-top:8px'>{det}</div></details>")
        blocks.append(
            f"<div class=card><b>{texts.tag(o.id)}</b> "
            f"<span class='badge {cls.get(o.status, '')}'>"
            f"{_esc(nice.get(o.status, o.status.replace('_', ' ')))}</span>"
            f"<div style='margin-top:10px'>{rows}</div>{bank}"
            f"<a class='btn ghost' style='margin-top:12px' "
            f"href='/o/{_esc(o.web_token)}'>Open live order page →</a></div>")
    return _page("My orders", f"<h1>My orders</h1>{''.join(blocks)}"
                 "<a class=btn href='/sell'>New order</a>"
                 + _fabs_html(support, whatsapp))




# ── legal / disclosure pages ─────────────────────────────────────────────────
# Deliberately factual: they describe what this desk actually does — no invented
# licences, insurance, or regulator names. On a money site, fabricated claims
# are worse than none (ad reviews and chargeback disputes both check them).

_LEGAL: dict[str, tuple[str, str]] = {
    "terms": ("Terms of Use", """
<h2>What this service is</h2>
<p class=muted>This site operates an over-the-counter (OTC) desk where you sell
USDT (Tether) and receive Indian rupees to your own bank account by UPI, IMPS,
CDM or cheque. Every deposit is verified on the blockchain before payout.</p>
<h2>Your order</h2>
<p class=muted>The rate shown when you place an order is the rate you are paid
at. Each order gets a unique exact amount — send exactly that amount, on the
network you chose, within the quote window shown on your order page. A deposit
that arrives late, on the wrong network, or with a different amount may not
auto-detect; contact support with your transaction hash and order ID.</p>
<h2>Eligibility and acceptable use</h2>
<p class=muted>You must be 18 or older and the owner of both the crypto you
send and the bank account you are paid to. Third-party payouts are not offered.
We may decline, hold, or cancel any order that appears fraudulent, automated,
or inconsistent with our AML policy, and may request information about the
source of funds before paying out.</p>
<h2>Payouts and refunds</h2>
<p class=muted>Verified deposits enter the payout queue and are paid by our
admins in order. If a verified deposit cannot be paid out, the USDT is returned
to a return address you provide. Unverified or never-received deposits cannot
be refunded because nothing was received.</p>
<h2>Liability</h2>
<p class=muted>On-chain transfers are irreversible. We are not liable for
transfers sent to a different address, on a different network, or outside an
order — double-check the address and amount against your live order page, which
is the only authoritative source. Our total liability for any order is limited
to the value of that order.</p>
<h2>Changes</h2>
<p class=muted>Terms may be updated; the version on this page applies to new
orders from the moment it is published.</p>"""),

    "privacy": ("Privacy &amp; Cookies Policy", """
<h2>What we collect</h2>
<p class=muted>To process your payout we collect the bank details you enter
(account holder, bank, account number, IFSC), your order details, and your IP
address (used only for rate-limiting and abuse prevention). We do not collect
names, emails, phone numbers, or documents through this site.</p>
<h2>Cookies</h2>
<p class=muted>One cookie. It holds a random identifier so your orders appear
under “My orders” on this browser. No analytics, no trackers, no third-party
cookies, no advertising pixels.</p>
<h2>What we do with it</h2>
<p class=muted>Your bank details are used to pay you and are visible to the
desk's admins for that purpose only. We do not sell or share your data with
anyone else. Blockchain transactions are public by nature — your deposit's
transaction hash exists on a public ledger independent of us.</p>
<h2>Retention and your rights</h2>
<p class=muted>Order and payout records are retained for the desk's accounting
and dispute handling. To correct your saved bank details, simply add a new bank
on your next order. For removal requests, contact support with your order ID —
we honour them once there is no open order or dispute.</p>"""),

    "risks": ("Cryptoasset Risks", """
<h2>Read this before you trade</h2>
<p class=muted>Crypto assets are volatile. The INR value of USDT can move at
any time; the rate you are paid is the rate locked when you placed the order,
not the rate at any later moment.</p>
<h2>Transfers are irreversible</h2>
<p class=muted>There is no bank to reverse an on-chain transfer. USDT sent to a
wrong address, on a wrong network, or in a wrong amount may be unrecoverable.
Always copy the address from your live order page and send the exact amount
shown, on the network shown.</p>
<h2>Not a deposit, not insured</h2>
<p class=muted>Crypto assets are not bank deposits, are not legal tender, and
are not covered by any deposit-insurance scheme. This desk is not a bank.</p>
<h2>Network fees and timing</h2>
<p class=muted>Your wallet may charge a network fee on top of the amount you
send — the amount that must ARRIVE is the exact amount shown on your order.
Blockchain congestion can delay confirmation beyond the quote window; if that
happens, submit your transaction hash from the order page and support will
review it.</p>
<h2>No advice</h2>
<p class=muted>Nothing on this site is investment, tax, or legal advice.</p>"""),

    "transactions": ("Transaction &amp; Pricing Information", """
<h2>Pricing</h2>
<p class=muted>The ₹/$ rate shown on the home page and on your order is
all-inclusive — there are no added fees, spreads, or deductions on our side.
What you see is what is paid to your bank. Your own wallet's network fee is the
only cost outside our control.</p>
<h2>Why the amount has odd paise</h2>
<p class=muted>Each order is tagged with a unique exact amount (unique cents).
That is how our scanner matches YOUR deposit to YOUR order automatically,
without asking for screenshots. It must arrive exactly.</p>
<h2>Timing</h2>
<p class=muted>Quotes stay live for the window shown on the order page.
Deposits are typically verified within seconds of the transaction confirming
on-chain. Bank payout follows in queue order — see the current estimate on the
home page.</p>
<h2>Tracking</h2>
<p class=muted>Your order page updates live, and every order stays available
under “My orders” on the browser you ordered from, including the deposit
transaction hash with a public block-explorer link.</p>
<h2 id=complaints>Complaints</h2>
<p class=muted>Message support (buttons on every page) with your order ID
(#ORD…). Include your transaction hash for deposit issues. Complaints are
handled by the desk's admins directly — the same people who run payouts — and
are typically answered the same day.</p>"""),

    "aml": ("AML / Clean-Funds Policy", """
<h2>Clean funds only</h2>
<p class=muted>Every rupee paid out by this desk comes from verified,
legitimate sources — mutual and stock-market funds, cash deposits, credit-card
and payment-gateway funds. We stand behind this guarantee to protect your bank
account from freezes and holds.</p>
<h2>What we require from you</h2>
<p class=muted>Sell only crypto you own, and only to a bank account in your
name. We may pause any order to ask about the source of funds, and we decline
orders that appear linked to fraud, scams, gambling proceeds, sanctioned
parties, or any unlawful activity.</p>
<h2>Monitoring</h2>
<p class=muted>Orders are screened for unusual patterns (volume, frequency,
mismatched details). Records of orders and payouts are retained. Where the law
requires it, suspicious activity is reported to the relevant authorities.</p>
<h2>Our discretion</h2>
<p class=muted>We may refuse service to anyone, hold a payout pending review,
or return a verified deposit instead of paying it out, when we judge that
completing the order would break this policy.</p>"""),
}


async def legal_page(request: web.Request):
    slug = request.match_info["slug"]
    doc = _LEGAL.get(slug)
    if doc is None:
        raise web.HTTPNotFound()
    title, body_html = doc
    async with Session() as s:
        support = await get_support(s)
        whatsapp = await get_whatsapp(s)
    body = (f"<h1>{title}</h1>{body_html}"
            f"<p class='muted small'>Questions about this policy? "
            f"{_support_html(support)}</p>"
            + _fabs_html(support, whatsapp))
    return _page(f"{html.unescape(title)} — P2P Desk", body)


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
        web.get("/legal/{slug}", legal_page),
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
