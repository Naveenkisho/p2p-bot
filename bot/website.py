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

import asyncio
import hashlib
import hmac
import html
import json
import logging
import re
import secrets
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

import aiohttp
from aiohttp import web
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

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
    get_support_email,
    get_whatsapp,
    set_setting,
    site_secret,
)
from .helpers import (
    TXID_RE,
    explorer_tx,
    norm_txid,
    queue_position,
    try_transition,
    txid_used_elsewhere,
    unsub_valid,
)
from .models import Account, BankCard, Order, OrderStatus, Ticket, Unsubscribe, User, utcnow
from .qr import qr_png

log = logging.getLogger(__name__)

COOKIE = "p2p_web"
_BRAND_NAME_WEB = "IndiaXchange"
COOKIE_TTL = 365 * 24 * 3600

# Statuses a customer may still submit a claim TXID for (mirrors the bot).
_CLAIMABLE = (OrderStatus.AWAITING_DEPOSIT.value, OrderStatus.EXPIRED.value,
              OrderStatus.CANCELLED.value)

# per-IP order-creation throttle: ip -> recent creation timestamps
_order_times: dict[str, deque] = {}


# ── identity / signing ────────────────────────────────────────────────────────

async def _secret() -> bytes:
    """Site-scoped signing secret (shared with the bulk sender via db)."""
    return await site_secret()


async def _sign_uid(uid: int) -> str:
    """Signed cookie value. Account cookies also bind the account's session
    version, so bumping sess_ver (password reset, Google takeover) instantly
    invalidates every previously issued cookie on every device."""
    extra = ""
    if uid <= -_ACCT_BASE:
        async with Session() as s:
            a = await s.get(Account, -uid - _ACCT_BASE)
        extra = f":{(getattr(a, 'sess_ver', 0) or 0) if a else 0}"
    mac = hmac.new(await _secret(), f"web:{uid}{extra}".encode(),
                   hashlib.sha256).hexdigest()
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
# support-ticket throttle: bounds junk tickets per IP
_ticket_times: dict[str, deque] = {}
_TICKET_MAX_PER_HOUR = 5


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


# ── customer accounts (signup gate) ──────────────────────────────────────────
# Selling requires an account: Google sign-in (ID token verified server-side)
# or email + phone + password. Each account owns a stable negative uid,
# -(2^48 + account.id) — anonymous browser uids stay below 2^47 in magnitude,
# Telegram ids are positive, so the three ranges can never collide. The uid
# goes into the SAME signed cookie the site already uses, and the browser's
# anonymous orders/cards/tickets are re-parented onto the account at login so
# history follows the person, not the device.

_ACCT_BASE = 1 << 48
_STOCK_TIERS = ("100", "200", "500", "1000", "2000+")

_signup_times: dict[str, deque] = {}
_SIGNUP_MAX_PER_HOUR = 8
_login_times: dict[str, deque] = {}
_LOGIN_MAX_PER_HOUR = 20

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _acct_uid(acct_id: int) -> int:
    return -(_ACCT_BASE + acct_id)


async def _account_from_request(request: web.Request) -> Account | None:
    """The signed-in account, or None for anonymous/visitor cookies."""
    uid = await _uid_from_cookie(request)
    if uid is None or uid > -_ACCT_BASE:
        return None
    async with Session() as s:
        return await s.get(Account, -uid - _ACCT_BASE)


def _hash_pw_sync(password: str, salt_hex: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(),
                               bytes.fromhex(salt_hex), 200_000).hex()


async def _hash_pw(password: str, salt_hex: str) -> str:
    """PBKDF2 is ~100ms of CPU — run it off the shared event loop so a burst
    of sign-in attempts can't stall the bot, panel and deposit scanner."""
    return await asyncio.to_thread(_hash_pw_sync, password, salt_hex)


# A throwaway salt for the "email doesn't exist" branch of sign-in, so a wrong
# email costs the same PBKDF2 as a wrong password — no timing oracle for
# whether an email is registered.
_DUMMY_SALT = secrets.token_hex(16)


def _valid_phone(phone: str) -> bool:
    digits = re.sub(r"[\s\-()]", "", phone).lstrip("+")
    # ASCII 0-9 only — str.isdigit() also accepts Arabic-Indic/Devanagari digits,
    # which would store an un-diallable number and break bulk SMS normalisation.
    return bool(digits) and all("0" <= c <= "9" for c in digits) and 7 <= len(digits) <= 15


def _norm_phone(phone: str) -> str:
    """Store phones with separators stripped (keep a leading +)."""
    plus = phone.lstrip().startswith("+")
    digits = re.sub(r"[\s\-()]", "", phone).lstrip("+")
    return ("+" if plus else "") + digits


def _valid_password(pw: str) -> str:
    """'' when acceptable, else the error message to show."""
    if not 8 <= len(pw) <= 128:
        return "Password must be 8+ characters."
    if not re.search(r"[^A-Za-z0-9]", pw):
        return "Password must include a special character, e.g. @ or #."
    return ""


async def _everify_token(uid: int, email: str) -> str:
    """Signed proof that THIS browser (uid) OTP-verified THIS email — issued
    by /signup/otp/check, demanded again by signup_post, so the client-side
    gating can't simply be bypassed with a hand-built POST."""
    msg = f"everify:{uid}:{(email or '').strip().lower()}".encode()
    return hmac.new(await _secret(), msg, hashlib.sha256).hexdigest()[:32]


def _safe_next(nxt: str) -> str:
    """Only same-site paths — never an absolute/protocol-relative URL, so the
    post-login redirect can't be pointed at another site."""
    return nxt if nxt.startswith("/") and not nxt.startswith("//") else "/sell"


async def _google_claims(credential: str) -> dict | None:
    """Verify a Google ID token. Google's tokeninfo endpoint checks the
    signature/expiry; we check the token is OURS (aud), from Google (iss),
    and carries a verified email — anything less and sign-in is refused."""
    if not credential or len(credential) > 4096:
        return None
    try:
        async with aiohttp.ClientSession(trust_env=True) as sess:
            async with sess.get("https://oauth2.googleapis.com/tokeninfo",
                                params={"id_token": credential},
                                timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    return None
                claims = await r.json()
    except Exception:
        log.warning("google tokeninfo lookup failed", exc_info=True)
        return None
    if claims.get("aud") != settings.google_client_id:
        return None
    if claims.get("iss") not in ("accounts.google.com",
                                 "https://accounts.google.com"):
        return None
    if str(claims.get("email_verified")).lower() != "true":
        return None
    if not claims.get("email") or not claims.get("sub"):
        return None
    return claims


async def _ensure_user_row(s, uid: int, name: str) -> None:
    if await s.get(User, uid) is None:
        s.add(User(id=uid, username="web", first_name=(name or "Web")[:60]))
        await s.flush()


async def _adopt_anon(s, anon_uid: int | None, acct_uid: int) -> None:
    """Re-parent this browser's anonymous history onto the account, and carry a
    ban with it so signing up can't launder away an admin's ban. Only ever
    anonymous-negative → account-negative; Telegram uids are untouchable."""
    if (anon_uid is None or anon_uid >= 0 or anon_uid <= -_ACCT_BASE
            or anon_uid == acct_uid):
        return
    anon = await s.get(User, anon_uid)
    if anon is not None and anon.banned:
        acct_user = await s.get(User, acct_uid)
        if acct_user is not None:
            acct_user.banned = True     # the ban follows the person, not the uid
    for model in (Order, BankCard, Ticket):
        await s.execute(update(model).where(model.user_id == anon_uid)
                        .values(user_id=acct_uid))


async def _login_account(request: web.Request, resp, acct: Account,
                         anon_uid: int | None) -> None:
    """Issue the account's session cookie + adopt the browser's anon history."""
    uid = _acct_uid(acct.id)
    async with Session() as s:
        await _ensure_user_row(s, uid, acct.name or acct.email)
        await _adopt_anon(s, anon_uid, uid)
        row = await s.get(Account, acct.id)
        if row is not None:
            row.last_login = utcnow()
        await s.commit()
    _set_uid_cookie(resp, await _sign_uid(uid), _is_https(request))


async def _notify_signup(request: web.Request, acct: Account) -> None:
    """Ping the admins about a completed signup (best-effort)."""
    bot = request.app.get("bot")
    if bot is None:
        return
    try:
        from .helpers import notify_admins
        via = "Google" if acct.provider == "google" else "email"
        lines = [f"🆕 <b>Website signup</b> — {html.escape(acct.email)}",
                 f"Name: {html.escape(acct.name or '—')}",
                 f"Phone: {html.escape(acct.phone or '—')}",
                 f"Daily stock: {html.escape(acct.stock or '—')} USDT",
                 f"Via: {via} · full list in the panel → Signups"]
        await notify_admins(bot, "\n".join(lines))
    except Exception:
        log.exception("signup admin notify failed")


# ── HTML shell ────────────────────────────────────────────────────────────────

def _esc(v) -> str:
    return html.escape("" if v is None else str(v))


def _ld(obj) -> str:
    """A JSON-LD <script> block. `<` is JSON-escaped so no value inside the
    object (article titles, business names) can ever close the script tag
    early and turn into live markup."""
    return ("<script type='application/ld+json'>"
            + json.dumps(obj).replace("<", "\\u003c") + "</script>")


# ── marketing pixels (admin-pasted head code, injected on public pages) ──────
# The desk owner pastes each provider's FULL snippet (the <script> block from
# Meta Events Manager / Google) in the panel's Marketing tab; it is injected
# verbatim into <head> on public pages only. This is admin-authored code (same
# trust model as Google Tag Manager) — only the authenticated panel can set it,
# and it never touches private/noindex order or account pages. Cached in-process
# (sync-readable from _page); the panel calls load_tracking() on save and
# start_site() loads it at boot.
_TRACKING = {"meta": "", "google": "", "custom": ""}
_TRACK_MAX = 20000        # sanity cap per snippet


async def load_tracking() -> None:
    async with Session() as s:
        _TRACKING["meta"] = (await get_setting(s, "track_meta_code") or "")[:_TRACK_MAX]
        _TRACKING["google"] = (await get_setting(s, "track_google_code") or "")[:_TRACK_MAX]
        _TRACKING["custom"] = (await get_setting(s, "track_custom_code") or "")[:_TRACK_MAX]


def _tracking_head() -> str:
    """The admin-pasted tracking snippets, injected verbatim (trusted input)."""
    parts = []
    if _TRACKING["meta"].strip():
        parts.append("<!-- Meta Pixel -->" + _TRACKING["meta"])
    if _TRACKING["google"].strip():
        parts.append("<!-- Google tag -->" + _TRACKING["google"])
    if _TRACKING["custom"].strip():
        parts.append("<!-- Extra tracking -->" + _TRACKING["custom"])
    return "".join(parts)


def _tracking_active() -> bool:
    return any(v.strip() for v in _TRACKING.values())


@web.middleware
async def _sec_headers(request: web.Request, handler):
    """Security headers on every response (incl. redirects/404s) + HSTS on HTTPS."""
    try:
        resp = await handler(request)
    except web.HTTPException as exc:
        resp = exc          # HTTPException is itself a Response — decorate + return
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    # strict-origin-when-cross-origin: same-origin requests keep the full path
    # (fine), but anything leaving our origin sends ONLY the bare origin — so a
    # secret /o/<token> path never leaks in a Referer, while Google Identity
    # Services still receives the origin it must match against the OAuth
    # client's authorized origins (no-referrer silently breaks the button).
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    # no-store by default: these pages are dynamic and often per-account (My
    # orders, the sell form's saved banks, order pages). Without this a caching
    # proxy like Cloudflare can store one visitor's page and serve it to
    # another. Endpoints that ARE safe to cache (QR PNGs) set their own
    # Cache-Control, which setdefault leaves untouched.
    resp.headers.setdefault("Cache-Control", "private, no-store")
    if _is_https(request):
        resp.headers.setdefault("Strict-Transport-Security",
                                "max-age=63072000; includeSubDomains")
    return resp


@lru_cache(maxsize=512)
def _gen_qr(address: str) -> bytes | None:
    """Auto-generated QR bytes for an address — memoized (a pure function; the QR
    for a given address never changes), so repeated /qr.png hits don't re-render."""
    return qr_png(address)


_FAVICON = (
    "<link rel=icon type='image/svg+xml' href='data:image/svg+xml;base64,"
    "PHN2ZyB4bWxucz0naHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmcnIHZpZXdCb3g9JzAgMCAzMi"
    "AzMic+PHJlY3Qgd2lkdGg9JzMyJyBoZWlnaHQ9JzMyJyByeD0nNycgZmlsbD0nIzBlMTMzMCcv"
    "PjxjaXJjbGUgY3g9JzE2JyBjeT0nMTYnIHI9JzknIGZpbGw9J25vbmUnIHN0cm9rZT0nIzAwYz"
    "I2Zicgc3Ryb2tlLXdpZHRoPScyLjQnLz48dGV4dCB4PScxNicgeT0nMjEnIGZvbnQtZmFtaWx5"
    "PSdBcmlhbCxzYW5zLXNlcmlmJyBmb250LXNpemU9JzEzJyBmb250LXdlaWdodD0nOTAwJyBmaW"
    "xsPScjMDBjMjZmJyB0ZXh0LWFuY2hvcj0nbWlkZGxlJz7igrk8L3RleHQ+PC9zdmc+'>")


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
html,body{overflow-x:clip}
body{margin:0;color:var(--text);line-height:1.55;
 background:radial-gradient(90% 340px at 50% 0,#e4faee 0%,#ffffff 78%) no-repeat,var(--bg);
 font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
 font-feature-settings:"tnum" 1;-webkit-font-smoothing:antialiased}
.wrap{max-width:680px;margin:0 auto;
 padding:0 16px calc(120px + env(safe-area-inset-bottom,0px))}
a{color:var(--accent-dark);text-decoration:none;font-weight:600}
h1{font-size:2rem;font-weight:900;letter-spacing:-.035em;line-height:1.12;
 margin:26px 0 10px;text-wrap:balance}
h1 .g{color:var(--accent-dark)}
h2{font-size:1.12rem;font-weight:800;letter-spacing:-.01em;margin:28px 0 10px}
.topbar{display:flex;align-items:center;gap:4px;padding:10px 0;flex-wrap:nowrap;
 position:sticky;top:0;z-index:40;background:rgba(255,255,255,.9);
 backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);
 border-bottom:1px solid var(--border);
 margin:0 -16px;padding-left:16px;padding-right:16px}
.topbar a{flex-shrink:0}
.topbar .brand{font-weight:900;font-size:1.08rem;letter-spacing:-.02em;color:var(--text);
 display:flex;align-items:center;gap:8px;margin-right:6px}
.topbar .dot{width:11px;height:11px;border-radius:50%;background:var(--accent);
 box-shadow:0 0 0 4px var(--accent-soft)}
.topbar .sp{flex:1}
/* inline nav — shown on wider screens, collapsed into the ☰ menu on phones */
.topnav{display:none;align-items:center;gap:2px}
.topnav a{color:var(--muted);font-weight:700;font-size:.85rem;padding:8px 11px;
 border-radius:999px}
.topnav a:hover{background:var(--surface-2);color:var(--text)}
.topbar a.nav.hot{background:var(--navy);color:#fff;font-weight:800;font-size:.85rem;
 padding:9px 16px;border-radius:999px}
.topbar a.nav.hot:hover{background:#000}
.topbar a.nav.me{border:1.5px solid var(--border);color:var(--text);font-weight:700;
 font-size:.85rem;padding:8px 14px;border-radius:999px;max-width:150px;overflow:hidden;
 text-overflow:ellipsis;white-space:nowrap}
.topbar a.nav.me:hover{background:var(--surface-2)}
.menubtn{display:inline-flex;align-items:center;justify-content:center;width:40px;
 height:40px;flex:0 0 40px;border:1.5px solid var(--border);border-radius:12px;
 background:var(--surface);color:var(--text);font-size:1.25rem;line-height:1;
 cursor:pointer;padding:0}
.menubtn:hover{background:var(--surface-2)}
.navmenu{position:absolute;top:calc(100% + 6px);right:16px;min-width:200px;
 background:#fff;border:1px solid var(--border);border-radius:16px;
 box-shadow:0 12px 34px rgba(14,19,48,.16);padding:6px;display:none;z-index:60}
.navmenu.open{display:block}
.navmenu a{display:block;padding:11px 14px;border-radius:10px;color:var(--text);
 font-weight:700;font-size:.95rem}
.navmenu a:hover{background:var(--surface-2)}
@media(min-width:760px){
 .topnav{display:flex}
 .menubtn{display:none}
 .navmenu{display:none!important}
}
.stockpick{display:grid;grid-template-columns:repeat(auto-fill,minmax(88px,1fr));
 gap:8px;margin:8px 0 4px}
.stockpick label{display:block;margin:0;cursor:pointer}
.stockpick input{position:absolute;opacity:0;width:0;height:0}
.stockpick span{display:block;text-align:center;padding:12px 4px;border-radius:14px;
 border:1.5px solid var(--border);background:var(--surface-2);font-weight:800;
 font-size:.92rem;color:var(--text)}
.stockpick input:checked+span{border-color:var(--accent);background:var(--accent-soft);
 box-shadow:0 0 0 3px var(--accent-soft)}
.authwrap{display:grid;gap:10px}
.authtabs{display:flex;gap:8px;margin:14px 0 2px}
.authtabs a{flex:1;text-align:center;padding:11px 8px;border-radius:999px;
 border:1.5px solid var(--border);font-weight:800;font-size:.92rem;color:var(--muted)}
.authtabs a.on{background:var(--navy);color:#fff;border-color:var(--navy)}
.gwrap{display:flex;justify-content:center;margin:14px 0 6px;min-height:44px}
.orline{display:flex;align-items:center;gap:12px;color:var(--faint);
 font-size:.8rem;font-weight:700;letter-spacing:.06em;margin:12px 0}
.orline:before,.orline:after{content:"";flex:1;height:1px;background:var(--border)}
.linkbtn{display:inline;width:auto;padding:0;border:0;background:none;
 color:var(--accent-dark);font:inherit;font-weight:600;cursor:pointer;
 box-shadow:none;text-decoration:underline}
.linkbtn:hover{background:none;color:var(--accent);transform:none;box-shadow:none}
.card{background:transparent;border:0;border-radius:0;box-shadow:none;
 padding:14px 0;margin:6px 0;overflow-wrap:anywhere}
.card.sep+.card.sep{border-top:1px solid var(--border);padding-top:22px;margin-top:14px}
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
.rategrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px}
.rategrid .rt{background:#fff;border:1px solid var(--border);border-radius:18px;
 padding:18px 10px;text-align:center;
 box-shadow:0 10px 24px rgba(14,19,48,.08),0 2px 5px rgba(14,19,48,.04)}
.rategrid .rt .nm{font-weight:800;font-size:.95rem}
.rategrid .rt .pr{font-weight:900;font-size:1.45rem;letter-spacing:-.02em;
 color:var(--accent-dark);margin:4px 0 2px;font-variant-numeric:tabular-nums}
.rategrid .rt .lm{font-size:.76rem;color:var(--faint);font-weight:700}
.rates{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
.rates td{padding:12px 4px;border-bottom:1px solid var(--border)}
.rates tr:last-child td{border-bottom:0}
.rates .r{text-align:right;font-weight:900;font-size:1.12rem;color:var(--accent-dark)}
.hero-badges{display:flex;gap:8px;flex-wrap:wrap;margin:16px 0 4px}
.marq{overflow:hidden;position:relative;margin:14px calc(50% - 50vw) 4px;
 -webkit-mask-image:linear-gradient(90deg,transparent,#000 10%,#000 90%,transparent);
 mask-image:linear-gradient(90deg,transparent,#000 10%,#000 90%,transparent)}
.marq .track{display:flex;gap:52px;width:max-content;align-items:center;
 padding:12px 0;animation:marq 28s linear infinite}
.marq .track.rev{animation:marq 34s linear infinite reverse}
.marq:hover .track{animation-play-state:paused}
@keyframes marq{to{transform:translateX(-50%)}}
.marq .lg{display:inline-flex;align-items:center;gap:10px;white-space:nowrap;
 font-weight:900;font-size:1.3rem;letter-spacing:-.02em}
.marq .lg .mk{font-size:1.15rem}
.marq .lg svg{height:26px;width:auto;flex:0 0 auto;display:block}
.trademarks{font-size:.72rem;color:var(--faint);margin:6px 0 0;font-weight:600}
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
.banner{border:0;border-left:5px solid var(--muted);background:var(--surface-2);
 border-radius:12px;padding:13px 15px;margin:12px 0}
.banner.ok{border-left-color:var(--accent)} .banner.warn{border-left-color:var(--warn)}
.banner.danger{border-left-color:var(--danger)}
.err{color:var(--danger);font-weight:700;margin:10px 0}
details{margin:12px 0}
details summary{cursor:pointer;color:var(--text);font-weight:700;padding:4px 0}
details summary::marker{color:var(--accent-dark)}
.footer{margin-top:0;color:#8b95b8;font-size:.82rem;text-align:center}
.darkband{margin:24px 0;display:grid;grid-template-columns:1fr 1fr;gap:14px}
.darkband .cell{background:#fff;border:1px solid var(--border);
 border-radius:20px;padding:24px 12px;text-align:center;
 box-shadow:0 12px 28px rgba(14,19,48,.10),0 2px 6px rgba(14,19,48,.05);
 animation:floaty 5s ease-in-out infinite;
 transition:transform .18s,box-shadow .18s}
.darkband .cell:nth-child(2),.darkband .cell:nth-child(5){animation-delay:1.6s}
.darkband .cell:nth-child(3),.darkband .cell:nth-child(4){animation-delay:3.2s}
.darkband .cell:hover{transform:translateY(-6px);
 box-shadow:0 20px 44px rgba(14,19,48,.15),0 4px 10px rgba(14,19,48,.07)}
.darkband .k{color:var(--faint);font-size:.68rem;font-weight:800;letter-spacing:.07em;
 text-transform:uppercase;margin-bottom:6px}
.darkband .v{color:var(--navy);font-size:1.5rem;font-weight:900;letter-spacing:-.02em;
 font-variant-numeric:tabular-nums;white-space:nowrap}
.darkband .v.sm{font-size:1.02rem;white-space:normal}
.rv{opacity:0;transform:translateY(16px);
 transition:opacity .6s cubic-bezier(.2,.7,.3,1),transform .6s cubic-bezier(.2,.7,.3,1)}
.rv.in{opacity:1;transform:none}
@media (prefers-reduced-motion:reduce){.rv{opacity:1;transform:none;transition:none}}
.bigfoot{background:var(--navy);color:#aab3cd;font-size:.88rem}
.bigfoot h3{color:#fff;font-size:.8rem;font-weight:800;letter-spacing:.06em;
 text-transform:uppercase;margin:18px 0 8px}
.bigfoot a{display:block;color:#cdd5ea;padding:4px 0;font-weight:600}
.bigfoot a:hover{color:#fff}
.bigfoot .cols{display:grid;grid-template-columns:1fr 1fr;gap:0 18px}
.bigfoot .legal{color:#7d88a6;font-size:.78rem;line-height:1.6;margin-top:18px;
 border-top:1px solid rgba(255,255,255,.10);padding-top:16px}
.fabs{position:fixed;right:14px;bottom:max(14px,env(safe-area-inset-bottom,14px));
 display:flex;flex-direction:column;gap:10px;z-index:60;align-items:flex-end}
.fab{display:inline-flex;align-items:center;gap:8px;border-radius:999px;
 padding:12px 18px;font-weight:800;font-size:.92rem;color:#fff;
 box-shadow:0 10px 26px rgba(14,19,48,.28)}
.fab:hover{filter:brightness(1.06);color:#fff}
.fab.wa{background:#25d366}
.fab.tg{background:#229ed9}
.fab.em{background:var(--navy)}
.emailpill{display:inline-flex;align-items:center;gap:8px;margin-top:10px;
 background:var(--accent-soft);color:var(--accent-dark);border:1.5px solid var(--accent);
 border-radius:999px;padding:11px 18px;font-weight:800;font-size:.95rem}
.emailpill:hover{background:var(--accent);color:var(--accent-ink)}
.cardpick{display:flex;flex-direction:column;gap:8px}
.cardpick label{margin:0;border:1.5px solid var(--border);border-radius:16px;
 padding:13px 15px;font-weight:800;color:var(--text);cursor:pointer;background:var(--surface);
 font-size:.98rem}
.cardpick input{display:none}
.cardpick label:has(input:checked){border-color:var(--accent);background:var(--accent-soft);
 box-shadow:0 0 0 3px var(--accent-soft)}
@media (max-width:380px){
 h1{font-size:1.7rem}
 .swapbox input,.swapbox .out{font-size:1.3rem}
 .darkband .v{font-size:1.3rem}
 .marq .lg{font-size:1.15rem}
}
@media (min-width:960px){
 .wrap.wide{max-width:1040px}
 .wide h1{font-size:2.7rem}
 .wide .lead{font-size:1.08rem;max-width:640px}
 .wide .steps3{display:grid;grid-template-columns:repeat(3,1fr);gap:6px 26px}
 .wide .steps3 .step{flex-direction:column;gap:10px}
 .wide .grid2{display:grid;grid-template-columns:1fr 1fr;gap:0 34px;align-items:start}
 /* sections stack full-width on desktop (like mobile). A 2-col grid here put a
    short heading next to the tall rates card and left a huge void, and the
    full-bleed marquee's 50vw math breaks inside a narrow grid column. */
 .wide .cols2{display:block}
 .wide .darkband{grid-template-columns:repeat(6,1fr)}
 .wide .darkband .v{font-size:1.6rem}
 .wide .cta-mid{max-width:420px;margin-left:auto;margin-right:auto}
 .bigfoot .cols{grid-template-columns:1fr 1fr 1fr}
}
.bigfoot{margin:44px calc(50% - 50vw) calc(-120px - env(safe-area-inset-bottom,0px));
 border-radius:0;
 padding:34px max(22px,calc(50vw - 500px)) calc(120px + env(safe-area-inset-bottom,0px))}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
"""


_TAIL = """<div class=bigfoot>
<div class=cols>
<div><h3>Trade</h3>
<a href="/sell">Sell USDT</a><a href="/signup">Sign up / Sign in</a>
<a href="/my">My orders</a>
<a href="/#rates">Live rates</a><a href="/#faq">FAQ</a>
<a href="/learn">Guides: USDT to INR</a>
<a href="/support">Create a ticket</a>
<a href="/guarantee">Clean-funds guarantee</a>
<a href="/about">About &amp; contact</a></div>
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
     e.target.querySelectorAll('[data-cu]').forEach(cu);}})},
   {threshold:.05,rootMargin:'0px 0px 300px 0px'});
 els.forEach(function(el){el.classList.add('rv');io.observe(el)});
 // safety net: a money site must never look blank — if the observer hasn't
 // revealed something shortly after load (fast scroll, odd browser, etc.),
 // reveal it anyway so no section is ever stuck invisible.
 setTimeout(function(){els.forEach(function(el){if(!el.classList.contains('in')){
   el.classList.add('in');el.querySelectorAll('[data-cu]').forEach(cu);}})},1800);
 function cu(el){
   if(el.dataset.done)return;el.dataset.done=1;
   var n=parseFloat(el.dataset.cu),pre=el.dataset.pre||'',suf=el.dataset.suf||'',
       dec=parseInt(el.dataset.dec||'0'),t0=null;
   if(!isFinite(n))return;
   function fr(t){if(!t0)t0=t;var p=Math.min(1,(t-t0)/2400);p=1-Math.pow(1-p,3);
     var v=n*p;
     el.textContent=pre+v.toLocaleString('en-IN',
       {minimumFractionDigits:dec,maximumFractionDigits:dec})+suf;
     if(p<1)requestAnimationFrame(fr);}
   requestAnimationFrame(fr);}
})();
// close the ☰ dropdown when tapping anywhere outside it
document.addEventListener('click',function(e){
 var m=document.getElementById('navmenu'),b=document.getElementById('menubtn');
 if(m&&m.classList.contains('open')&&!m.contains(e.target)&&e.target!==b){
   m.classList.remove('open');b.setAttribute('aria-expanded',false);}});
</script>
"""


def _page(title: str, body: str, desc: str = "", wide: bool = False,
          path: str = "", noindex: bool = False, acct: str = "") -> web.Response:
    desc = desc or "Sell USDT for INR — instant bank payout, on-chain verified."
    head_extra = ""
    if noindex:
        head_extra += "<meta name=robots content='noindex,nofollow'>"
    if settings.site_url and path and not noindex:
        url = settings.site_url.rstrip("/") + path
        head_extra += (f"<link rel=canonical href='{_esc(url)}'>"
                       f"<meta property=og:url content='{_esc(url)}'>")
    head_extra += (f"<meta property=og:title content='{_esc(title)}'>"
                   f"<meta property=og:description content='{_esc(desc)}'>"
                   "<meta property=og:type content=website>"
                   "<meta property=og:site_name content='P2P Desk'>"
                   "<meta name=twitter:card content=summary>")
    if not noindex:
        head_extra += _tracking_head()      # marketing pixels on public pages
    if acct:
        label = acct if len(acct) <= 18 else acct[:16] + "…"
        acct_link = (f"<a class='nav me' href='/my' title='{_esc(acct)}'>"
                     f"{_esc(label)}</a>")
    else:
        acct_link = "<a class='nav me' href='/signup'>Sign up</a>"
    _NAVLINKS = (
        '<a href="/">Home</a><a href="/#rates">Rates</a>'
        '<a href="/guarantee">Guarantee</a><a href="/learn">Learn</a>'
        '<a href="/my">My orders</a><a href="/banks">My banks</a>'
        '<a href="/support">Support</a>')
    doc = f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,maximum-scale=1,viewport-fit=cover">
<meta name=description content="{_esc(desc)}">{_FAVICON}{head_extra}
<title>{_esc(title)}</title><style>{_STYLE}{_TRUST_CSS}{_FOOTER_CSS}</style></head><body>
<div class="wrap{' wide' if wide else ''}">
<div class=topbar><a href="/" class=brand><span class=dot></span>P2P Desk</a>
<nav class=topnav>{_NAVLINKS}</nav>
<span class=sp></span>
{acct_link}
<a class="nav hot" href="/sell">Sell USDT</a>
<button class=menubtn id=menubtn aria-label="Open menu" aria-expanded=false
 onclick="var m=document.getElementById('navmenu'),o=m.classList.toggle('open');
 this.setAttribute('aria-expanded',o)">☰</button>
<nav class=navmenu id=navmenu>{_NAVLINKS}</nav></div>
{body}
{_footer()}
{_TAIL}
</body></html>"""
    return web.Response(text=doc, content_type="text/html", headers={
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        # see _sec_headers: keeps token paths out of cross-origin referers while
        # letting Google Identity Services validate the origin
        "Referrer-Policy": "strict-origin-when-cross-origin",
        # never let a proxy cache a per-account page and serve it to someone else
        "Cache-Control": "private, no-store",
    })


# Support email is cached in-process so the floating button (rendered from the
# sync _fabs_html, called on every page) can show it without each handler having
# to load it. Refreshed at site start and whenever the panel saves settings.
_SUPPORT_CACHE = {"email": "", "tg": "", "whatsapp": ""}


async def load_support_cache() -> None:
    async with Session() as s:
        _SUPPORT_CACHE["email"] = await get_support_email(s)
        sup = await get_support(s) or ""
        _SUPPORT_CACHE["tg"] = next(
            (h.lstrip("@") for h in sup.split() if h.startswith("@")), "")
        _SUPPORT_CACHE["whatsapp"] = "".join(
            ch for ch in (await get_whatsapp(s) or "") if ch.isdigit())


def _footer() -> str:
    """Site-wide footer built from the contact channels we actually have
    (Telegram support, email, website) + legal links and rights. Fed by the
    support cache so it's editable from the panel with no redeploy."""
    year = datetime.now(timezone.utc).year
    tg = _SUPPORT_CACHE.get("tg", "")
    email = _SUPPORT_CACHE.get("email", "")
    wa = _SUPPORT_CACHE.get("whatsapp", "")
    site = (settings.site_url or "").rstrip("/")
    contact = []
    if tg:
        contact.append(f"<a href='https://t.me/{_esc(tg)}' target=_blank "
                       "rel=noopener>Telegram support</a>")
    if wa:
        contact.append(f"<a href='https://wa.me/{_esc(wa)}' target=_blank "
                       "rel=noopener>WhatsApp</a>")
    if email:
        contact.append(f"<a href='mailto:{_esc(email)}'>{_esc(email)}</a>")
    contact_col = ("<br>".join(contact)
                   or "<span class=fmut>Contact us in the app</span>")
    site_line = (f"<br><a href='{_esc(site)}'>{_esc(site.split('://')[-1])}</a>"
                 if site else "")
    return f"""<footer class=sitefoot><div class=footgrid>
<div><div class=footbrand><span class=footmark>&#8377;</span>India<span
 class=g>Xchange</span></div>
<p class=fmut>USDT&nbsp;&rarr;&nbsp;INR trading desk. Instant bank payout,
on-chain verified, 100% clean funds. Full INR paid &mdash; no 1% TDS.</p></div>
<div><h4>Desk</h4><a href="/">Home</a><a href="/#rates">Live rates</a>
<a href="/guarantee">Guarantee</a><a href="/learn">Learn</a>
<a href="/my">My orders</a></div>
<div><h4>Legal</h4><a href="/legal/terms">Terms of Use</a>
<a href="/legal/privacy">Privacy &amp; Cookies</a>
<a href="/legal/aml">AML / Clean-Funds Policy</a>
<a href="/support">Support &amp; complaints</a></div>
<div><h4>Contact</h4>{contact_col}{site_line}</div>
</div>
<div class=footbar>
<span>&copy; {year} {_BRAND_NAME_WEB}. All rights reserved.</span>
<span class=fmut>Third-party wallet &amp; exchange names/logos are trademarks of
their respective owners, shown for compatibility only &mdash; no affiliation.</span>
</div></footer>"""


def _fabs_html(support: str, whatsapp: str, email: str = "") -> str:
    """Floating email / WhatsApp / Telegram support buttons, bottom-right on
    every page. Rendered only for the channels that are actually configured."""
    fabs = ""
    email = (email or _SUPPORT_CACHE["email"]).strip()
    if email:
        fabs += (f"<a class='fab em' href='mailto:{_esc(email)}'>✉️ Email</a>")
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
    """A neutral 'message us on Telegram' link — the raw @handle is never shown
    in page copy (it lives only in the href and the floating support button,
    both fed by the panel's editable support setting)."""
    first = next((h for h in support.split() if h.startswith("@")), "")
    if first:
        return (f"<a href='https://t.me/{_esc(first.lstrip('@'))}' target=_blank "
                "rel=noopener>message us on Telegram</a>")
    return "contact support"


def _email_html(email: str, prefix: str = " · ") -> str:
    email = (email or "").strip()
    if not email:
        return ""
    return (f"{prefix}<a href='mailto:{_esc(email)}'>{_esc(email)}</a>")


def _email_pill(email: str) -> str:
    """A prominent, highlighted email button (support page + support card)."""
    email = (email or "").strip()
    if not email:
        return ""
    return (f"<a class=emailpill href='mailto:{_esc(email)}'>✉️ Email us — "
            f"{_esc(email)}</a>")


# ── brand SVG graphics (self-drawn; no external images, CSP-safe) ─────────────
# Clean vector guides in the site palette (navy #0e1330 / green #00c26f). Each
# scales fluidly (viewBox + width:100%) and carries a role/label for a11y.

_TRUST_POINTS = [
    ("Full INR paid", "no 1% TDS deducted"),
    ("100% clean, verified funds", "every rupee from legitimate sources"),
    ("No cyber-complaint or freeze risk", "clean funds can't be traced to a scam"),
    ("Private &amp; on-chain verified", "proof shared on every completed deal"),
]


def _trust_strip() -> str:
    """Highlighted reassurance strip shown wherever prices appear — every claim
    is backed by the clean-funds guarantee (nothing here promises tax evasion
    or non-cooperation with authorities; it's the customer-safety that verified
    clean funds actually deliver)."""
    pills = "".join(
        f"<div class=tpill><span class=tcheck>&#10003;</span><div>"
        f"<b>{a}</b><br><span class=tsub>{b}</span></div></div>"
        for a, b in _TRUST_POINTS)
    return (f"<div class=trust>{pills}</div>")


_TRUST_CSS = """
.trust{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:14px 0}
.tpill{display:flex;gap:9px;align-items:flex-start;background:var(--accent-soft,#e1f9ee);
 border:1px solid #bdead2;border-radius:12px;padding:11px 13px}
.tpill b{color:#0e1330;font-size:.92rem;line-height:1.25}
.tcheck{flex:none;width:20px;height:20px;border-radius:50%;background:#00c26f;color:#062b1a;
 font-weight:800;font-size:12px;display:flex;align-items:center;justify-content:center;margin-top:1px}
.tsub{color:#3c6b53;font-size:.78rem;line-height:1.3}
@media(max-width:760px){.trust{grid-template-columns:1fr 1fr}}
@media(max-width:420px){.trust{grid-template-columns:1fr}}
"""

_FOOTER_CSS = """
.sitefoot{background:#0e1330;color:#c9d0e3;margin-top:40px;padding:34px 20px 22px}
.footgrid{max-width:1080px;margin:0 auto;display:grid;
 grid-template-columns:1.6fr 1fr 1fr 1.2fr;gap:26px}
.sitefoot h4{color:#fff;font-size:.82rem;letter-spacing:.05em;text-transform:uppercase;
 margin:0 0 10px}
.sitefoot a{display:block;color:#c9d0e3;text-decoration:none;font-size:.9rem;
 padding:3px 0}
.sitefoot a:hover{color:#00c26f}
.footbrand{font-weight:800;font-size:1.15rem;color:#fff;margin-bottom:8px}
.footbrand .g{color:#00c26f}
.footmark{display:inline-block;width:26px;height:26px;border:2px solid #00c26f;
 border-radius:7px;text-align:center;line-height:24px;color:#00c26f;font-weight:800;
 margin-right:7px;vertical-align:middle}
.fmut{color:#8b95b8;font-size:.84rem;line-height:1.55}
.footbar{max-width:1080px;margin:24px auto 0;padding-top:16px;
 border-top:1px solid #26304f;display:flex;flex-wrap:wrap;gap:10px 24px;
 justify-content:space-between;font-size:.76rem;color:#8b95b8;line-height:1.5}
@media(max-width:760px){.footgrid{grid-template-columns:1fr 1fr}}
@media(max-width:440px){.footgrid{grid-template-columns:1fr}}
"""


def _figure(svg: str, caption: str = "") -> str:
    cap = (f"<figcaption style='color:#5a657d;font-size:.85rem;margin-top:8px;"
           f"text-align:center'>{caption}</figcaption>" if caption else "")
    return (f"<figure style='margin:0 0 8px'>{svg}{cap}</figure>")


# 4-step sell flow — choose → send USDT → verified on-chain → paid in INR
_SVG_FLOW = """<svg viewBox="0 0 920 210" width="100%" style="height:auto;max-width:920px;display:block;margin:0 auto" role="img" aria-label="How selling works in four steps">
<line x1="115" y1="72" x2="805" y2="72" stroke="#e6eaf1" stroke-width="4" stroke-dasharray="1 12" stroke-linecap="round"/>
<g font-family="Arial,Helvetica,sans-serif">
<g transform="translate(115,72)"><circle r="40" fill="#ffffff" stroke="#0e1330" stroke-width="2.5"/><rect x="-16" y="-18" width="32" height="36" rx="4" fill="none" stroke="#0e1330" stroke-width="2.5"/><line x1="-9" y1="-8" x2="9" y2="-8" stroke="#00c26f" stroke-width="2.5" stroke-linecap="round"/><line x1="-9" y1="0" x2="9" y2="0" stroke="#00c26f" stroke-width="2.5" stroke-linecap="round"/><line x1="-9" y1="8" x2="2" y2="8" stroke="#00c26f" stroke-width="2.5" stroke-linecap="round"/><circle cx="30" cy="-30" r="13" fill="#00c26f"/><text x="30" y="-25" font-size="15" font-weight="800" fill="#062b1a" text-anchor="middle">1</text></g>
<g transform="translate(345,72)"><circle r="40" fill="#ffffff" stroke="#0e1330" stroke-width="2.5"/><circle r="18" fill="none" stroke="#00c26f" stroke-width="2.5"/><text y="6" font-size="18" font-weight="800" fill="#0e1330" text-anchor="middle">&#8366;</text><circle cx="30" cy="-30" r="13" fill="#00c26f"/><text x="30" y="-25" font-size="15" font-weight="800" fill="#062b1a" text-anchor="middle">2</text></g>
<g transform="translate(575,72)"><circle r="40" fill="#ffffff" stroke="#0e1330" stroke-width="2.5"/><path d="M-14 0 l9 10 l19 -20" fill="none" stroke="#00c26f" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/><circle r="24" fill="none" stroke="#0e1330" stroke-width="2" stroke-dasharray="3 4"/><circle cx="30" cy="-30" r="13" fill="#00c26f"/><text x="30" y="-25" font-size="15" font-weight="800" fill="#062b1a" text-anchor="middle">3</text></g>
<g transform="translate(805,72)"><circle r="40" fill="#0e1330"/><rect x="-19" y="-13" width="38" height="26" rx="4" fill="none" stroke="#00c26f" stroke-width="2.5"/><text y="6" font-size="17" font-weight="800" fill="#ffffff" text-anchor="middle">&#8377;</text><circle cx="30" cy="-30" r="13" fill="#00c26f"/><text x="30" y="-25" font-size="15" font-weight="800" fill="#062b1a" text-anchor="middle">4</text></g>
<g font-size="15" font-weight="700" fill="#0e1330" text-anchor="middle"><text x="115" y="140">Choose &amp; enter bank</text><text x="345" y="140">Send exact USDT</text><text x="575" y="140">Verified on-chain</text><text x="805" y="140">Paid in INR</text></g>
<g font-size="12.5" fill="#5a657d" text-anchor="middle"><text x="115" y="162">method, amount, IFSC</text><text x="345" y="162">to your address</text><text x="575" y="162">in seconds, no screenshots</text><text x="805" y="162">UPI / IMPS to any bank</text></g>
</g></svg>"""

# Pricing — USDT coin → 1:1 locked rate → INR note
_SVG_RATE = """<svg viewBox="0 0 460 150" width="100%" style="height:auto;max-width:460px;display:block;margin:0 auto" role="img" aria-label="The rate you see is the rate you are paid">
<g font-family="Arial,Helvetica,sans-serif">
<circle cx="70" cy="70" r="42" fill="#e1f9ee" stroke="#00c26f" stroke-width="2.5"/><text x="70" y="82" font-size="38" font-weight="800" fill="#00a85f" text-anchor="middle">&#8366;</text><text x="70" y="128" font-size="13" font-weight="700" fill="#0e1330" text-anchor="middle">1 USDT</text>
<g transform="translate(230,70)"><line x1="-58" y1="0" x2="52" y2="0" stroke="#0e1330" stroke-width="3" stroke-linecap="round"/><polygon points="52,-8 68,0 52,8" fill="#0e1330"/><rect x="-46" y="-26" width="92" height="26" rx="13" fill="#0e1330"/><text x="0" y="-7" font-size="14" font-weight="800" fill="#ffffff" text-anchor="middle">LIVE RATE</text><text x="0" y="30" font-size="12.5" fill="#5a657d" text-anchor="middle">locked when you order</text></g>
<rect x="330" y="34" width="96" height="64" rx="10" fill="#0e1330"/><text x="378" y="80" font-size="34" font-weight="800" fill="#ffffff" text-anchor="middle">&#8377;</text><text x="378" y="128" font-size="13" font-weight="700" fill="#0e1330" text-anchor="middle">to your bank</text>
</g></svg>"""

# AML — shield with a magnifier over a checkmark: clean-funds screening
_SVG_AML = """<svg viewBox="0 0 200 200" width="150" style="height:auto;max-width:170px;display:block;margin:0 auto" role="img" aria-label="Every deposit and payout is screened for clean funds">
<path d="M100 18 L168 44 V96 C168 140 138 172 100 186 C62 172 32 140 32 96 V44 Z" fill="#e1f9ee" stroke="#00c26f" stroke-width="4"/>
<path d="M100 30 L156 51 V96 C156 133 131 160 100 172 Z" fill="#00c26f" opacity="0.12"/>
<circle cx="90" cy="88" r="30" fill="#ffffff" stroke="#0e1330" stroke-width="5"/>
<path d="M78 88 l9 10 l18 -20" fill="none" stroke="#00c26f" stroke-width="6" stroke-linecap="round" stroke-linejoin="round"/>
<line x1="111" y1="109" x2="132" y2="130" stroke="#0e1330" stroke-width="8" stroke-linecap="round"/>
</svg>"""

# Guarantee — shield with ₹ and a chain link: clean funds, on-chain verified
_SVG_GUARANTEE = """<svg viewBox="0 0 200 200" width="150" style="height:auto;max-width:170px;display:block;margin:0 auto" role="img" aria-label="100 percent clean funds guarantee, verified on-chain">
<path d="M100 16 L170 43 V97 C170 142 139 175 100 189 C61 175 30 142 30 97 V43 Z" fill="#0e1330"/>
<path d="M100 30 L156 52 V97 C156 134 131 161 100 173 C69 161 44 134 44 97 V52 Z" fill="none" stroke="#00c26f" stroke-width="3" stroke-dasharray="4 5"/>
<text x="100" y="112" font-family="Arial,Helvetica,sans-serif" font-size="66" font-weight="800" fill="#ffffff" text-anchor="middle">&#8377;</text>
<g transform="translate(100,150)"><rect x="-26" y="-9" width="24" height="18" rx="9" fill="none" stroke="#00c26f" stroke-width="4"/><rect x="2" y="-9" width="24" height="18" rx="9" fill="none" stroke="#00c26f" stroke-width="4"/></g>
</svg>"""


# ── landing ───────────────────────────────────────────────────────────────────

async def home(request: web.Request):
    async with Session() as s:
        rates = await get_rates(s)
        is_open, _ = await desk_state(s)
        support = await get_support(s)
        whatsapp = await get_whatsapp(s)
        support_email = await get_support_email(s)
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
        f"<div class=rt><div class=nm>{_esc(SERVICES.get(k, k))}</div>"
        f"<div class=pr>₹{v:g}<span style='font-size:.8rem'>/$</span></div>"
        f"<div class=lm>{limits[k][0]:g}$ – {limits[k][1]:g}$ per order</div></div>"
        for k, v in rates.items())
    open_badge = ("<span class='badge ok'>● Desk open now</span>" if is_open
                  else "<span class='badge danger'>● Desk closed — check back soon</span>")
    cta = ("<a class='btn cta-mid' href='/sell'>Sell USDT now</a>" if is_open
           else "<button class='btn cta-mid' disabled style='opacity:.6'>Desk closed</button>")
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
<div class="v sm">{_esc(settings.eta_text)}</div></div>
<div class=cell><div class=k>Verification</div>
<div class="v sm">On-chain</div></div>
</div>"""
    nets = "TRC20 (TRON) and BEP20 (BSC)" if two_chains else "TRC20 (TRON)"
    body = f"""
<h1>Sell USDT.<br><span class=g>Get INR in your bank.</span></h1>
<p class="muted lead">Send USDT, we verify it <b>on-chain automatically</b>, and our
admins pay your bank — UPI, IMPS, CDM or cheque. The same desk thousands trade on
Telegram, now on the web.</p>
<div class=hero-badges>{open_badge}
<span class=badge>100% clean funds</span>
<span class=badge>Auto-verified deposits</span>
<span class=badge>Proof on every deal</span></div>
{cta}
{stats}
<div class=cols2>
<div class=card id=rates><h2 style="margin-top:0">Live rates <span class=livewrap><span class=livedot></span>LIVE<span class=livebars><i></i><i></i><i></i></span></span></h2>
<div class=rategrid>{rows or "<span class=muted>No rates live right now.</span>"}</div>
<p class='muted small' style="margin:10px 0 0">Rates are live — the rate you see when you
order is the rate you're paid at. Networks accepted: <b>{nets}</b>.</p>
{_figure(_SVG_RATE, "The live rate is locked the moment you order — you're paid at exactly that rate.")}</div>
{_trust_strip()}
<h2>How it works</h2>
<div class=card>{_figure(_SVG_FLOW)}<div class=steps3>
<div class=step><div class=n>1</div><div><b>Choose method &amp; amount</b><br>
<span class='muted small'>Pick your payout method, enter the USDT amount (each method
shows its min/max), and your bank details for the INR payout.</span></div></div>
<div class=step><div class=n>2</div><div><b>Send the exact USDT amount</b><br>
<span class='muted small'>We show a deposit address + QR. Send the exact amount — our
scanner verifies it on-chain in seconds, no screenshots needed.</span></div></div>
<div class=step><div class=n>3</div><div><b>Get paid in INR</b><br>
<span class='muted small'>Verified deposits enter the payout queue and our admins pay
your bank directly — typically {_esc(settings.eta_text)}. Proof shared on every deal.</span></div></div></div></div>
<div class=card><h2 style="margin-top:0">Sell from any wallet or exchange</h2>
<p class='muted small' style="margin:0 0 4px">Withdraw USDT from wherever you hold it and
send it to your order address — it works the same from every app:</p>
<div class=marq><div class=track><span class=lg style='color:#0e1330'><svg viewBox='0 0 32 32'><polygon points='16,11.8 20.2,16 16,20.2 11.8,16' fill='#F0B90B'/><polygon points='16,4.5 19.2,7.7 16,10.9 12.8,7.7' fill='#F0B90B'/><polygon points='16,21.1 19.2,24.3 16,27.5 12.8,24.3' fill='#F0B90B'/><polygon points='7.7,12.8 10.9,16 7.7,19.2 4.5,16' fill='#F0B90B'/><polygon points='24.3,12.8 27.5,16 24.3,19.2 21.1,16' fill='#F0B90B'/></svg>Binance</span><span class=lg style='color:#3375BB'><svg viewBox='0 0 32 32'><path d='M16 4 L6 7.6 V15 C6 21.4 10.6 25.9 16 28.4 Z' fill='#5aa0e6'/><path d='M16 4 L26 7.6 V15 C26 21.4 21.4 25.9 16 28.4 Z' fill='#3375BB'/></svg>Trust Wallet</span><span class=lg style='color:#111111'><svg viewBox='0 0 32 32'><rect x=4 y=4 width=7.4 height=7.4 fill='#111'/><rect x=20.6 y=4 width=7.4 height=7.4 fill='#111'/><rect x=12.3 y=12.3 width=7.4 height=7.4 fill='#111'/><rect x=4 y=20.6 width=7.4 height=7.4 fill='#111'/><rect x=20.6 y=20.6 width=7.4 height=7.4 fill='#111'/></svg>OKX</span><span class=lg style='color:#B87500'><svg viewBox='0 0 32 32'><rect x=3 y=3 width=26 height=26 rx=8 fill='#F7A600'/><text x=16 y=22 font-size=15 font-weight=800 font-family='Arial,Helvetica,sans-serif' text-anchor=middle fill='#fff'>B</text></svg>Bybit</span><span class=lg style='color:#1f9e83'><svg viewBox='0 0 32 32'><path d='M10 4 H22 L28 16 L22 28 H10 L4 16 Z' fill='#23AF91'/><rect x=11 y=9.5 width=2.6 height=13 fill='#fff'/><path d='M13.6 16 L19.3 10.3 L21.9 12.9 L18.8 16 L21.9 19.1 L19.3 21.7 Z' fill='#fff'/></svg>KuCoin</span><span class=lg style='color:#2980FE'><svg viewBox='0 0 32 32'><rect x=3 y=3 width=26 height=26 rx=7 fill='#2980FE'/><text x=16 y=21 font-size=11.5 font-weight=800 font-family='Arial,Helvetica,sans-serif' text-anchor=middle fill='#fff'>TP</text></svg>TokenPocket</span><span class=lg style='color:#0e1330'><svg viewBox='0 0 32 32'><polygon points='16,11.8 20.2,16 16,20.2 11.8,16' fill='#F0B90B'/><polygon points='16,4.5 19.2,7.7 16,10.9 12.8,7.7' fill='#F0B90B'/><polygon points='16,21.1 19.2,24.3 16,27.5 12.8,24.3' fill='#F0B90B'/><polygon points='7.7,12.8 10.9,16 7.7,19.2 4.5,16' fill='#F0B90B'/><polygon points='24.3,12.8 27.5,16 24.3,19.2 21.1,16' fill='#F0B90B'/></svg>Binance</span><span class=lg style='color:#3375BB'><svg viewBox='0 0 32 32'><path d='M16 4 L6 7.6 V15 C6 21.4 10.6 25.9 16 28.4 Z' fill='#5aa0e6'/><path d='M16 4 L26 7.6 V15 C26 21.4 21.4 25.9 16 28.4 Z' fill='#3375BB'/></svg>Trust Wallet</span><span class=lg style='color:#111111'><svg viewBox='0 0 32 32'><rect x=4 y=4 width=7.4 height=7.4 fill='#111'/><rect x=20.6 y=4 width=7.4 height=7.4 fill='#111'/><rect x=12.3 y=12.3 width=7.4 height=7.4 fill='#111'/><rect x=4 y=20.6 width=7.4 height=7.4 fill='#111'/><rect x=20.6 y=20.6 width=7.4 height=7.4 fill='#111'/></svg>OKX</span><span class=lg style='color:#B87500'><svg viewBox='0 0 32 32'><rect x=3 y=3 width=26 height=26 rx=8 fill='#F7A600'/><text x=16 y=22 font-size=15 font-weight=800 font-family='Arial,Helvetica,sans-serif' text-anchor=middle fill='#fff'>B</text></svg>Bybit</span><span class=lg style='color:#1f9e83'><svg viewBox='0 0 32 32'><path d='M10 4 H22 L28 16 L22 28 H10 L4 16 Z' fill='#23AF91'/><rect x=11 y=9.5 width=2.6 height=13 fill='#fff'/><path d='M13.6 16 L19.3 10.3 L21.9 12.9 L18.8 16 L21.9 19.1 L19.3 21.7 Z' fill='#fff'/></svg>KuCoin</span><span class=lg style='color:#2980FE'><svg viewBox='0 0 32 32'><rect x=3 y=3 width=26 height=26 rx=7 fill='#2980FE'/><text x=16 y=21 font-size=11.5 font-weight=800 font-family='Arial,Helvetica,sans-serif' text-anchor=middle fill='#fff'>TP</text></svg>TokenPocket</span></div><div class='track rev'><span class=lg style='color:#3067F0'><svg viewBox='0 0 32 32'><polygon points='16,3.5 28.5,16 16,28.5 3.5,16' fill='#3067F0'/><text x=16 y=20.5 font-size=11 font-weight=800 font-family='Arial,Helvetica,sans-serif' text-anchor=middle fill='#fff'>W</text></svg>WazirX</span><span class=lg style='color:#4A24AE'><svg viewBox='0 0 32 32'><rect x=3 y=3 width=26 height=26 rx=8 fill='#4A24AE'/><text x=16 y=22 font-size=15 font-weight=800 font-family='Arial,Helvetica,sans-serif' text-anchor=middle fill='#fff'>C</text></svg>CoinDCX</span><span class=lg style='color:#4A7DFF'><svg viewBox='0 0 32 32'><rect x=3 y=3 width=26 height=26 rx=8 fill='#4A7DFF'/><text x=16 y=22 font-size=15 font-weight=800 font-family='Arial,Helvetica,sans-serif' text-anchor=middle fill='#fff'>S</text></svg>SafePal</span><span class=lg style='color:#0a9c9c'><svg viewBox='0 0 32 32'><rect x=3 y=3 width=26 height=26 rx=8 fill='#00C2C2'/><text x=16 y=22 font-size=15 font-weight=800 font-family='Arial,Helvetica,sans-serif' text-anchor=middle fill='#fff'>B</text></svg>Bitget</span><span class=lg style='color:#1972E2'><svg viewBox='0 0 32 32'><rect x=3 y=3 width=26 height=26 rx=8 fill='#1972E2'/><text x=16 y=22 font-size=15 font-weight=800 font-family='Arial,Helvetica,sans-serif' text-anchor=middle fill='#fff'>M</text></svg>MEXC</span><span class=lg style='color:#2354E6'><svg viewBox='0 0 32 32'><rect x=3 y=3 width=26 height=26 rx=8 fill='#2354E6'/><text x=16 y=22 font-size=15 font-weight=800 font-family='Arial,Helvetica,sans-serif' text-anchor=middle fill='#fff'>G</text></svg>Gate.io</span><span class=lg style='color:#3067F0'><svg viewBox='0 0 32 32'><polygon points='16,3.5 28.5,16 16,28.5 3.5,16' fill='#3067F0'/><text x=16 y=20.5 font-size=11 font-weight=800 font-family='Arial,Helvetica,sans-serif' text-anchor=middle fill='#fff'>W</text></svg>WazirX</span><span class=lg style='color:#4A24AE'><svg viewBox='0 0 32 32'><rect x=3 y=3 width=26 height=26 rx=8 fill='#4A24AE'/><text x=16 y=22 font-size=15 font-weight=800 font-family='Arial,Helvetica,sans-serif' text-anchor=middle fill='#fff'>C</text></svg>CoinDCX</span><span class=lg style='color:#4A7DFF'><svg viewBox='0 0 32 32'><rect x=3 y=3 width=26 height=26 rx=8 fill='#4A7DFF'/><text x=16 y=22 font-size=15 font-weight=800 font-family='Arial,Helvetica,sans-serif' text-anchor=middle fill='#fff'>S</text></svg>SafePal</span><span class=lg style='color:#0a9c9c'><svg viewBox='0 0 32 32'><rect x=3 y=3 width=26 height=26 rx=8 fill='#00C2C2'/><text x=16 y=22 font-size=15 font-weight=800 font-family='Arial,Helvetica,sans-serif' text-anchor=middle fill='#fff'>B</text></svg>Bitget</span><span class=lg style='color:#1972E2'><svg viewBox='0 0 32 32'><rect x=3 y=3 width=26 height=26 rx=8 fill='#1972E2'/><text x=16 y=22 font-size=15 font-weight=800 font-family='Arial,Helvetica,sans-serif' text-anchor=middle fill='#fff'>M</text></svg>MEXC</span><span class=lg style='color:#2354E6'><svg viewBox='0 0 32 32'><rect x=3 y=3 width=26 height=26 rx=8 fill='#2354E6'/><text x=16 y=22 font-size=15 font-weight=800 font-family='Arial,Helvetica,sans-serif' text-anchor=middle fill='#fff'>G</text></svg>Gate.io</span></div></div>
<p class='muted small' style="margin:8px 0 0">…and any other wallet or exchange that can
send USDT on your chosen network. Pick the network on the sell form and match it when
you withdraw.</p>
<p class=trademarks>Logos are trademarks of their respective owners, shown to indicate wallet &amp; network compatibility — not affiliation or endorsement.</p></div>
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
</div>
<div class=card><h2 style="margin-top:0">100% Clean Funds — our guarantee</h2>
<p class='muted small' style="margin:0">Every rupee we pay out comes from verified,
legitimate sources — mutual &amp; stock-market funds, cash deposits, credit-card and
payment-gateway funds. Your account is never at risk of a freeze or hold.
<a href="/guarantee"><b>How the guarantee works →</b></a></p></div>
<h2 id=faq>Frequently asked</h2>
<div class=card><div class=grid2>
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
<details><summary>Do I need an account?</summary><p class='muted small'>Yes — a free
30-second signup (Google or email) before your first order. Your orders, tickets and
saved banks then follow your account on any device, under
<a href="/my">My orders</a>.</p></details>
</div></div>
<div class=card id=support><b>Support</b><br><span class=small>{_support_html(support)}
<span class=muted>— mention your order ID (#ORD…)</span></span>
{_email_pill(support_email)}
<a class="btn ghost" style="margin-top:12px;max-width:340px" href="/support">Create a support ticket</a></div>
{_fabs_html(support, whatsapp)}"""
    faq_ld = ({
        "@context": "https://schema.org", "@type": "FAQPage",
        "mainEntity": [
            {"@type": "Question", "name": q, "acceptedAnswer":
             {"@type": "Answer", "text": a}} for q, a in [
                ("How fast do I get paid when selling USDT for INR?",
                 f"Deposits verify on-chain within seconds of confirming; bank "
                 f"payout typically follows in {settings.eta_text} via UPI or IMPS."),
                ("Which networks can I send USDT on?",
                 "TRC20 (TRON), and BEP20 (BSC) when enabled — pick the network "
                 "on the sell form and match it in your wallet."),
                ("Can I sell USDT directly from Binance or another exchange?",
                 "Yes — place the order, then withdraw the exact USDT amount from "
                 "your exchange to the address shown, on the same network."),
                ("Do I need an account to sell USDT here?",
                 "Yes — a free 30-second signup with Google or email before "
                 "your first order. Your orders and saved banks then follow "
                 "your account on any device."),
            ]]})
    org = {"@context": "https://schema.org", "@type": "Organization",
           "name": settings.biz_name or "P2P Desk"}
    if settings.site_url:
        org["url"] = settings.site_url
    if settings.biz_email:
        org["email"] = settings.biz_email
    body += _ld(faq_ld) + _ld(org)
    acct = await _account_from_request(request)
    return _page("Sell USDT for INR at Live Rates — Instant Bank Payout | P2P Desk",
                 body,
                 "Sell USDT for INR at live rates. On-chain verified deposits, "
                 "instant bank payout via UPI/IMPS/CDM across India. Free "
                 "signup. 100% clean funds guarantee.",
                 wide=True, path="/", acct=acct.email if acct else "")


# ── signup / sign-in pages ───────────────────────────────────────────────────

def _uq(s: str) -> str:
    return quote(s, safe="/")


def _stock_pick(sel: str = "") -> str:
    tiles = "".join(
        f"<label><input type=radio name=stock value='{t}' required "
        f"{'checked' if t == sel else ''}><span>{t}<br>"
        f"<span class='muted small' style='font-weight:600'>USDT/day</span></span></label>"
        for t in _STOCK_TIERS)
    return f"<div class=stockpick>{tiles}</div>"


def _google_button(csrf: str, nxt: str) -> str:
    """The official Sign-in-with-Google button (Google Identity Services).
    The JS callback drops the returned ID token into a hidden form and posts
    it to /auth/google, where it is verified server-side against our client
    id. Rendered only when a client id is configured."""
    if not settings.google_client_id:
        return ""
    cid = _esc(settings.google_client_id)
    return (
        f"<form id=gform method=post action=/auth/google style=display:none>"
        f"<input type=hidden name=csrf value='{csrf}'>"
        f"<input type=hidden name=next value='{_esc(nxt)}'>"
        f"<input type=hidden name=credential></form>"
        f"<div id=g_id_onload data-client_id=\"{cid}\" "
        "data-callback=\"onGoogleCred\" data-auto_prompt=\"false\"></div>"
        "<div class=gwrap><div class=\"g_id_signin\" data-type=standard "
        "data-shape=pill data-size=large data-text=continue_with "
        "data-logo_alignment=left></div></div>"
        "<script>function onGoogleCred(r){"
        "var f=document.getElementById('gform');"
        "f.credential.value=r.credential;f.submit();}</script>"
        "<script src=\"https://accounts.google.com/gsi/client\" async defer>"
        "</script><div class=orline>OR</div>")


def _auth_body(csrf: str, nxt: str, mode: str, error: str, p: dict) -> str:
    """Tabs + Google button + the signup or sign-in form (no heading)."""
    err = f"<p class=err>{_esc(error)}</p>" if error else ""
    tabs = (f"<div class=authtabs>"
            f"<a href='/signup?next={_uq(nxt)}'"
            f"{' class=on' if mode == 'up' else ''}>Create account</a>"
            f"<a href='/signup?mode=in&amp;next={_uq(nxt)}'"
            f"{' class=on' if mode == 'in' else ''}>Sign in</a></div>")
    g = _google_button(csrf, nxt)
    if mode == "in":
        form = f"""<form method=post action=/signin><div class=card>
<input type=hidden name=csrf value='{csrf}'>
<input type=hidden name=next value='{_esc(nxt)}'>
<label>Email</label>
<input name=email type=email autocomplete=email required
 value="{_esc(p.get('email', ''))}">
<label>Password</label>
<input name=password type=password autocomplete=current-password required>
<div style=margin-top:16px><button class=btn>Sign in</button></div>
<p class='muted small' style='margin:12px 0 0'>
<a href='/reset?next={_uq(nxt)}'>Forgot your password?</a> Reset it with a code
sent to your email.</p>
</div></form>"""
    else:
        # Inline email verification: the address is OTP-verified IN the form,
        # BEFORE the account exists — the everify hidden field carries a signed
        # proof the server checks again in signup_post (JS gating alone would
        # be bypassable). Fixed +91 prefix: customers type just the 10 digits.
        form = f"""<form method=post action=/signup id=suform><div class=card>
<input type=hidden name=csrf value='{csrf}'>
<input type=hidden name=next value='{_esc(nxt)}'>
<input type=hidden name=everify id=everify value=''>
<label>Your name</label>
<input name=name autocomplete=name required maxlength=120
 value="{_esc(p.get('name', ''))}">
<label>Email <span class=reqtag>verify to continue</span></label>
<div class=vrow>
<input name=email id=suemail type=email autocomplete=email required
 value="{_esc(p.get('email', ''))}">
<button type=button class=vbtnmain id=vbtn>Verify now</button>
</div>
<div id=vcodebox class=codebox style='display:none;margin-top:10px'>
<div class=vrow>
<input id=vcode inputmode=numeric maxlength=6 placeholder='6-digit code'
 autocomplete=one-time-code style='letter-spacing:4px'>
<button type=button class=vbtnmain id=vok>Confirm</button>
</div>
<p class='muted small' id=vhint style='margin:6px 0 0'>We emailed a code to
your address — enter it here. <a href='#' id=vresend>Resend</a></p>
</div>
<p class='small' id=vstate style='display:none;margin:6px 0 0'></p>
<label>Mobile number</label>
<div class=vrow>
<span class=cc>&#127470;&#127475; +91</span>
<input name=phone inputmode=numeric maxlength=10 pattern='[6-9][0-9]{{9}}'
 placeholder='98765 43210' required value="{_esc(p.get('phone', ''))}"
 title='10-digit Indian mobile number'>
</div>
<label>Password</label>
<input name=password id=supw type=password autocomplete=new-password
 minlength=8 required>
<p class='muted small' style='margin:4px 0 0'>8+ characters including a special
character (e.g. <b>@</b> or <b>#</b>).</p>
<label>Confirm password</label>
<input name=password2 id=supw2 type=password autocomplete=new-password
 minlength=8 required>
<label>How much USDT do you sell per day?</label>
{_stock_pick(p.get('stock', ''))}
<div style=margin-top:16px><button class=btn id=subtn>Create account →</button></div>
<p class='muted small' style='margin:10px 0 0'>By signing up you agree to the
<a href='/legal/terms'>Terms of Use</a> and
<a href='/legal/privacy'>Privacy Policy</a>.</p>
</div></form>
<style>
.vrow{{display:flex;gap:8px;align-items:stretch}}
.vrow input{{flex:1;min-width:0}}
.vbtnmain{{flex:none;background:#00c26f;border:1px solid #00a85f;color:#062b1a;
 font-weight:800;cursor:pointer;font-size:.9rem;padding:0 16px;border-radius:10px;
 white-space:nowrap;box-shadow:0 2px 8px rgba(0,194,111,.35)}}
.vbtnmain:hover{{background:#00a85f}}
.vbtnmain:disabled{{background:var(--surface-2);color:var(--faint);
 border-color:var(--border);box-shadow:none;cursor:default}}
.reqtag{{display:inline-block;background:#fbefdd;color:#b45309;font-size:.7rem;
 font-weight:800;text-transform:uppercase;letter-spacing:.04em;padding:2px 8px;
 border-radius:20px;margin-left:6px;vertical-align:middle}}
.codebox{{background:#e9effe;border:1.5px solid #b9cbfa;border-radius:12px;
 padding:12px 14px}}
.codebox input{{background:#fff}}
.cc{{flex:none;display:flex;align-items:center;padding:0 12px;background:var(--surface-2);
 border:1px solid var(--border);border-radius:10px;font-weight:700;white-space:nowrap}}
.vok{{color:var(--ok);font-weight:700}}
.verr{{color:var(--danger);font-weight:600}}
</style>
<script>
(function(){{
 var csrf='{csrf}',em=document.getElementById('suemail'),
     vb=document.getElementById('vbtn'),box=document.getElementById('vcodebox'),
     code=document.getElementById('vcode'),ok=document.getElementById('vok'),
     res=document.getElementById('vresend'),st=document.getElementById('vstate'),
     ev=document.getElementById('everify'),f=document.getElementById('suform');
 function state(msg,cls){{st.style.display='block';st.className='small '+cls;
   st.textContent=msg}}
 function send(){{
   if(!em.checkValidity()){{em.reportValidity();return}}
   vb.disabled=true;vb.textContent='Sending…';
   fetch('/signup/otp',{{method:'POST',headers:{{'Content-Type':'application/json'}},
     body:JSON.stringify({{csrf:csrf,email:em.value.trim()}})}})
   .then(function(r){{return r.json()}})
   .then(function(d){{
     vb.textContent='Verify now';vb.disabled=false;
     if(!d.ok){{
       if(d.signin){{st.style.display='block';st.className='small verr';
         st.innerHTML=d.error+' <a href="/signup?mode=in&next={_uq(nxt)}"><b>Sign in \\u2192</b></a>';
         return}}
       state(d.error||'Could not send the code.','verr');return}}
     box.style.display='block';st.style.display='none';code.focus();
   }})
   .catch(function(){{vb.textContent='Verify now';vb.disabled=false;
     state('Network error — try again.','verr')}});
 }}
 function check(){{
   ok.disabled=true;
   fetch('/signup/otp/check',{{method:'POST',
     headers:{{'Content-Type':'application/json'}},
     body:JSON.stringify({{csrf:csrf,email:em.value.trim(),code:code.value.trim()}})}})
   .then(function(r){{return r.json()}})
   .then(function(d){{
     ok.disabled=false;
     if(!d.ok){{state(d.error||'Wrong code.','verr');return}}
     ev.value=d.token;box.style.display='none';
     state('\\u2713 '+em.value.trim()+' verified','vok');
     em.readOnly=true;vb.style.display='none';
   }})
   .catch(function(){{ok.disabled=false;state('Network error — try again.','verr')}});
 }}
 if(vb){{vb.addEventListener('click',send)}}
 if(res){{res.addEventListener('click',function(e){{e.preventDefault();send()}})}}
 if(ok){{ok.addEventListener('click',check)}}
 if(code){{code.addEventListener('keydown',function(e){{
   if(e.key==='Enter'){{e.preventDefault();check()}}}})}}
 em.addEventListener('input',function(){{ev.value='';em.readOnly=false;
   vb.style.display='';st.style.display='none'}});
 f.addEventListener('submit',function(e){{
   var p1=document.getElementById('supw'),p2=document.getElementById('supw2');
   if(p1.value!==p2.value){{e.preventDefault();
     state('Passwords do not match.','verr');p2.focus();return}}
   if(!/[^A-Za-z0-9]/.test(p1.value)){{e.preventDefault();
     state('Password needs a special character, e.g. @','verr');p1.focus();return}}
 }});
}})();
</script>"""
    return tabs + err + g + form


async def signup_get(request: web.Request, error: str = "",
                     prefill: dict | None = None, mode: str = ""):
    p = prefill or {}
    mode = mode or ("in" if request.query.get("mode") == "in" else "up")
    nxt = _safe_next(str(p.get("next", "") or request.query.get("next", "")
                         or "/sell"))
    if not error:
        acct = await _account_from_request(request)
        if acct is not None:      # already signed in — nothing to do here
            raise web.HTTPFound(f"/signup/stock?next={_uq(nxt)}"
                                if not acct.stock else nxt)
    uid, is_new = await _ensure_uid(request)
    csrf = await _csrf(f"auth:{uid}")
    head = ("<h1>Welcome <span class=g>back</span></h1>"
            "<p class='muted lead'>Sign in to sell and see your orders and "
            "saved banks on any device.</p>" if mode == "in" else
            "<h1>Create your <span class=g>account</span></h1>"
            "<p class='muted lead'>One quick signup and the desk is yours — "
            "sell USDT, track every order live, and reuse your saved banks "
            "from any device.</p>")
    async with Session() as s:
        support = await get_support(s)
        whatsapp = await get_whatsapp(s)
    body = head + _auth_body(csrf, nxt, mode, error, p) + _fabs_html(support, whatsapp)
    resp = _page(("Sign in — P2P Desk" if mode == "in"
                  else "Create your account — P2P Desk"), body,
                 "Sign up free to sell USDT for INR — instant bank payouts, "
                 "orders tracked live.", noindex=True)
    if is_new:
        _set_uid_cookie(resp, await _sign_uid(uid), _is_https(request))
    return resp


async def signup_post(request: web.Request):
    data = await request.post()
    uid = await _uid_from_cookie(request)
    if uid is None:
        return await signup_get(request, "Please enable cookies and try again.")
    nxt = _safe_next(str(data.get("next", "")))
    p = {k: str(data.get(k, "")).strip()
         for k in ("name", "email", "phone", "stock")}
    p["next"] = nxt
    if not hmac.compare_digest(str(data.get("csrf", "")),
                               await _csrf(f"auth:{uid}")):
        return await signup_get(request, "That form expired — please try again.", p)
    password = str(data.get("password", ""))
    password2 = str(data.get("password2", ""))
    email = p["email"].lower()
    if not _EMAIL_RE.match(email) or len(email) > 190:
        return await signup_get(request, "Please enter a valid email address.", p)
    if not p["name"] or len(p["name"]) > 120:
        return await signup_get(request, "Please enter your name.", p)
    # the form shows a fixed +91 prefix, so a bare 10-digit Indian mobile is
    # the normal case; a full +country-code number still validates for safety
    digits = re.sub(r"[\s\-()]", "", p["phone"])
    if re.fullmatch(r"[6-9]\d{9}", digits):
        p["phone"] = "+91" + digits
    elif not _valid_phone(p["phone"]):
        return await signup_get(request, "Please enter your 10-digit mobile "
                                "number (the +91 is already there).", p)
    pw_err = _valid_password(password)
    if pw_err:
        return await signup_get(request, pw_err, p)
    if password != password2:
        return await signup_get(request, "The two passwords don't match — "
                                "please retype them.", p)
    if p["stock"] not in _STOCK_TIERS:
        return await signup_get(request, "Please pick how much USDT you sell "
                                "per day.", p)
    # email must be OTP-verified BEFORE the account exists (Verify now in the
    # form). The signed everify token is the proof; JS-only gating would be
    # bypassable with a hand-built POST. Skipped only while SMTP is unset
    # (no way to send codes), in which case signups stay open as before.
    from . import sender as _sender
    email_gate = _sender.email_ready(await _sender.email_config())
    if email_gate:
        token = str(data.get("everify", ""))
        if not hmac.compare_digest(token, await _everify_token(uid, email)):
            return await signup_get(
                request, "Please verify your email first — tap the blue "
                "“Verify now” next to the email box and enter the code we "
                "send you.", p)
    ip = _client_ip(request)
    if _bucket_throttled(_signup_times, ip, _SIGNUP_MAX_PER_HOUR, 3600):
        return await signup_get(request, "Too many signups from this "
                                "connection — please try again later.", p)
    _bucket_record(_signup_times, ip, 3600)
    salt = secrets.token_hex(16)
    try:
        async with Session() as s:
            existing = await s.scalar(select(Account)
                                      .where(Account.email == email))
            if existing is not None:
                if existing.pw_hash:
                    return await signup_get(
                        request, "That email is already registered — sign in "
                        "instead.", p, mode="in")
                return await signup_get(
                    request, "That email signed up with Google — use the "
                    "Google button below.", p)
            acct = Account(email=email, name=p["name"],
                           phone=_norm_phone(p["phone"]),
                           provider="email", pw_salt=salt,
                           pw_hash=await _hash_pw(password, salt),
                           stock=p["stock"],
                           email_verified=True)   # OTP proven above (or no SMTP)
            s.add(acct)
            await s.commit()
    except IntegrityError:      # signup race on the same email
        return await signup_get(request, "That email is already registered — "
                                "sign in instead.", p, mode="in")
    resp = web.HTTPFound(nxt)
    await _login_account(request, resp, acct, uid)
    await _notify_signup(request, acct)
    log.info("web signup #%s via email from %s", acct.id, ip)
    return resp


async def signin_post(request: web.Request):
    data = await request.post()
    uid = await _uid_from_cookie(request)
    if uid is None:
        return await signup_get(request, "Please enable cookies and try again.",
                                mode="in")
    nxt = _safe_next(str(data.get("next", "")))
    email = str(data.get("email", "")).strip().lower()
    p = {"email": email, "next": nxt}
    if not hmac.compare_digest(str(data.get("csrf", "")),
                               await _csrf(f"auth:{uid}")):
        return await signup_get(request, "That form expired — please try "
                                "again.", p, mode="in")
    ip = _client_ip(request)
    if _bucket_throttled(_login_times, ip, _LOGIN_MAX_PER_HOUR, 3600):
        return await signup_get(request, "Too many attempts — please wait a "
                                "while.", p, mode="in")
    _bucket_record(_login_times, ip, 3600)
    async with Session() as s:
        acct = await s.scalar(select(Account).where(Account.email == email))
    password = str(data.get("password", ""))
    if acct is not None and not acct.pw_hash:
        # spend the same PBKDF2 as a real check so this branch isn't a faster,
        # tell-tale response, then point them at the right button
        await _hash_pw(password, _DUMMY_SALT)
        return await signup_get(request, "That email signed up with Google — "
                                "use the Google button below.", p, mode="in")
    # always run one PBKDF2 (dummy salt when the email is unknown) so a wrong
    # email and a wrong password take the same time — no account-enumeration
    # timing oracle
    salt = acct.pw_salt if acct else _DUMMY_SALT
    calc = await _hash_pw(password, salt)
    if acct is None or not hmac.compare_digest(acct.pw_hash, calc):
        return await signup_get(request, "Email or password is incorrect.",
                                p, mode="in")
    resp = web.HTTPFound(f"/signup/stock?next={_uq(nxt)}"
                         if not acct.stock else nxt)
    await _login_account(request, resp, acct, uid)
    return resp


async def auth_google(request: web.Request):
    if not settings.google_client_id:
        raise web.HTTPNotFound()
    data = await request.post()
    uid = await _uid_from_cookie(request)
    if uid is None:
        return await signup_get(request, "Please enable cookies and try again.")
    nxt = _safe_next(str(data.get("next", "")))
    if not hmac.compare_digest(str(data.get("csrf", "")),
                               await _csrf(f"auth:{uid}")):
        return await signup_get(request, "That form expired — please try again.")
    ip = _client_ip(request)
    if _bucket_throttled(_login_times, ip, _LOGIN_MAX_PER_HOUR, 3600):
        return await signup_get(request, "Too many attempts — please wait a while.")
    _bucket_record(_login_times, ip, 3600)
    claims = await _google_claims(str(data.get("credential", "")))
    if claims is None:
        return await signup_get(request, "Google sign-in could not be verified "
                                "— please try again.")
    email = str(claims["email"]).lower()
    sub = str(claims["sub"])
    name = str(claims.get("name") or "")[:120]
    try:
        async with Session() as s:
            acct = await s.scalar(select(Account)
                                  .where(Account.google_sub == sub))
            if acct is None:
                acct = await s.scalar(select(Account)
                                      .where(Account.email == email))
                if acct is not None:
                    # Google proved ownership of this email (email_verified);
                    # any pre-existing email+password row for it was created
                    # WITHOUT verification, so it may be an attacker squatting
                    # the victim's address. Take the account over for Google
                    # and DROP the unverified password — otherwise the squatter
                    # keeps password access to the account the real owner now
                    # uses (account pre-hijacking).
                    acct.google_sub = sub
                    acct.provider = "google"
                    acct.pw_hash = ""
                    acct.pw_salt = ""
                    acct.email_verified = True   # Google verified the address
                    acct.sess_ver = (acct.sess_ver or 0) + 1   # kill old sessions
                    if not acct.name:
                        acct.name = name
                else:
                    acct = Account(email=email, name=name, provider="google",
                                   google_sub=sub, email_verified=True)
                    s.add(acct)
                await s.commit()
            elif not acct.email_verified:
                acct.email_verified = True       # repeat Google login backfills
                await s.commit()
    except IntegrityError:      # two first-logins racing — re-read the winner
        async with Session() as s:
            acct = await s.scalar(select(Account)
                                  .where(Account.google_sub == sub))
        if acct is None:
            return await signup_get(request, "Google sign-in could not be "
                                    "verified — please try again.")
    resp = web.HTTPFound(f"/signup/stock?next={_uq(nxt)}"
                         if not acct.stock else nxt)
    await _login_account(request, resp, acct, uid)
    log.info("web google sign-in #%s from %s", acct.id, ip)
    return resp


_signup_otp_times: dict[str, deque] = {}       # per source IP
_signup_otp_email_times: dict[str, deque] = {}  # per TARGET email (churn-proof)
_reset_times: dict[str, deque] = {}


async def _auth_json(request: web.Request):
    """(uid, data) for the signup-page AJAX endpoints, or (None, error_resp)."""
    try:
        data = await request.json()
        assert isinstance(data, dict)
    except Exception:
        return None, web.json_response({"ok": False, "error": "Bad request."})
    uid = await _uid_from_cookie(request)
    if uid is None:
        return None, web.json_response(
            {"ok": False, "error": "Enable cookies and reload the page."})
    if not hmac.compare_digest(str(data.get("csrf", "")),
                               await _csrf(f"auth:{uid}")):
        return None, web.json_response(
            {"ok": False, "error": "This form expired — reload the page."})
    return (uid, data), None


async def signup_otp_post(request: web.Request):
    """Send the inline signup verification code (the blue 'Verify now')."""
    ctx, err = await _auth_json(request)
    if err is not None:
        return err
    uid, data = ctx
    email = str(data.get("email", "")).strip().lower()
    if not _EMAIL_RE.match(email) or len(email) > 190:
        return web.json_response({"ok": False, "error": "Enter a valid email address."})
    # already registered (manual OR Google)? send them to sign-in instead of
    # letting them verify + re-attempt a signup that would fail anyway
    async with Session() as s:
        exists = await s.scalar(select(Account.id).where(Account.email == email))
    if exists is not None:
        return web.json_response({"ok": False, "signin": True,
                                  "error": "This email already has an account — "
                                  "please sign in instead."})
    ip = _client_ip(request)
    # Per-TARGET-email cap first: the anon browser uid is minted fresh on every
    # GET /signup, so a per-uid cap is defeated by cookie churn — but the target
    # address is attack-invariant, so this bounds how many codes any one inbox
    # can receive no matter how many uids/IPs an attacker cycles through.
    if _bucket_throttled(_signup_otp_email_times, email, 4, 3600):
        return web.json_response({"ok": False, "error": "That address was just "
                                  "sent several codes — check your inbox/spam or "
                                  "try again later."})
    if _bucket_throttled(_signup_otp_times, ip, 8, 3600):
        return web.json_response({"ok": False, "error": "Too many codes from this "
                                  "connection — try again later."})
    from . import sender as _sender
    if not _sender.email_ready(await _sender.email_config()):
        return web.json_response({"ok": False, "error": "Verification is briefly "
                                  "unavailable — try again in a few minutes."})
    ok, msg = await _sender.issue_email_otp(uid, email)
    if not ok:
        return web.json_response({"ok": False, "error": msg})
    _bucket_record(_signup_otp_email_times, email, 3600)
    _bucket_record(_signup_otp_times, ip, 3600)
    return web.json_response({"ok": True})


async def signup_otp_check(request: web.Request):
    """Check the inline code; hands back the signed everify token the signup
    POST requires."""
    ctx, err = await _auth_json(request)
    if err is not None:
        return err
    uid, data = ctx
    email = str(data.get("email", "")).strip().lower()
    from . import sender as _sender
    ok, result = _sender.verify_email_otp(uid, str(data.get("code", "")))
    if not ok:
        return web.json_response({"ok": False, "error": result})
    if result.lower() != email:
        return web.json_response({"ok": False, "error": "That code was for a "
                                  "different address — request a fresh one."})
    return web.json_response({"ok": True, "token": await _everify_token(uid, email)})


def _reset_body(csrf: str, nxt: str, stage: str, email: str = "",
                error: str = "", note: str = "") -> str:
    err = f"<p class=err>{_esc(error)}</p>" if error else ""
    ok = f"<div class='banner ok'>{_esc(note)}</div>" if note else ""
    if stage == "done":
        inner = (f"{ok}<p class='muted lead'>You can sign in with your new "
                 f"password now.</p><a class=btn href='/signup?mode=in&next={_uq(nxt)}'>"
                 "Sign in →</a>")
    elif stage == "confirm":
        inner = f"""{ok}{err}
<form method=post action='/reset?next={_uq(nxt)}'><div class=card>
<input type=hidden name=csrf value='{csrf}'>
<input type=hidden name=act value=confirm>
<input type=hidden name=email value='{_esc(email)}'>
<label>6-digit code (sent to {_esc(email)})</label>
<input name=code inputmode=numeric maxlength=6 autocomplete=one-time-code
 placeholder='123456' style='letter-spacing:4px' required>
<label>New password</label>
<input name=password type=password autocomplete=new-password minlength=8 required>
<p class='muted small' style='margin:4px 0 0'>8+ characters including a special
character (e.g. <b>@</b>).</p>
<label>Confirm new password</label>
<input name=password2 type=password autocomplete=new-password minlength=8 required>
<div style=margin-top:14px><button class=btn>Set new password</button></div>
</div></form>
<form method=post action='/reset?next={_uq(nxt)}' style='margin-top:10px'>
<input type=hidden name=csrf value='{csrf}'>
<input type=hidden name=act value=request>
<input type=hidden name=email value='{_esc(email)}'>
<button class=linkbtn>Resend the code</button></form>"""
    else:
        inner = f"""{err}
<p class='muted lead'>Enter your account email — we'll send a 6-digit code so
only the real owner of the inbox can set a new password.</p>
<form method=post action='/reset?next={_uq(nxt)}'><div class=card>
<input type=hidden name=csrf value='{csrf}'>
<input type=hidden name=act value=request>
<label>Email</label>
<input name=email type=email autocomplete=email required value='{_esc(email)}'>
<div style=margin-top:14px><button class=btn>Email me a code</button></div>
</div></form>"""
    return "<h1>Reset your <span class=g>password</span></h1>" + inner


async def reset_get(request: web.Request):
    uid, is_new = await _ensure_uid(request)
    csrf = await _csrf(f"auth:{uid}")
    nxt = _safe_next(request.query.get("next", "/sell"))
    resp = _page("Reset password", _reset_body(csrf, nxt, "email"), noindex=True)
    if is_new:
        _set_uid_cookie(resp, await _sign_uid(uid), _is_https(request))
    return resp


async def reset_post(request: web.Request):
    """Password reset by email OTP only. The generic 'if registered, a code is
    on its way' phrasing never confirms whether an address has an account."""
    uid = await _uid_from_cookie(request)
    if uid is None:
        raise web.HTTPFound("/reset")
    data = await request.post()
    nxt = _safe_next(request.query.get("next", "/sell"))
    csrf = await _csrf(f"auth:{uid}")
    if not hmac.compare_digest(str(data.get("csrf", "")), csrf):
        return _page("Reset password", _reset_body(
            csrf, nxt, "email", error="That form expired — try again."),
            noindex=True)
    act = str(data.get("act", ""))
    email = str(data.get("email", "")).strip().lower()
    if not _EMAIL_RE.match(email) or len(email) > 190:
        return _page("Reset password", _reset_body(
            csrf, nxt, "email", error="Enter a valid email address."),
            noindex=True)
    async with Session() as s:
        acct = await s.scalar(select(Account).where(Account.email == email))

    if act == "request":
        ip = _client_ip(request)
        if _bucket_throttled(_reset_times, ip, 6, 3600):
            return _page("Reset password", _reset_body(
                csrf, nxt, "email", email=email,
                error="Too many reset requests — try again later."), noindex=True)
        _bucket_record(_reset_times, ip, 3600)
        from . import sender as _sender
        if acct is not None and acct.pw_hash:
            await _sender.issue_email_otp(-(_ACCT_BASE + acct.id), email)
        elif acct is not None and acct.google_sub:
            # a Google account has no password to reset — adding one via
            # mailbox access would reopen the pre-hijack hole Google closed
            return _page("Reset password", _reset_body(
                csrf, nxt, "email", email=email,
                error="This account signs in with Google — use the Google "
                "button on the sign-in page."), noindex=True)
        return _page("Reset password", _reset_body(
            csrf, nxt, "confirm", email=email,
            note=f"If {email} is registered, a 6-digit code is on its way."),
            noindex=True)

    if act == "confirm":
        password = str(data.get("password", ""))
        pw_err = _valid_password(password)
        if pw_err:
            return _page("Reset password", _reset_body(
                csrf, nxt, "confirm", email=email, error=pw_err), noindex=True)
        if password != str(data.get("password2", "")):
            return _page("Reset password", _reset_body(
                csrf, nxt, "confirm", email=email,
                error="The two passwords don't match."), noindex=True)
        from . import sender as _sender
        ok = False
        if acct is not None and acct.pw_hash:
            got, result = _sender.verify_email_otp(-(_ACCT_BASE + acct.id),
                                                   str(data.get("code", "")))
            ok = got and result.lower() == email
        if not ok:
            return _page("Reset password", _reset_body(
                csrf, nxt, "confirm", email=email,
                error="That code isn't right (or expired) — check the email "
                "or resend."), noindex=True)
        salt = secrets.token_hex(16)
        async with Session() as s:
            fresh = await s.scalar(select(Account).where(Account.email == email))
            if fresh is None:
                raise web.HTTPFound("/reset")
            fresh.pw_salt = salt
            fresh.pw_hash = await _hash_pw(password, salt)
            fresh.email_verified = True     # the OTP just proved the mailbox
            # sign out EVERY device that used the old password — a thief with
            # a stolen session doesn't survive the reset
            fresh.sess_ver = (fresh.sess_ver or 0) + 1
            await s.commit()
        log.info("password reset via email OTP for account #%s", fresh.id)
        return _page("Reset password", _reset_body(
            csrf, nxt, "done", note="✅ Password updated."), noindex=True)

    raise web.HTTPFound("/reset")


async def verify_email_get(request: web.Request, error: str = "", ok: str = ""):
    """OTP entry after a manual signup — the address only starts receiving
    order updates once the 6-digit code checks out."""
    acct = await _account_from_request(request)
    if acct is None:
        raise web.HTTPFound("/signup")
    nxt = _safe_next(request.query.get("next", "/sell"))
    if acct.email_verified:
        raise web.HTTPFound(nxt)
    uid = await _uid_from_cookie(request)
    csrf = await _csrf(f"auth:{uid}")
    note = (f"<div class='banner warn'>{_esc(error)}</div>" if error
            else f"<div class='banner ok'>{_esc(ok)}</div>" if ok else "")
    body = f"""<div class=authwrap><div class=card style='max-width:430px;margin:40px auto'>
<h1 style='margin-top:0'>Check your inbox</h1>
<p class=muted>We sent a 6-digit code to <b>{_esc(acct.email)}</b>. Enter it to
start receiving your order confirmations and payment receipts by email.</p>{note}
<form method=post action='/verify-email?next={_uq(nxt)}'>
<input type=hidden name=csrf value='{csrf}'><input type=hidden name=act value=verify>
<label>Verification code</label>
<input name=code inputmode=numeric autocomplete=one-time-code maxlength=6
 placeholder='123456' style='letter-spacing:6px;font-size:1.3em;text-align:center'>
<div class=row style='margin-top:12px'><button>Verify email</button></div></form>
<form method=post action='/verify-email?next={_uq(nxt)}' style='margin-top:10px'>
<input type=hidden name=csrf value='{csrf}'><input type=hidden name=act value=resend>
<button class='btn small' style='background:var(--surface-2);color:var(--text)'>Resend code</button>
</form>
<p class='muted small' style='margin-top:14px'>Wrong address? <a href='/signup'>Sign up
again</a> with the right one. You can also <a href='{_esc(nxt)}'>skip for now</a> —
order emails stay off until the address is verified.</p>
</div></div>"""
    return _page("Verify email", body, noindex=True, acct=acct.name or acct.email)


async def verify_email_post(request: web.Request):
    acct = await _account_from_request(request)
    if acct is None:
        raise web.HTTPFound("/signup")
    uid = await _uid_from_cookie(request)
    data = await request.post()
    nxt = _safe_next(request.query.get("next", "/sell"))
    if not hmac.compare_digest(str(data.get("csrf", "")), await _csrf(f"auth:{uid}")):
        return await verify_email_get(request, error="That form expired — try again.")
    from . import sender as _sender
    if str(data.get("act", "")) == "resend":
        ok, msg = await _sender.issue_email_otp(uid, acct.email)
        return await verify_email_get(
            request, ok=f"Fresh code sent to {acct.email}." if ok else "",
            error="" if ok else msg)
    ok, result = _sender.verify_email_otp(uid, str(data.get("code", "")))
    if not ok:
        return await verify_email_get(request, error=result)
    if result.lower() != acct.email.lower():
        return await verify_email_get(request, error="That code was for a "
                                      "different address — request a fresh one.")
    async with Session() as s:
        fresh = await s.get(Account, acct.id)
        if fresh is not None:
            fresh.email_verified = True
            await s.commit()
    raise web.HTTPFound(nxt)


async def stock_get(request: web.Request, error: str = ""):
    acct = await _account_from_request(request)
    if acct is None:
        raise web.HTTPFound("/signup")
    nxt = _safe_next(str(request.query.get("next", "") or "/sell"))
    if acct.stock and not error:
        raise web.HTTPFound(nxt)
    csrf = await _csrf(f"auth:{_acct_uid(acct.id)}")
    err = f"<p class=err>{_esc(error)}</p>" if error else ""
    body = f"""<h1>One last <span class=g>step</span></h1>
<p class='muted lead'>How much USDT do you plan to sell per day? This helps the
desk keep enough INR float ready so your payouts never wait.</p>{err}
<form method=post action=/signup/stock><div class=card>
<input type=hidden name=csrf value='{csrf}'>
<input type=hidden name=next value='{_esc(nxt)}'>
<label>Daily selling stock (USDT)</label>
{_stock_pick()}
<div style=margin-top:16px><button class=btn>Start selling →</button></div>
</div></form>"""
    return _page("Almost done — P2P Desk", body, noindex=True, acct=acct.email)


async def stock_post(request: web.Request):
    acct = await _account_from_request(request)
    if acct is None:
        raise web.HTTPFound("/signup")
    data = await request.post()
    nxt = _safe_next(str(data.get("next", "")))
    if not hmac.compare_digest(str(data.get("csrf", "")),
                               await _csrf(f"auth:{_acct_uid(acct.id)}")):
        return await stock_get(request, "That form expired — please try again.")
    tier = str(data.get("stock", ""))
    if tier not in _STOCK_TIERS:
        return await stock_get(request, "Please pick one of the options.")
    first = not acct.stock
    async with Session() as s:
        row = await s.get(Account, acct.id)
        if row is None:
            raise web.HTTPFound("/signup")
        row.stock = tier
        await s.commit()
        acct = row
    if first:
        await _notify_signup(request, acct)
    raise web.HTTPFound(nxt)


async def logout(request: web.Request):
    # POST + CSRF so a third-party page can't force-logout a visitor with a
    # cross-site GET (e.g. an <img src=/logout>).
    uid = await _uid_from_cookie(request)
    if uid is not None:
        data = await request.post()
        if not hmac.compare_digest(str(data.get("csrf", "")),
                                   await _csrf(f"auth:{uid}")):
            raise web.HTTPFound("/my")
    resp = web.HTTPFound("/")
    resp.del_cookie(COOKIE)
    return resp


def _logout_form(uid: int, csrf: str, label: str = "Sign out") -> str:
    return (f"<form method=post action=/logout style='display:inline'>"
            f"<input type=hidden name=csrf value='{csrf}'>"
            f"<button class=linkbtn>{_esc(label)}</button></form>")


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
    acct = await _account_from_request(request)
    me = acct.email if acct else ""
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
                     + _fabs_html(support, whatsapp), acct=me)
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
        from . import banks as _banks
        pbank = p.get("bank", "")
        bank_opts = ("<option value='' disabled" +
                     (" selected" if not pbank else "") + ">Select your bank…</option>"
                     + "".join(
                         f"<option{' selected' if n == pbank else ''}>{_esc(n)}</option>"
                         for n in _banks.bank_names()))
        pat = p.get("acctype", "")
        acctype_opts = ("<option value='' disabled" +
                        (" selected" if not pat else "") + ">Type…</option>"
                        + "".join(
                            f"<option{' selected' if t == pat else ''}>{_esc(t)}</option>"
                            for t in _banks.ACCOUNT_TYPES))
        bank_codes_js = json.dumps({n: c for n, c in _banks.BANKS})
        body = f"""
<h1>Sell USDT</h1>
{_trust_strip()}
<p class='muted small'>Fill this once — your deposit address and exact amount come next.
The quote stays live for {ttl} minutes after you submit.</p>
{err}
<form method=post action=/sell id=sellform><div class=card>
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
<input name=holder autocomplete="name" value="{_esc(p.get('holder', ''))}">
<label>Bank</label>
<select name=bank id=bank>{bank_opts}</select>
<div class=row style="gap:12px">
<div style="flex:2"><label>Account number</label>
<input name=account id=acct inputmode=numeric autocomplete="off"
 value="{_esc(p.get('account', ''))}"></div>
<div style="flex:1"><label>Account type</label>
<select name=acctype id=acctype>{acctype_opts}</select></div></div>
<label>IFSC code</label>
<input name=ifsc id=ifsc autocapitalize=characters autocomplete="off" maxlength=11
 placeholder="e.g. HDFC0001234" value="{_esc(p.get('ifsc', ''))}"
 style="text-transform:uppercase">
<p class=hint id=ifschint style="margin:6px 0 0"></p>
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
function bankReq(on){['holder','bank','account','ifsc','acctype'].forEach(function(n){
  var el=document.getElementsByName(n)[0];if(el)el.required=on});}
function updBank(){
  if(!pick){bankReq(true);return}
  var sel=pick.querySelector('input:checked');
  var isNew=!sel||sel.value==='new';
  nb.style.display=isNew?'':'none';bankReq(isNew);}
if(pick)pick.addEventListener('change',updBank);updBank();
// live IFSC ↔ bank check: the 4-letter IFSC prefix must match the picked bank
var CODES=""" + bank_codes_js + """;
var bsel=document.getElementById('bank'),ifsc=document.getElementById('ifsc'),
    ih=document.getElementById('ifschint');
function ifscChk(){
  if(!ifsc||!ih)return true;
  var v=(ifsc.value||'').toUpperCase().replace(/\\s/g,'');ifsc.value=v;
  var bank=bsel?bsel.value:'',code=CODES[bank]||'';
  if(!v){ih.className='hint';ih.textContent=code?('This bank\\u2019s IFSC starts with '+code+'0'):'';return false}
  var ex=code?(code+'0001234'):'HDFC0001234';
  if(!/^[A-Z]{4}0[A-Z0-9]{6}$/.test(v)){ih.className='hint bad';
    ih.textContent='IFSC should be 11 chars, e.g. '+ex+'.';return false}
  if(code&&v.slice(0,4)!==code){ih.className='hint bad';
    ih.textContent='That IFSC isn\\u2019t '+bank+' \\u2014 its codes start with '+code+'0.';return false}
  ih.className='hint';ih.innerHTML='\\u2713 IFSC looks valid';ih.style.color='var(--ok)';return true}
function ifscPh(){if(!ifsc)return;var code=CODES[bsel?bsel.value:'']||'';
  ifsc.placeholder='e.g. '+(code?code+'0001234':'HDFC0001234')}
if(ifsc){ifsc.addEventListener('input',ifscChk)}
if(bsel){bsel.addEventListener('change',function(){ih.style.color='';ifscPh();ifscChk()})}
ifscPh();
</script>""")
        # Staged submit overlay: the browser POST is near-instant, so without
        # this the "processing" moment is invisible. Steps tick while the form
        # ACTUALLY submits at the end — pure presentation, zero flow change,
        # and a no-JS browser still posts natively.
        body += """
<div id=procwrap style="display:none;position:fixed;inset:0;z-index:80;
 background:rgba(14,19,48,.55);backdrop-filter:blur(3px)">
 <div style="max-width:380px;margin:18vh auto 0;background:var(--surface);
  border:1px solid var(--border);border-radius:16px;box-shadow:var(--shadow);
  padding:26px 26px 20px">
  <div style="font-weight:800;font-size:1.05rem;margin-bottom:14px">
   Setting up your order&hellip;</div>
  <div id=procsteps></div>
  <p class="muted small" style="margin:14px 0 0">Do not close this page.</p>
 </div>
</div>
<style>
.pstep{display:flex;gap:10px;align-items:center;padding:7px 0;color:var(--muted);
 font-size:.95rem;transition:color .2s}
.pstep .pic{width:22px;height:22px;border-radius:50%;flex:none;display:flex;
 align-items:center;justify-content:center;font-size:13px;font-weight:800;
 border:2px solid var(--border);color:transparent}
.pstep.on{color:var(--text)}
.pstep.on .pic{border-color:var(--accent);border-top-color:transparent;
 animation:pspin .7s linear infinite}
.pstep.done{color:var(--text)}
.pstep.done .pic{border-color:var(--accent);background:var(--accent);
 color:#062b1a;animation:none}
@keyframes pspin{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion:reduce){.pstep.on .pic{animation:none;
 border-top-color:var(--accent)}}
</style>
<script>
(function(){
 var f=document.getElementById('sellform');if(!f)return;
 var STEPS=['Verifying your details','Locking today\\u2019s rate',
   'Reserving your unique deposit amount','Creating your secure order'];
 var wrap=document.getElementById('procwrap'),
     box=document.getElementById('procsteps'),armed=false,bypass=false;
 f.addEventListener('submit',function(e){
   if(bypass)return;             // network-fallback native submit
   if(armed){e.preventDefault();return}
   if(!f.checkValidity())return; // let the browser show what's missing
   e.preventDefault();armed=true;
   var go=document.getElementById('go');if(go){go.disabled=true;go.style.opacity=.6}
   box.innerHTML=STEPS.map(function(s){
     return '<div class=pstep><span class=pic>\\u2713</span><span>'+s+'</span></div>'}).join('');
   wrap.style.display='block';document.body.style.overflow='hidden';
   var rows=box.children,i=0;
   // steps advance while the REAL request is in flight; the last one keeps
   // spinning until the server actually responds (backend-driven, not a timer)
   function step(){
     if(i>0)rows[i-1].className='pstep done';
     if(i<rows.length-1){rows[i].className='pstep on';i++;setTimeout(step,550)}
     else{rows[i].className='pstep on'}  // hold + spin on the last
   }
   step();
   fetch(f.action,{method:'POST',body:new FormData(f),redirect:'follow',
     headers:{'X-Requested-With':'fetch'}})
    .then(function(r){
      if(r.redirected){for(var k=0;k<rows.length;k++)rows[k].className='pstep done';
        window.location.assign(r.url);return}
      return r.text().then(function(html){  // validation error → re-render
        document.open();document.write(html);document.close()})})
    .catch(function(){bypass=true;f.submit()});  // network hiccup → native POST
 });
})();
</script>"""
        body += _fabs_html(support, whatsapp)
        resp = _page("Sell USDT for INR — Live Rate & Instant Quote | P2P Desk",
                     body, "Get your USDT deposit address and a locked INR rate "
                     "in one step. UPI, IMPS, CDM payouts across India.",
                     path="/sell", acct=me)
    if is_new:
        _set_uid_cookie(resp, await _sign_uid(uid), _is_https(request))
    return resp


async def _sell_gate(request: web.Request, error: str = "") -> web.Response:
    """The signup gate shown on /sell for visitors without an account — same
    URL and SEO meta as the sell page, with the auth forms where the order
    form will appear once they're signed in."""
    async with Session() as s:
        support = await get_support(s)
        whatsapp = await get_whatsapp(s)
    uid, is_new = await _ensure_uid(request)
    csrf = await _csrf(f"auth:{uid}")
    body = ("<h1>Sell USDT.<br><span class=g>Sign in to start.</span></h1>"
            "<p class='muted lead'>Create your free account — or sign in — and "
            "the sell form opens right here. Your orders, tickets and saved "
            "banks then follow your account on any device.</p>"
            + _auth_body(csrf, "/sell", "up", error, {})
            + _fabs_html(support, whatsapp))
    resp = _page("Sell USDT for INR — Live Rate & Instant Quote | P2P Desk",
                 body, "Get your USDT deposit address and a locked INR rate "
                 "in one step. UPI, IMPS, CDM payouts across India.",
                 path="/sell")
    if is_new:
        _set_uid_cookie(resp, await _sign_uid(uid), _is_https(request))
    return resp


async def sell_get(request: web.Request):
    acct = await _account_from_request(request)
    if acct is None:
        return await _sell_gate(request)
    if not acct.stock:
        raise web.HTTPFound("/signup/stock?next=/sell")
    return await _sell_form(request)


async def sell_post(request: web.Request):
    acct = await _account_from_request(request)
    if acct is None:
        return await _sell_gate(request, "Please sign in to continue.")
    if not acct.stock:
        raise web.HTTPFound("/signup/stock?next=/sell")
    data = await request.post()
    uid = await _uid_from_cookie(request)
    if uid is None:
        return await _sell_form(request, "Please enable cookies and try again.")
    if not hmac.compare_digest(str(data.get("csrf", "")), await _csrf(f"sell:{uid}")):
        return await _sell_form(request, "That form expired — please try again.")
    prefill = {k: str(data.get(k, "")).strip()
               for k in ("service", "usd", "holder", "bank", "account", "ifsc",
                         "acctype", "network", "card_id")}
    ip = _client_ip(request)
    if _throttled(ip):
        return await _sell_form(request, "Too many orders from this connection — "
                                "please wait a while or contact support.", prefill)

    from .handlers.sell import _tag_amount
    from .handlers.start import make_bank_label

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
        from . import banks as _banks
        bank = prefill["bank"]
        ifsc = _banks.norm_ifsc(prefill["ifsc"])
        prefill["ifsc"] = ifsc                       # echo the normalised IFSC back
        acctype = prefill["acctype"]
        if not _banks.is_bank(bank):
            return await _sell_form(request, "Please pick your bank from the list.", prefill)
        if not (prefill["holder"] and prefill["account"].isdigit()
                and 6 <= len(prefill["account"]) <= 20):
            return await _sell_form(request, "Please check the details — the account "
                                    "number should be digits only (6–20).", prefill)
        if not _banks.acct_type_ok(acctype):
            return await _sell_form(request, "Please choose the account type "
                                    "(Savings or Current).", prefill)
        ifsc_err = _banks.ifsc_error(bank, ifsc)
        if ifsc_err:
            return await _sell_form(request, ifsc_err, prefill)
        if len(prefill["holder"]) > 80:
            return await _sell_form(request, "That holder name looks too long.", prefill)
        # bank name first — make_bank_label derives "<Bank> ••1234" from line 0
        details = (f"{bank}\nA/c holder: {prefill['holder']}\n"
                   f"A/C {prefill['account']}\nIFSC {ifsc}\nType: {acctype}")

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
        # email-only data is read BEFORE the commit: after it, nothing that can
        # raise may sit between the persisted order and the customer's redirect
        ttl_min = await get_deposit_ttl(s)
        await s.commit()
        token = order.web_token
        order_id = order.id
        bank_label = card.label

    # The client IP is otherwise invisible (the site runs with access logging
    # off so order tokens stay out of the logs), and it is the one thing that
    # proves the reverse proxy is forwarding visitors correctly: if every order
    # here shows the SAME address, the per-IP limits are all sharing one bucket
    # and real customers will start being refused. Also the first thing you want
    # when investigating abuse.
    log.info("web order #%s created from %s (%s %.2f USDT)",
             order_id, ip, service, usd)
    _record_order(ip)
    # Emailed confirmation of what they submitted (fire-and-forget; selling
    # requires a signed-in account, so acct is always present here). Only a
    # VERIFIED address gets mail — unverified/fake signups stay silent.
    try:
        from . import sender as _sender
        if acct.email and acct.email_verified:
            base = ((settings.site_url or "").rstrip("/")
                    or ("https://" if _is_https(request) else "http://")
                    + request.host)
            subj, inner = _sender.order_created_email(
                texts.tag(order_id), order.usd_amount,
                SERVICES.get(service, service), rate, order.inr_amount,
                bank_label, "BEP20 (BSC)" if net_key == "BEP20" else "TRC20 (TRON)",
                f"{base}/o/{token}", ttl_min)
            await _sender.send_transactional(acct.email, acct.name or "", subj,
                                             inner, legal=True)
    except Exception:
        log.exception("order confirmation email failed")
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
<div id=seenbn class="banner ok" style="display:none"><b>Deposit detected
on-chain!</b> It's confirming now — usually under a minute. This page updates
by itself the moment it credits.</div>
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
<p class='muted small' style="margin:10px 0 4px">Sent the USDT but nothing happened?
<a href="/support?order={_esc(token)}&amp;cat=deposit"><b>Create a support ticket</b></a>
— admins are alerted instantly.</p>
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
.then(function(j){{if(j.status!=='{st}')location.reload();
else if(j.seen)document.getElementById('seenbn').style.display='block';}})
.catch(function(){{}});}},6000);
</script>"""
        return _page(f"Order {texts.tag(order.id)} — send USDT", body + fabs,
                     noindex=True)

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
        return _page(f"Order {texts.tag(order.id)} — verified", body + fabs,
                     noindex=True)

    if st == OrderStatus.COMPLETED.value:
        body = f"""
<h1>Paid! {tagline}</h1>
<div class="banner ok"><b>₹{order.inr_amount:,.2f} sent to {_esc(bank_label)}.</b><br>
<span class=small>Thanks for trading with us — proof is shared on every deal.</span></div>
<a class=btn href="/sell">Sell more USDT</a>
<a class="btn ghost" href="/my">All my orders</a>
<p class='muted small'>{_support_html(support)}</p>"""
        return _page(f"Order {texts.tag(order.id)} — paid", body + fabs,
                     noindex=True)

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
<p class='muted small'>Paid but it expired anyway?
<a href="/support?order={_esc(token)}&amp;cat=deposit"><b>Create a support ticket</b></a>.</p>
<p class='muted small'>{_support_html(support)} — mention {tagline}</p>
<script>setInterval(function(){{fetch('/o/{_esc(token)}/status.json').then(r=>r.json())
.then(function(j){{if(j.status!=='{st}')location.reload();}}).catch(function(){{}});}},8000);</script>"""
        return _page(f"Order {texts.tag(order.id)}", body + fabs, noindex=True)

    # refund / rejected / anything else — simple status card
    body = f"""
<h1>Order {tagline}</h1>
<div class=banner>Status: <b>{_esc(st.replace('_', ' '))}</b></div>
<p class='muted small'>{_support_html(support)} — mention {tagline}</p>"""
    return _page(f"Order {texts.tag(order.id)}", body + fabs, noindex=True)




async def order_status(request: web.Request):
    order = await _order_by_token(request.match_info["token"])
    if order is None:
        raise web.HTTPNotFound()
    from . import scanner
    seen = (order.status == OrderStatus.AWAITING_DEPOSIT.value
            and order.id in scanner.deposit_seen)
    return web.json_response({"status": order.status, "seen": seen})


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
        updated = await try_transition(s, order.id, (OrderStatus.AWAITING_DEPOSIT,),
                                       OrderStatus.CANCELLED)
    if updated is not None:
        # cancellation notice (fire-and-forget) — closes the email loop opened
        # by the order-confirmation message
        try:
            from . import sender as _sender
            email, name = await _sender.email_for_uid(order.user_id)
            if email:
                subj, inner = _sender.order_cancelled_email(
                    texts.tag(order.id), order.usd_amount,
                    SERVICES.get(order.service, order.service),
                    "You cancelled this order before sending the deposit.")
                await _sender.send_transactional(email, name, subj, inner)
        except Exception:
            log.exception("cancellation email failed")
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
    # "TXID valid — under manual verification" email (fire-and-forget)
    try:
        from . import sender as _sender
        email, name = await _sender.email_for_uid(order.user_id)
        if email:
            base = ((settings.site_url or "").rstrip("/")
                    or ("https://" if _is_https(request) else "http://")
                    + request.host)
            subj, inner = _sender.claim_submitted_email(
                texts.tag(order.id), order.usd_amount,
                SERVICES.get(order.service, order.service), txid,
                f"{base}/o/{token}")
            await _sender.send_transactional(email, name, subj, inner)
    except Exception:
        log.exception("claim-submitted email failed")
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
    acct = await _account_from_request(request)
    me = acct.email if acct else ""
    if acct:
        lo = _logout_form(uid, await _csrf(f"auth:{uid}"))
        me_line = (f"<p class='muted small'>Signed in as <b>{_esc(me)}</b> · "
                   f"{lo}</p>")
    else:
        me_line = ("<p class='muted small'><a href='/signup?mode=in&next=/my'>"
                   "Sign in</a> to see your orders from every device.</p>")
    if uid is None:
        return _page("My orders", "<h1>My orders</h1>" + me_line +
                     "<div class=banner>No orders on "
                     "this device yet — they appear here after your first order.</div>"
                     "<a class=btn href='/sell'>Sell USDT</a>", noindex=True)
    async with Session() as s:
        orders = (await s.scalars(select(Order).where(Order.user_id == uid)
                                  .order_by(Order.id.desc()).limit(20))).all()
        support = await get_support(s)
        whatsapp = await get_whatsapp(s)
        card_ids = [o.bank_card_id for o in orders if o.bank_card_id]
        cards = {c.id: c for c in (await s.scalars(
            select(BankCard).where(BankCard.id.in_(card_ids)))).all()} if card_ids else {}
    if not orders:
        return _page("My orders", "<h1>My orders</h1>" + me_line +
                     "<div class=banner>No orders yet."
                     "</div><a class=btn href='/sell'>Sell USDT</a>",
                     noindex=True, acct=me)
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
            f"<div class='card sep'><b>{texts.tag(o.id)}</b> "
            f"<span class='badge {cls.get(o.status, '')}'>"
            f"{_esc(nice.get(o.status, o.status.replace('_', ' ')))}</span>"
            f"<div style='margin-top:10px'>{rows}</div>{bank}"
            f"<a class='btn ghost' style='margin-top:12px' "
            f"href='/o/{_esc(o.web_token)}'>Open live order page →</a></div>")
    async with Session() as s:
        tickets = (await s.scalars(select(Ticket).where(Ticket.user_id == uid)
                                   .order_by(Ticket.id.desc()).limit(10))).all()
    tk = ""
    if tickets:
        rows_t = "".join(
            f"<div class=kv><span class=k>{_tkt_tag(t.id)} · {_ist(t.created_at)}</span>"
            f"<span class=v><span class='badge {'warn' if t.status == 'open' else 'ok'}'>"
            f"{_esc(t.status)}</span></span></div>"
            for t in tickets)
        tk = (f"<h2>My tickets</h2><div class=card>{rows_t}"
              "<a class='btn ghost' style='margin-top:12px' href='/support'>"
              "New ticket</a></div>")
    return _page("My orders", f"<h1>My orders</h1>{me_line}{''.join(blocks)}"
                 "<a class=btn href='/sell'>New order</a>" + tk
                 + _fabs_html(support, whatsapp), noindex=True, acct=me)






# ── support tickets ──────────────────────────────────────────────────────────

_TICKET_CATS = {
    "deposit": "I sent USDT but my order isn't credited",
    "payout": "Deposit verified but INR not received",
    "other": "Something else",
}


def _tkt_tag(tid: int) -> str:
    return f"#TKT{tid:04d}"


async def support_get(request: web.Request, error: str = "",
                      prefill: dict | None = None):
    p = prefill or {}
    uid, is_new = await _ensure_uid(request)
    acct = await _account_from_request(request)
    if acct and not p.get("contact"):
        p["contact"] = acct.email        # signed-in default; still editable
    async with Session() as s:
        support = await get_support(s)
        whatsapp = await get_whatsapp(s)
        support_email = await get_support_email(s)
        orders = [] if is_new else (await s.scalars(
            select(Order).where(Order.user_id == uid)
            .order_by(Order.id.desc()).limit(10))).all()
    # arriving from an order page pre-selects that order + the deposit category
    otok = str(request.query.get("order", "") or p.get("order", ""))
    cat = str(request.query.get("cat", "") or p.get("category", "") or "deposit")
    if cat not in _TICKET_CATS:
        cat = "other"
    csrf = await _csrf(f"tkt:{uid}")
    cat_opts = "".join(
        f"<option value={k} {'selected' if k == cat else ''}>{v}</option>"
        for k, v in _TICKET_CATS.items())
    ord_opts = "<option value=''>— not about a specific order —</option>" + "".join(
        f"<option value='{_esc(o.web_token)}' "
        f"{'selected' if o.web_token == otok else ''}>"
        f"{texts.tag(o.id)} — {texts.usd_str(o.usd_amount)} USDT · "
        f"{_esc(o.status.replace('_', ' '))}</option>"
        for o in orders if o.web_token)
    err = f"<p class=err>{_esc(error)}</p>" if error else ""
    body = f"""
<h1>Support</h1>
<p class='muted small'>Tell us what happened — our admins see tickets instantly and
reply on the contact you give below. For deposit issues, include your transaction
hash (TXID) so we can check the chain right away.</p>
{('<div class=card style="padding-top:0"><b>Email us directly</b><br>'
  '<span class="muted small">We reply from a real inbox — fastest for detailed '
  'issues.</span><br>' + _email_pill(support_email) + '</div>') if support_email else ''}
{err}
<form method=post action=/support><div class=card>
<input type=hidden name=csrf value='{csrf}'>
<label>What's the problem?</label>
<select name=category>{cat_opts}</select>
<label>Related order (optional)</label>
<select name=order>{ord_opts}</select>
<label>Transaction hash / TXID (for deposit issues)</label>
<input name=txid placeholder="64-character hash, 0x… on BEP20"
 value="{_esc(p.get('txid', ''))}">
<label>Describe what happened</label>
<textarea name=message rows=5 required
 placeholder="What you sent, when, and what you expected">{_esc(p.get('message', ''))}</textarea>
<label>How do we reach you? (email / Telegram @username / WhatsApp number)</label>
<input name=contact placeholder="you@email.com, @yourname or +91…" required
 value="{_esc(p.get('contact', ''))}">
<div style="margin-top:16px"><button class=btn>Create ticket</button></div>
</div></form>
<p class='muted small'>Prefer chat? {_support_html(support)}{_email_html(support_email)}</p>
{_fabs_html(support, whatsapp)}"""
    resp = _page("Support — P2P Desk", body, path="/support",
                 acct=acct.email if acct else "")
    if is_new:
        _set_uid_cookie(resp, await _sign_uid(uid), _is_https(request))
    return resp


async def support_post(request: web.Request):
    data = await request.post()
    uid, is_new = await _ensure_uid(request)
    prefill = {k: str(data.get(k, "")).strip()
               for k in ("category", "order", "txid", "message", "contact")}
    if not hmac.compare_digest(str(data.get("csrf", "")), await _csrf(f"tkt:{uid}")):
        return await support_get(request, "That form expired — please try again.",
                                 prefill)
    ip = _client_ip(request)
    if _bucket_throttled(_ticket_times, ip, _TICKET_MAX_PER_HOUR, 3600):
        return await support_get(request, "Too many tickets from this connection — "
                                 "please wait a while.", prefill)
    cat = prefill["category"] if prefill["category"] in _TICKET_CATS else "other"
    msg = prefill["message"][:2000].strip()
    contact = prefill["contact"][:120].strip()
    txid = norm_txid(prefill["txid"]) if prefill["txid"] else ""
    if len(msg) < 10:
        return await support_get(request, "Please describe the problem in a bit "
                                 "more detail.", prefill)
    if len(contact) < 3:
        return await support_get(request, "Please give a contact so we can reach "
                                 "you back.", prefill)
    if txid and not TXID_RE.fullmatch(txid):
        return await support_get(request, "That TXID doesn't look right — it's 64 "
                                 "characters (0x… on BEP20). Leave it blank if "
                                 "you don't have it.", prefill)

    order_id = None
    async with Session() as s:
        if prefill["order"]:
            o = await s.scalar(select(Order).where(Order.web_token == prefill["order"]))
            # only their own order can be attached
            if o is not None and o.user_id == uid:
                order_id = o.id
        user = await s.get(User, uid)
        if user is None:
            user = User(id=uid, username="web", first_name="Web")
            s.add(user)
            await s.flush()
        t = Ticket(user_id=uid, order_id=order_id, category=cat,
                   txid=txid or None, contact=contact, message=msg)
        s.add(t)
        await s.commit()
        tid = t.id
    _bucket_record(_ticket_times, ip, 3600)
    log.info("support ticket %s from %s (%s, order=%s)", tid, ip, cat, order_id)

    # ping the admins in Telegram right away — the panel has the full record
    bot = request.app.get("bot")
    if bot is not None:
        from .helpers import notify_admins
        note = (f"🎫 <b>New support ticket {_tkt_tag(tid)}</b> — "
                f"{_esc(_TICKET_CATS[cat])}\n"
                + (f"Order {texts.tag(order_id)}\n" if order_id else "")
                + (f"TX <code>{_esc(txid[:16])}…</code>\n" if txid else "")
                + f"Contact: {_esc(contact)}\n"
                f"“{_esc(msg[:300])}”\n\nFull details in the panel → Tickets.")
        try:
            await notify_admins(bot, note)
        except Exception:
            log.exception("ticket admin notify failed")

    body = f"""
<h1>Ticket created {_tkt_tag(tid)}</h1>
<div class='banner ok'><b>Our admins have been alerted.</b><br>
<span class=small>We'll reach you at <b>{_esc(contact)}</b>. You can also check
this ticket's status any time under <a href='/my'>My orders</a>.</span></div>
<a class=btn href="/my">My orders &amp; tickets</a>"""
    resp = _page("Ticket created — P2P Desk", body, noindex=True)
    _set_uid_cookie(resp, await _sign_uid(uid), _is_https(request))
    return resp




async def guarantee_page(request: web.Request):
    async with Session() as s:
        support = await get_support(s)
        whatsapp = await get_whatsapp(s)
    body = f"""
<h1>The 100% Clean-Funds <span class=g>Guarantee</span></h1>
{_figure(_SVG_GUARANTEE)}
{_trust_strip()}
<p class="muted lead">The biggest fear when selling USDT in India isn't the rate —
it's receiving money from an unknown source and having your bank account frozen.
Our entire desk is built so that can't happen to you.</p>

<h2>Where every rupee comes from</h2>
<div class=card><div class=steps3>
<div class=step><div class=n>1</div><div><b>Market &amp; mutual-fund settlements</b><br>
<span class='muted small'>Withdrawals from stock-market and mutual-fund accounts —
money with a full paper trail behind it.</span></div></div>
<div class=step><div class=n>2</div><div><b>Cash &amp; CDM deposits</b><br>
<span class='muted small'>Physical cash deposited over the counter or by cash-deposit
machine — clean at the moment it enters the banking system.</span></div></div>
<div class=step><div class=n>3</div><div><b>Card &amp; gateway settlements</b><br>
<span class='muted small'>Credit-card and payment-gateway settlement funds from
regular commerce.</span></div></div></div></div>

<h2>What we never touch</h2>
<p class=muted>No gaming or betting money, no proceeds of scams or fraud, no
third-party transfers from strangers, nothing linked to sanctioned parties. Our
AML screening (see the <a href="/legal/aml">AML policy</a>) exists to keep those
out — and orders that fail it are refused or refunded, not passed on to you.</p>

<h2>Why this protects your account</h2>
<p class=muted>Bank freezes on P2P traders almost always trace back to one thing:
a payment whose sender's money was dirty. Because every payout we make comes from
the verified sources above, there is no dirty sender in the chain — which is why
we can stand behind the guarantee on every single deal, with proof shared on each
completed order.</p>

<h2>Our record, in the open</h2>
<p class=muted>Every deposit is verified on a public blockchain and every completed
deal gets a proof card. If anything about a payout ever concerns you, raise a
<a href="/support">support ticket</a> — admins see tickets instantly.</p>
<a class="btn cta-mid" href="/sell">Sell USDT with the guarantee</a>
{_fabs_html(support, whatsapp)}"""
    acct = await _account_from_request(request)
    return _page("100% Clean-Funds Guarantee — P2P Desk", body,
                 "Every payout from verified clean sources — market funds, cash "
                 "deposits, card settlements. Your account is never at risk.",
                 wide=True, path="/guarantee", acct=acct.email if acct else "")


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
<p class=muted>To create your account we collect your name, email, phone number,
and the daily volume band you select (or, if you sign in with Google, your name
and email from your verified Google profile). To process a payout we collect the
bank details you enter (account holder, bank, account number, IFSC) and your
order details. We also log your IP address, used only for rate-limiting and
abuse prevention.</p>
<h2>Cookies</h2>
<p class=muted>We set one first-party cookie that keeps you signed in and links
your orders to your account. {TRACKING_COOKIES}</p>
<h2>What we do with it</h2>
<p class=muted>Your account and bank details are used to run your account and pay
you, and are visible to the desk's admins for that purpose only. We do not sell
your data. Blockchain transactions are public by nature — your deposit's
transaction hash exists on a public ledger independent of us.</p>
{TRACKING_SECTION}<h2>Retention and your rights</h2>
<p class=muted>Order and payout records are retained for the desk's accounting
and dispute handling. To correct your saved bank details, simply add a new bank
on your next order. For account or data removal, contact support with your
account email — we honour it once there is no open order or dispute.</p>"""),

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
    if slug == "aml":
        body_html = _figure(_SVG_AML, "Every deposit and payout is screened — "
                            "clean funds in, clean funds out.") + body_html
    if slug == "privacy":
        # keep the policy truthful about whatever tracking is actually live
        if _tracking_active():
            names = [s for s, on in [("Google", _TRACKING["google"].strip()),
                                     ("Meta (Facebook)", _TRACKING["meta"].strip())]
                     if on]
            if _TRACKING["custom"].strip():
                names.append("other analytics/advertising tools")
            svcs = " and ".join(names) if names else "analytics/advertising tools"
            body_html = body_html.replace("{TRACKING_COOKIES}",
                "We also use analytics/advertising cookies set by "
                f"{svcs} to measure how visitors reach us from ads.")
            body_html = body_html.replace("{TRACKING_SECTION}",
                "<h2>Advertising &amp; analytics</h2><p class=muted>When you "
                "visit our public pages we load measurement tags from "
                f"{svcs} so we can see which ads bring visitors and improve "
                "them. These providers may set their own cookies and receive "
                "your IP address and the pages you view, under their own "
                "privacy policies. They are not loaded on your private order "
                "or account pages, and we never send them your bank details.</p>")
        else:
            body_html = body_html.replace(
                "{TRACKING_COOKIES}",
                "No analytics, no advertising trackers, no third-party cookies.")
            body_html = body_html.replace("{TRACKING_SECTION}", "")
    async with Session() as s:
        support = await get_support(s)
        whatsapp = await get_whatsapp(s)
    body = (f"<h1>{title}</h1>{body_html}"
            f"<p class='muted small'>Questions about this policy? "
            f"{_support_html(support)}</p>"
            + _fabs_html(support, whatsapp))
    acct = await _account_from_request(request)
    return _page(f"{html.unescape(title)} — P2P Desk", body,
                 path=f"/legal/{slug}", acct=acct.email if acct else "")




# ── learn: SEO article pages ─────────────────────────────────────────────────
# Articles are plain .md files in content/articles/, read from disk at request
# time — publishing a new article is `git pull` on the server, no restart.
# Front matter (title/desc/keyword/date) sits above a `---` line; the body is
# a small, escape-first markdown subset (##, ###, -, **bold**, [text](url)).

_CONTENT_DIR = Path(__file__).resolve().parent.parent / "content" / "articles"
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")


def _read_article(path: Path) -> dict | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    meta_raw, sep, body = raw.partition("\n---\n")
    if not sep:
        return None
    meta = {}
    for line in meta_raw.splitlines():
        k, colon, v = line.partition(":")
        if colon:
            meta[k.strip().lower()] = v.strip()
    if not meta.get("title"):
        return None
    return {"slug": path.stem, "title": meta["title"],
            "desc": meta.get("desc", ""), "keyword": meta.get("keyword", ""),
            "date": meta.get("date", ""), "body": body}


def _articles() -> list[dict]:
    if not _CONTENT_DIR.is_dir():
        return []
    arts = []
    for p in sorted(_CONTENT_DIR.glob("*.md")):
        if not _SLUG_RE.match(p.stem):
            continue
        art = _read_article(p)
        if art:
            arts.append(art)
    arts.sort(key=lambda a: (a["date"], a["slug"]), reverse=True)
    return arts


def _md_html(md: str) -> str:
    """Tiny escape-first markdown renderer. Everything is HTML-escaped BEFORE
    any markup is applied, and links only keep hrefs that are same-site paths
    or https — so article files can never inject script, whatever they say."""
    def inline(s: str) -> str:
        s = _esc(s)
        s = _MD_BOLD_RE.sub(r"<b>\1</b>", s)
        def link(m):
            txt, url = m.group(1), m.group(2)
            if url.startswith("/") or url.startswith("https://"):
                return f"<a href='{url}'>{txt}</a>"
            return txt
        return _MD_LINK_RE.sub(link, s)
    out: list[str] = []
    para: list[str] = []
    items: list[str] = []
    def flush_para():
        if para:
            out.append("<p class=muted>" + inline(" ".join(para)) + "</p>")
            para.clear()
    def flush_list():
        if items:
            out.append("<ul class=muted>"
                       + "".join(f"<li>{inline(i)}</li>" for i in items)
                       + "</ul>")
            items.clear()
    for line in md.splitlines():
        t = line.strip()
        if not t:
            flush_para(); flush_list()
        elif t.startswith("### "):
            flush_para(); flush_list()
            out.append(f"<h3>{inline(t[4:])}</h3>")
        elif t.startswith("## "):
            flush_para(); flush_list()
            out.append(f"<h2>{inline(t[3:])}</h2>")
        elif t.startswith("- "):
            flush_para()
            items.append(t[2:])
        else:
            flush_list()
            para.append(t)
    flush_para(); flush_list()
    return "".join(out)


async def learn_index(request: web.Request):
    async with Session() as s:
        support = await get_support(s)
        whatsapp = await get_whatsapp(s)
    acct = await _account_from_request(request)
    arts = _articles()
    cards = "".join(
        f"<div class='card sep'><a href='/learn/{a['slug']}'>"
        f"<b style='font-size:1.06rem'>{_esc(a['title'])}</b></a>"
        f"<p class='muted small' style='margin:6px 0 8px'>{_esc(a['desc'])}</p>"
        f"<a class=small href='/learn/{a['slug']}'>Read the guide →</a></div>"
        for a in arts) or ("<div class=banner>Guides are being written — "
                           "check back soon.</div>")
    body = ("<h1>Selling USDT in India,<br><span class=g>explained properly."
            "</span></h1>"
            "<p class='muted lead'>Practical guides on turning USDT into rupees "
            "in your bank — rates, safety, bank freezes, networks, timing — "
            "written by the desk that does this all day.</p>"
            + cards
            + "<a class='btn cta-mid' href='/sell'>Sell USDT now</a>"
            + _fabs_html(support, whatsapp))
    return _page("USDT to INR Guides — Sell USDT in India | P2P Desk", body,
                 "Guides on selling USDT for INR in India: best rates, staying "
                 "safe, avoiding bank freezes, TRC20 vs BEP20, and how instant "
                 "payouts work.", path="/learn",
                 acct=acct.email if acct else "")


async def learn_page(request: web.Request):
    slug = request.match_info["slug"]
    if not _SLUG_RE.match(slug):
        raise web.HTTPNotFound()
    art = None
    p = _CONTENT_DIR / f"{slug}.md"
    if p.is_file():
        art = _read_article(p)
    if art is None:
        raise web.HTTPNotFound()
    async with Session() as s:
        support = await get_support(s)
        whatsapp = await get_whatsapp(s)
    acct = await _account_from_request(request)
    ld = {"@context": "https://schema.org", "@type": "Article",
          "headline": art["title"], "description": art["desc"],
          "author": {"@type": "Organization",
                     "name": settings.biz_name or "P2P Desk"}}
    if art["date"]:
        ld["datePublished"] = art["date"]
    if settings.site_url:
        ld["mainEntityOfPage"] = (settings.site_url.rstrip("/")
                                  + f"/learn/{slug}")
    body = (f"<p class='muted small' style='margin:24px 0 0'>"
            f"<a href='/learn'>← All guides</a></p>"
            f"<h1>{_esc(art['title'])}</h1>"
            + _md_html(art["body"])
            + "<div class=card><b>Ready to sell?</b><p class='muted small' "
              "style='margin:6px 0 10px'>Live rates, on-chain verification and "
              "bank payout typically in "
            + _esc(settings.eta_text) + ". Backed by the "
              "<a href='/guarantee'>100% clean-funds guarantee</a>.</p>"
              "<a class=btn href='/sell'>Sell USDT now</a></div>"
            + _ld(ld) + _fabs_html(support, whatsapp))
    return _page(f"{art['title']} | P2P Desk", body, art["desc"],
                 path=f"/learn/{slug}", acct=acct.email if acct else "")


# ── about / robots / sitemap ─────────────────────────────────────────────────

async def about_page(request: web.Request):
    async with Session() as s:
        support = await get_support(s)
        whatsapp = await get_whatsapp(s)
    name = settings.biz_name.strip()
    ident = ""
    if name:
        rows = [("Business name", _esc(name))]
        if settings.biz_address.strip():
            rows.append(("Address", _esc(settings.biz_address)))
        if settings.biz_email.strip():
            rows.append(("Email", f"<a href='mailto:{_esc(settings.biz_email)}'>"
                                  f"{_esc(settings.biz_email)}</a>"))
        ident = "<div class=card>" + "".join(
            f"<div class=kv><span class=k>{k}</span><span class=v>{v}</span></div>"
            for k, v in rows) + "</div>"
    else:
        ident = ("<div class='banner warn'>Business identity not configured yet — "
                 "set P2P_BIZ_NAME / P2P_BIZ_ADDRESS / P2P_BIZ_EMAIL in .env.</div>")
    body = f"""
<h1>About this desk</h1>
<p class=muted>An independent over-the-counter desk for selling USDT (Tether) for
Indian rupees — operating on Telegram since before this website existed, now open
to everyone. Deposits are verified on a public blockchain and payouts are made by
the desk's own admins over UPI, IMPS, CDM and cheque.</p>
{ident}
<h2>Contact</h2>
<p class=muted>Fastest: the chat buttons on every page ({_support_html(support)}).
For a written record, <a href="/support">create a support ticket</a> — admins are
alerted instantly and reply on the contact you give.</p>
<h2>How we operate</h2>
<p class=muted>Rates are locked at order time and are all-inclusive. Every deposit
is matched on-chain by exact amount, every completed deal gets a proof card, and
every payout comes from verified clean sources — the
<a href="/guarantee">clean-funds guarantee</a> explains the sourcing, and the
<a href="/legal/terms">Terms of Use</a>, <a href="/legal/privacy">Privacy
Policy</a> and <a href="/legal/aml">AML policy</a> set out the rules we hold both
sides to.</p>
{_fabs_html(support, whatsapp)}"""
    acct = await _account_from_request(request)
    return _page("About & Contact — P2P Desk", body,
                 "Who runs this USDT-to-INR desk, how to reach us, and the rules "
                 "we operate by.", path="/about", acct=acct.email if acct else "")


_SITE_PATHS = ["/", "/sell", "/guarantee", "/learn", "/support", "/about",
               "/legal/terms", "/legal/privacy", "/legal/risks",
               "/legal/transactions", "/legal/aml"]


async def robots_txt(request: web.Request):
    lines = ["User-agent: *", "Disallow: /o/", "Disallow: /my",
             "Disallow: /auth/", "Allow: /"]
    if settings.site_url:
        lines.append(f"Sitemap: {settings.site_url.rstrip('/')}/sitemap.xml")
    return web.Response(text="\n".join(lines) + "\n", content_type="text/plain")


async def sitemap_xml(request: web.Request):
    base = settings.site_url.rstrip("/")
    if not base:
        raise web.HTTPNotFound(text="set P2P_SITE_URL to enable the sitemap")
    paths = _SITE_PATHS + [f"/learn/{a['slug']}" for a in _articles()]
    urls = "".join(f"<url><loc>{base}{p}</loc></url>" for p in paths)
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
           f"{urls}</urlset>")
    return web.Response(text=xml, content_type="application/xml")


# ── my banks (save/manage payout accounts) ───────────────────────────────────

async def banks_get(request: web.Request, error: str = "", ok: str = ""):
    """Dedicated page to save and manage payout bank accounts up front, so the
    sell form is one tap. Same validation as the sell flow."""
    acct = await _account_from_request(request)
    if acct is None:
        raise web.HTTPFound("/signup?next=/banks")
    uid = await _uid_from_cookie(request)
    from . import banks as _banks
    async with Session() as s:
        cards = (await s.scalars(select(BankCard).where(BankCard.user_id == uid)
                                 .order_by(BankCard.id.desc()))).all()
        support = await get_support(s)
        whatsapp = await get_whatsapp(s)
    csrf = await _csrf(f"banks:{uid}")
    seen, uniq = set(), []
    for c in cards:
        if c.details.strip() not in seen:
            seen.add(c.details.strip())
            uniq.append(c)
    if uniq:
        rows = "".join(
            f"<div class=card><div class=row style='align-items:flex-start'>"
            f"<div style='flex:1'><b>{_esc(c.label)}</b>"
            f"<pre style='white-space:pre-wrap;margin:8px 0 0;font-family:inherit;"
            f"color:var(--muted);font-size:.9rem'>{_esc(c.details)}</pre></div>"
            f"<form method=post action='/banks/{c.id}/delete' "
            f"onsubmit=\"return confirm('Remove this bank?')\">"
            f"<input type=hidden name=csrf value='{csrf}'>"
            f"<button class='btn small' style='background:var(--danger-soft);"
            f"color:var(--danger)'>Remove</button></form></div></div>"
            for c in uniq)
        listing = f"<h2>Saved banks ({len(uniq)})</h2>{rows}"
    else:
        listing = ("<div class=card><span class=muted>No saved banks yet — add "
                   "one below and it'll be ready to pick on every order.</span></div>")
    bank_opts = ("<option value='' disabled selected>Select your bank…</option>"
                 + "".join(f"<option>{_esc(n)}</option>" for n in _banks.bank_names()))
    type_opts = ("<option value='' disabled selected>Type…</option>"
                 + "".join(f"<option>{_esc(t)}</option>" for t in _banks.ACCOUNT_TYPES))
    codes = json.dumps({n: c for n, c in _banks.BANKS})
    err = f"<p class=err>{_esc(error)}</p>" if error else ""
    okb = f"<div class='banner ok'>{_esc(ok)}</div>" if ok else ""
    body = f"""<h1>My <span class=g>banks</span></h1>
<p class='muted lead'>Save your payout accounts once — then selling is a single tap.
Your banks are private to your account and used only for your INR payouts.</p>{okb}{err}
{listing}
<h2>Add a bank</h2>
<form method=post action=/banks id=bankform><div class=card>
<input type=hidden name=csrf value='{csrf}'>
<label>Account holder name</label>
<input name=holder autocomplete=name required maxlength=80>
<label>Bank</label><select name=bank id=bank>{bank_opts}</select>
<div class=row style='gap:12px'>
<div style='flex:2'><label>Account number</label>
<input name=account id=acct inputmode=numeric autocomplete=off required></div>
<div style='flex:1'><label>Account type</label>
<select name=acctype>{type_opts}</select></div></div>
<label>IFSC code</label>
<input name=ifsc id=ifsc autocapitalize=characters autocomplete=off maxlength=11
 placeholder='e.g. HDFC0001234' required style='text-transform:uppercase'>
<p class=hint id=ifschint style='margin:6px 0 0'></p>
<div style='margin-top:16px'><button class=btn>Save bank</button></div>
</div></form>
<script>
var CODES={codes};
var bsel=document.getElementById('bank'),ifsc=document.getElementById('ifsc'),
    ih=document.getElementById('ifschint');
function chk(){{var v=(ifsc.value||'').toUpperCase().replace(/\\s/g,'');ifsc.value=v;
  var code=CODES[bsel.value]||'';
  if(!v){{ih.className='hint';ih.textContent=code?('This bank\\u2019s IFSC starts with '+code+'0'):'';return}}
  var ex=code?(code+'0001234'):'HDFC0001234';
  if(!/^[A-Z]{{4}}0[A-Z0-9]{{6}}$/.test(v)){{ih.className='hint bad';ih.textContent='IFSC should be 11 chars, e.g. '+ex+'.';return}}
  if(code&&v.slice(0,4)!==code){{ih.className='hint bad';ih.textContent='That IFSC isn\\u2019t '+bsel.value+' \\u2014 codes start with '+code+'0.';return}}
  ih.className='hint';ih.style.color='var(--ok)';ih.innerHTML='\\u2713 IFSC looks valid'}}
function ph(){{var code=CODES[bsel.value]||'';ifsc.placeholder='e.g. '+(code?code+'0001234':'HDFC0001234')}}
ifsc.addEventListener('input',chk);bsel.addEventListener('change',function(){{ih.style.color='';ph();chk()}});ph();
</script>
{_fabs_html(support, whatsapp)}"""
    return _page("My banks", body, noindex=True, acct=acct.email)


async def banks_add(request: web.Request):
    acct = await _account_from_request(request)
    if acct is None:
        raise web.HTTPFound("/signup?next=/banks")
    uid = await _uid_from_cookie(request)
    data = await request.post()
    if not hmac.compare_digest(str(data.get("csrf", "")), await _csrf(f"banks:{uid}")):
        return await banks_get(request, error="That form expired — please try again.")
    from . import banks as _banks
    from .handlers.start import make_bank_label
    holder = str(data.get("holder", "")).strip()
    bank = str(data.get("bank", "")).strip()
    account = str(data.get("account", "")).strip()
    acctype = str(data.get("acctype", "")).strip()
    ifsc = _banks.norm_ifsc(str(data.get("ifsc", "")))
    if not _banks.is_bank(bank):
        return await banks_get(request, error="Please pick your bank from the list.")
    if not (holder and len(holder) <= 80 and account.isdigit()
            and 6 <= len(account) <= 20):
        return await banks_get(request, error="Check the holder name and a digits-"
                               "only account number (6–20).")
    if not _banks.acct_type_ok(acctype):
        return await banks_get(request, error="Please choose the account type.")
    ifsc_err = _banks.ifsc_error(bank, ifsc)
    if ifsc_err:
        return await banks_get(request, error=ifsc_err)
    details = (f"{bank}\nA/c holder: {holder}\nA/C {account}\n"
               f"IFSC {ifsc}\nType: {acctype}")
    async with Session() as s:
        # ensure the account's user row exists (negative uid)
        if await s.get(User, uid) is None:
            s.add(User(id=uid, username="web", first_name=holder[:60] or "Web"))
            await s.flush()
        dup = await s.scalar(select(BankCard).where(BankCard.user_id == uid,
                                                    BankCard.details == details))
        if dup is None:
            s.add(BankCard(user_id=uid, label=make_bank_label(details),
                           details=details))
            await s.commit()
    return await banks_get(request, ok="Bank saved — pick it on your next order.")


async def banks_delete(request: web.Request):
    acct = await _account_from_request(request)
    if acct is None:
        raise web.HTTPFound("/signup?next=/banks")
    uid = await _uid_from_cookie(request)
    data = await request.post()
    if not hmac.compare_digest(str(data.get("csrf", "")), await _csrf(f"banks:{uid}")):
        raise web.HTTPFound("/banks")
    try:
        cid = int(request.match_info["id"])
    except ValueError:
        raise web.HTTPFound("/banks")
    async with Session() as s:
        card = await s.get(BankCard, cid)
        if card is not None and card.user_id == uid:   # never delete another's card
            await s.delete(card)
            await s.commit()
    raise web.HTTPFound("/banks")


# ── unsubscribe (bulk-email opt-out) ──────────────────────────────────────────

async def _do_unsubscribe(email: str, token: str) -> bool:
    """Record an opt-out if the signed token matches the email. Idempotent."""
    norm = (email or "").strip().lower()
    if not norm or not unsub_valid(await _secret(), norm, token):
        return False
    async with Session() as s:
        if await s.get(Unsubscribe, norm) is None:
            s.add(Unsubscribe(email=norm))
            try:
                await s.commit()
            except IntegrityError:
                await s.rollback()   # raced another click — already recorded
    return True


def _unsub_expired():
    body = ("<h1>Link expired</h1>"
            "<p class=lead>This unsubscribe link isn't valid. If you keep getting "
            "mail, reply to any message and we'll remove you.</p>")
    return _page("Unsubscribe", body, noindex=True)


def _unsub_done():
    body = ("<h1>You're unsubscribed</h1>"
            "<p class=lead>You won't receive marketing emails from us again. "
            "Order and support emails still reach you.</p>"
            "<p><a class='btn' href='/'>Back to site</a></p>")
    return _page("Unsubscribed", body, noindex=True)


async def unsubscribe_get(request: web.Request):
    """A GET must NOT change state — mail-security scanners and link prefetchers
    fetch every URL in a delivered email, and each carries a valid token, so a
    mutating GET would silently opt real people out. Instead we validate the
    token for display only and show a one-tap Confirm button that POSTs."""
    email = request.query.get("e", "")
    token = request.query.get("t", "")
    if not unsub_valid(await _secret(), (email or "").strip().lower(), token):
        return _unsub_expired()
    action = f"/unsubscribe?e={quote(email)}&amp;t={quote(token)}"
    body = (f"<h1>Unsubscribe {_esc(email)}?</h1>"
            "<p class=lead>Stop receiving marketing emails. Order and support "
            "emails still reach you.</p>"
            f"<form method=post action='{action}'>"
            "<button class=btn type=submit>Unsubscribe me</button></form>")
    return _page("Unsubscribe", body, noindex=True)


async def unsubscribe_post(request: web.Request):
    """The only mutating path — the Confirm button above and the RFC 8058
    one-click List-Unsubscribe-Post header both land here. Body ignored; the
    signed token in the query authenticates."""
    ok = await _do_unsubscribe(request.query.get("e", ""),
                               request.query.get("t", ""))
    return _unsub_done() if ok else _unsub_expired()


# ── app ───────────────────────────────────────────────────────────────────────

async def start_site(bot):
    """Start the public customer site (same process/DB); returns the AppRunner
    or None when disabled (site_port=0)."""
    if not settings.site_port:
        log.info("customer website disabled (P2P_SITE_PORT=0)")
        return None
    await load_tracking()          # load marketing-pixel IDs into the cache
    await load_support_cache()     # load support email for the floating button
    app = web.Application(middlewares=[_sec_headers])
    app["bot"] = bot
    app.add_routes([
        web.get("/", home),
        web.get("/sell", sell_get),
        web.post("/sell", sell_post),
        web.get("/my", my_orders),
        web.get("/signup", signup_get),
        web.post("/signup", signup_post),
        web.post("/signin", signin_post),
        web.post("/auth/google", auth_google),
        web.get("/signup/stock", stock_get),
        web.post("/signup/stock", stock_post),
        web.post("/signup/otp", signup_otp_post),
        web.post("/signup/otp/check", signup_otp_check),
        web.get("/reset", reset_get),
        web.post("/reset", reset_post),
        web.get("/verify-email", verify_email_get),
        web.post("/verify-email", verify_email_post),
        web.get("/banks", banks_get),
        web.post("/banks", banks_add),
        web.post("/banks/{id:\\d+}/delete", banks_delete),
        web.post("/logout", logout),
        web.get("/learn", learn_index),
        web.get("/learn/{slug}", learn_page),
        web.get("/legal/{slug}", legal_page),
        web.get("/support", support_get),
        web.get("/guarantee", guarantee_page),
        web.get("/about", about_page),
        web.get("/robots.txt", robots_txt),
        web.get("/sitemap.xml", sitemap_xml),
        web.get("/unsubscribe", unsubscribe_get),
        web.post("/unsubscribe", unsubscribe_post),
        web.post("/support", support_post),
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
