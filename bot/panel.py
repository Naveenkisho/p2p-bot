"""Optional web admin panel for the P2P desk.

Runs in the same process as the bot (its own aiohttp server) and shares the
same database, so nothing extra to host. Disabled unless a panel password is
set. Binds to 127.0.0.1 by default — put nginx + TLS in front for remote
access; never expose the raw port to the internet, because this panel can
change the bot token and the admin list.

Security: required password, HMAC-signed HttpOnly SameSite=Strict session
cookie with a TTL, a CSRF token on every form, and a simple login throttle.
"""

import asyncio
import csv
import hashlib
import hmac
import html
import io
import logging
import os
import secrets
import time
from urllib.parse import quote

from aiohttp import web

from . import texts
from .actions import (
    complete_order,
    compose_announcement,
    confirm_deposit,
    decline_order,
    launch_broadcast,
    record_manual_order,
    refund_order,
    reject_refund,
)
from .config import SERVICES, settings
from .db import (
    MAX_QR_BYTES,
    Session,
    bep20_active,
    clear_network_qr,
    desk_state,
    get_bep20_address,
    get_bscscan_key,
    get_deposit_address,
    get_deposit_ttl,
    get_desk_open,
    get_network_qr_raw,
    get_rates,
    get_setting,
    get_support,
    get_support_email,
    get_whatsapp,
    set_network_qr,
    set_setting,
)
from .helpers import is_bep20, is_trc20
from .models import Account, Ticket, Order, OrderStatus, User
from . import sender
from .qr import qr_png
from sqlalchemy import and_, func, or_, select

log = logging.getLogger(__name__)

COOKIE = "p2p_panel"
SESSION_TTL = 12 * 3600
TABS = {
    "active": ("💰 Active", (OrderStatus.DEPOSIT_RECEIVED.value,
                             OrderStatus.PENDING_PAYOUT.value)),
    "pending": ("⏳ Pending", (OrderStatus.AWAITING_DEPOSIT.value,)),
    "refunds": ("↩️ Refunds", (OrderStatus.CANCELLED.value,
                               OrderStatus.REFUND_REQUESTED.value)),
    "done": ("✅ Done", (OrderStatus.COMPLETED.value, OrderStatus.REFUNDED.value,
                         OrderStatus.EXPIRED.value, OrderStatus.REFUND_REJECTED.value)),
}
_login_fails: dict[str, tuple[int, float]] = {}

# A cancelled order only needs the admin if the customer submitted a TXID (claiming
# they paid, or requesting a refund). A bare cancel — the customer just backed out
# before paying — is a dead order and should NOT clutter the actionable lists.
_HAS_CLAIM = or_(and_(Order.claim_txid.is_not(None), Order.claim_txid != ""),
                 and_(Order.refund_txid.is_not(None), Order.refund_txid != ""))


def _tab_filter(tab: str):
    """WHERE clause for a panel tab. 'refunds' shows only ACTIONABLE cancels (a TXID
    was submitted) plus refund requests; a bare cancel falls through to 'done'."""
    S = OrderStatus
    if tab == "pending":
        return Order.status == S.AWAITING_DEPOSIT.value
    if tab == "active":
        return Order.status.in_((S.DEPOSIT_RECEIVED.value, S.PENDING_PAYOUT.value))
    if tab == "refunds":
        return or_(Order.status == S.REFUND_REQUESTED.value,
                   and_(Order.status == S.CANCELLED.value, _HAS_CLAIM))
    # done / closed — including bare cancels (cancelled, no TXID submitted)
    closed = (S.COMPLETED.value, S.REFUNDED.value, S.EXPIRED.value, S.REFUND_REJECTED.value)
    return or_(Order.status.in_(closed),
               and_(Order.status == S.CANCELLED.value, ~_HAS_CLAIM))


# ── secrets / auth ────────────────────────────────────────────────────────────

async def _panel_password() -> str:
    async with Session() as s:
        db_pw = await get_setting(s, "panel_password")
    return (db_pw or "").strip() or settings.panel_password


async def _secret() -> bytes:
    if settings.panel_secret:
        return settings.panel_secret.encode()
    async with Session() as s:
        val = await get_setting(s, "panel_secret")
        if not val:
            val = secrets.token_hex(32)
            await set_setting(s, "panel_secret", val)
    return val.encode()


async def _sign(issued: int) -> str:
    mac = hmac.new(await _secret(), str(issued).encode(), hashlib.sha256).hexdigest()
    return f"{issued}.{mac}"


async def _valid_cookie(raw: str | None) -> bool:
    if not raw or "." not in raw:
        return False
    issued_s, _, _ = raw.partition(".")
    if not issued_s.isdigit():
        return False
    issued = int(issued_s)
    if issued + SESSION_TTL < int(time.time()):
        return False
    return hmac.compare_digest(raw, await _sign(issued))


async def _csrf_for(request: web.Request) -> str:
    raw = request.cookies.get(COOKIE, "")
    issued = raw.partition(".")[0] or "0"
    return hmac.new(await _secret(), f"csrf:{issued}".encode(), hashlib.sha256).hexdigest()


def _authed(handler):
    async def wrapper(request: web.Request):
        if not await _valid_cookie(request.cookies.get(COOKIE)):
            raise web.HTTPFound("/login")
        return await handler(request)
    return wrapper


async def _check_csrf(request: web.Request, data) -> bool:
    return hmac.compare_digest(data.get("csrf", ""), await _csrf_for(request))


# ── HTML ──────────────────────────────────────────────────────────────────────

def _esc(v) -> str:
    return html.escape("" if v is None else str(v))


_STYLE = """
*{box-sizing:border-box}
:root{
 --bg:#eef1f5;--surface:#ffffff;--surface-2:#f7f9fc;--border:#e3e7ee;
 --text:#131722;--muted:#5b6577;--faint:#8b95a6;
 --accent:#4f46e5;--accent-ink:#ffffff;--accent-soft:#eceafe;
 --ok:#15803d;--ok-soft:#e6f5ec;--warn:#b45309;--warn-soft:#fbefdd;
 --danger:#b42318;--danger-soft:#fce9e6;--info:#1d4ed8;--info-soft:#e7eefe;
 --shadow:0 1px 2px rgba(16,24,40,.05),0 4px 14px rgba(16,24,40,.06);
 --radius:14px;color-scheme:light dark}
@media (prefers-color-scheme:dark){:root{
 --bg:#0c0e13;--surface:#161a22;--surface-2:#1c212b;--border:#29303c;
 --text:#e8ebf2;--muted:#98a1b2;--faint:#697487;
 --accent:#8b8bff;--accent-ink:#0c0e13;--accent-soft:#20223a;
 --ok:#57d98a;--ok-soft:#15251b;--warn:#f5b544;--warn-soft:#2a2212;
 --danger:#f6837a;--danger-soft:#2a1716;--info:#66a6ff;--info-soft:#122036;
 --shadow:0 1px 2px rgba(0,0,0,.5)}}
html{-webkit-text-size-adjust:100%}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
 margin:0;background:var(--bg);color:var(--text);line-height:1.5;
 font-feature-settings:"tnum" 1;-webkit-font-smoothing:antialiased}
.wrap{max-width:960px;margin:0 auto;padding:0 16px 40px}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
h1{font-size:1.5rem;font-weight:700;letter-spacing:-.01em;margin:18px 0 6px}
h2{font-size:1.02rem;font-weight:650;letter-spacing:-.005em;margin:22px 0 8px;color:var(--text)}
.appbar{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:6px;
 flex-wrap:wrap;background:color-mix(in srgb,var(--surface) 90%,transparent);
 backdrop-filter:saturate(1.4) blur(8px);border-bottom:1px solid var(--border);
 padding:11px 16px;margin:0 -16px 6px}
.appbar .brand{font-weight:750;letter-spacing:-.02em;margin-right:8px;display:flex;
 align-items:center;gap:8px}
.appbar .brand .dot{width:9px;height:9px;border-radius:50%;background:var(--accent);
 box-shadow:0 0 0 4px var(--accent-soft)}
.appbar nav{display:flex;gap:2px;flex-wrap:wrap;align-items:center}
.appbar nav a{color:var(--muted);padding:7px 11px;border-radius:9px;font-size:.92rem;
 font-weight:550;transition:background .12s,color .12s}
.appbar nav a:hover{background:var(--surface-2);text-decoration:none;color:var(--text)}
.appbar nav a.on{background:var(--accent-soft);color:var(--accent)}
.appbar .sp{flex:1}
.appbar .out{color:var(--faint);font-size:.9rem}
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
 padding:15px 16px;margin:12px 0;overflow-wrap:anywhere;box-shadow:var(--shadow)}
.card.link{transition:border-color .12s,transform .06s}
.card.link:hover{border-color:var(--accent)}
.muted{color:var(--muted);font-size:.9em}
.faint{color:var(--faint)}
.badge{display:inline-flex;align-items:center;gap:5px;font-size:.72rem;font-weight:650;
 letter-spacing:.02em;text-transform:uppercase;padding:3px 9px;border-radius:999px;
 background:var(--surface-2);color:var(--muted);border:1px solid var(--border);white-space:nowrap}
.badge.ok{background:var(--ok-soft);color:var(--ok);border-color:transparent}
.badge.warn{background:var(--warn-soft);color:var(--warn);border-color:transparent}
.badge.danger{background:var(--danger-soft);color:var(--danger);border-color:transparent}
.badge.info{background:var(--info-soft);color:var(--info);border-color:transparent}
.badge.accent{background:var(--accent-soft);color:var(--accent);border-color:transparent}
.amt{font-variant-numeric:tabular-nums;font-weight:700;font-size:1.12rem;letter-spacing:-.01em}
.arrow{color:var(--faint);margin:0 6px}
.banner{border:1px solid var(--border);border-left:4px solid var(--muted);
 background:var(--surface);border-radius:12px;padding:11px 14px;margin:12px 0;box-shadow:var(--shadow)}
.banner.ok{border-left-color:var(--ok)}
.banner.warn{border-left-color:var(--warn)}
.banner.danger{border-left-color:var(--danger)}
label{display:block;font-size:.86rem;font-weight:550;color:var(--muted);margin:10px 0 4px}
input,select,textarea{width:100%;padding:10px 12px;margin:0 0 4px;font-size:.98rem;
 border-radius:10px;border:1px solid var(--border);background:var(--surface-2);
 color:var(--text);font-family:inherit}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--accent);
 box-shadow:0 0 0 3px var(--accent-soft)}
button,.btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;
 padding:10px 16px;border:0;border-radius:10px;background:var(--accent);color:var(--accent-ink);
 font-size:.95rem;font-weight:600;cursor:pointer;font-family:inherit;transition:filter .12s}
button:hover,.btn:hover{filter:brightness(1.06);text-decoration:none}
button.warn{background:var(--warn)} button.danger{background:var(--danger)}
button.ghost{background:var(--surface-2);color:var(--text);border:1px solid var(--border)}
code{background:var(--surface-2);border:1px solid var(--border);padding:2px 6px;border-radius:6px;
 font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.86em;word-break:break-all;overflow-wrap:anywhere}
.bankwrap{margin:6px 0}
.bankblk{background:var(--surface-2);border:1px solid var(--border);border-radius:10px;
 padding:10px 12px;margin:0;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
 font-size:.9em;white-space:pre-wrap;word-break:break-word;overflow-wrap:anywhere}
.copybtn{margin-top:8px;padding:6px 12px;background:var(--surface-2);color:var(--text);
 border:1px solid var(--border);font-size:.82em;font-weight:600;border-radius:8px;cursor:pointer}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.duo{display:grid;grid-template-columns:1fr 1fr;gap:18px;align-items:start}
.duo h2{margin-top:0}
@media (max-width:820px){.duo{grid-template-columns:1fr}}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin:10px 0 4px}
.tabs a{padding:8px 14px;border-radius:10px;background:var(--surface);border:1px solid var(--border);
 color:var(--muted);font-weight:600;font-size:.9rem}
.tabs a:hover{text-decoration:none;border-color:var(--accent)}
.tabs a.on{background:var(--accent);border-color:var(--accent);color:var(--accent-ink)}
.exportbar{font-size:.85rem;color:var(--muted);margin:8px 0}
.exportbar a{color:var(--muted);font-weight:550}
.qrbox{display:flex;gap:14px;align-items:flex-start;flex-wrap:wrap}
.qrimg{width:132px;height:132px;border-radius:12px;border:1px solid var(--border);
 background:#fff;padding:6px;object-fit:contain}
.kbd{font-size:.82rem;color:var(--faint)}
.authwrap{min-height:78vh;display:flex;align-items:center;justify-content:center}
.authcard{width:100%;max-width:370px;background:var(--surface);border:1px solid var(--border);
 border-radius:18px;padding:28px 26px;box-shadow:var(--shadow)}
.authcard .brand{justify-content:center;font-size:1.18rem;margin:0 0 4px}
.authcard h1{margin:2px 0 2px;font-size:1.2rem;text-align:center}
.authcard p.sub{text-align:center;color:var(--muted);font-size:.9rem;margin:2px 0 6px}
.authcard button{width:100%;margin-top:8px}
.err{color:var(--danger);font-weight:600;font-size:.9rem;margin:10px 0 0}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
"""


def _page(title: str, body: str) -> web.Response:
    doc = f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>{_esc(title)} · P2P Desk</title><style>{_STYLE}</style></head>
<body><div class=wrap>{body}</div>
<script>function _cp(b){{var p=b.parentNode.querySelector('.bankblk');if(!p)return;
navigator.clipboard.writeText(p.textContent).then(function(){{var t=b.textContent;
b.textContent='✅ Copied';setTimeout(function(){{b.textContent=t}},1500)}},function(){{}});}}</script>
</body></html>"""
    return web.Response(text=doc, content_type="text/html")


def _bank_lines(details: str | None) -> list[str]:
    return [ln.strip() for ln in (details or "").splitlines() if ln.strip()]


def _bank_block(details: str | None, copy: bool = True) -> str:
    """Bank details as a clean vertical block — one field per line, kept exactly
    as entered so the admin can select/paste it in one go. A Copy button lifts
    the whole block to the clipboard."""
    lines = _bank_lines(details)
    if not lines:
        return "<div class=bankwrap>—</div>"       # block-level so it always line-breaks
    pre = f"<pre class=bankblk>{_esc(chr(10).join(lines))}</pre>"
    if not copy:
        return f"<div class=bankwrap>{pre}</div>"
    return (f"<div class=bankwrap>{pre}"
            f"<button type=button class=copybtn onclick=\"_cp(this)\">📋 Copy</button></div>")


def _print_page(title: str, body: str) -> web.Response:
    """A clean, light, print-optimised page with a Save-as-PDF button — on a
    phone that's Share → Print → Save to Files, or the button's print dialog."""
    doc = f"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>{_esc(title)}</title><style>
body{{font-family:system-ui,sans-serif;max-width:820px;margin:0 auto;padding:16px;color:#111;background:#fff}}
h1{{font-size:1.35em;margin:.2em 0}} h2{{font-size:1.05em}}
table{{border-collapse:collapse;width:100%;font-size:.82em;margin-top:8px}}
th,td{{border:1px solid #bbb;padding:6px 8px;text-align:left;vertical-align:top;word-break:break-word}}
th{{background:#f0f0f0}}
.mono{{font-family:ui-monospace,monospace;word-break:break-all}}
.kv{{margin:3px 0}} .kv b{{display:inline-block;min-width:130px;color:#333;vertical-align:top}}
.bankblk{{display:inline-block;background:#f4f4f4;border:1px solid #ccc;border-radius:6px;
  padding:8px 11px;margin:0;font-family:ui-monospace,monospace;white-space:pre-wrap;word-break:break-word}}
.pbtn{{position:sticky;top:8px;background:#0b7a55;color:#fff;border:0;border-radius:8px;
  padding:11px 18px;font-size:1em;margin-bottom:14px;cursor:pointer}}
@media print{{.pbtn{{display:none}} a{{color:#000;text-decoration:none}}}}
</style></head><body>
<button class=pbtn onclick="window.print()">🖨 Save as PDF / Print</button>
{body}
<script>setTimeout(function(){{try{{window.print()}}catch(e){{}}}},500)</script>
</body></html>"""
    return web.Response(text=doc, content_type="text/html")


def _csv_safe(v) -> str:
    """Neutralise CSV formula injection — a user-typed cell (bank details, name)
    starting with = + - @ or a control char could run as a formula in Excel."""
    s = "" if v is None else str(v)
    return ("'" + s) if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


async def _export_rows(tab: str):
    """(order, user, card) tuples for a tab, or every order when tab == 'all'.
    Users and cards are batch-fetched (3 queries total, no per-row N+1) so even a
    full 'all' export can't flood the single shared SQLite connection."""
    from .models import Ticket, BankCard, User
    async with Session() as s:
        if tab == "all":
            orders = (await s.scalars(select(Order).order_by(Order.id.desc()))).all()
        else:
            ftab = tab if tab in TABS else "active"
            orders = (await s.scalars(select(Order).where(_tab_filter(ftab))
                                      .order_by(Order.id.desc()))).all()
        uids = list({o.user_id for o in orders})
        cids = list({o.bank_card_id for o in orders if o.bank_card_id})
        users = {u.id: u for u in (await s.scalars(
            select(User).where(User.id.in_(uids)))).all()} if uids else {}
        cards = {c.id: c for c in (await s.scalars(
            select(BankCard).where(BankCard.id.in_(cids)))).all()} if cids else {}
    return [(o, users.get(o.user_id), cards.get(o.bank_card_id)) for o in orders]


def _desk_toggle_btn(switch_on: bool, csrf: str, back: str) -> str:
    label = "🔴 Close desk now" if switch_on else "🟢 Open desk now"
    cls = "warn" if switch_on else ""
    return (f"<form method=post action=/desk/toggle style='display:inline'>"
            f"<input type=hidden name=csrf value='{csrf}'>"
            f"<input type=hidden name=back value='{_esc(back)}'>"
            f"<button class='{cls}'>{label}</button></form>")


# Order status → semantic badge class, so state reads at a glance in the panel.
_BADGE_CLASS = {
    OrderStatus.COMPLETED.value: "ok",
    OrderStatus.PENDING_PAYOUT.value: "warn",
    OrderStatus.DEPOSIT_RECEIVED.value: "info",
    OrderStatus.AWAITING_DEPOSIT.value: "muted",
    OrderStatus.REFUND_REQUESTED.value: "warn",
    OrderStatus.REFUNDED.value: "accent",
    OrderStatus.CANCELLED.value: "danger",
    OrderStatus.EXPIRED.value: "muted",
    OrderStatus.REFUND_REJECTED.value: "danger",
}


def _badge(status: str) -> str:
    cls = _BADGE_CLASS.get(status, "muted")
    label = status.replace("_", " ")
    return (f"<span class='badge {cls}'>{texts.STATUS_EMOJI.get(status, '•')} "
            f"{_esc(label)}</span>")


def _nav(active: str) -> str:
    def link(href, label, key):
        return f"<a href='{href}' class='{'on' if key == active else ''}'>{label}</a>"
    return ("<header class=appbar>"
            "<span class=brand><span class=dot></span>P2P Desk</span>"
            "<nav>" + link("/", "Orders", "orders")
            + link("/pay", "Manual pay", "pay")
            + link("/tickets", "Tickets", "tickets")
            + link("/users", "Users", "users")
            + link("/marketing", "Marketing", "marketing")
            + link("/messaging", "Messaging", "messaging")
            + link("/broadcast", "Broadcast", "broadcast")
            + link("/backups", "Backups", "backups")
            + link("/settings", "Settings", "settings")
            + "</nav><span class=sp></span>"
            "<a href='/logout' class=out>Logout</a></header>")


def _qr_card(net_key: str, label: str, current_addr: str,
             stored_png: bytes | None, stored_addr: str, csrf: str) -> str:
    """One network's QR manager: live preview, status badge, upload + remove."""
    if not current_addr:
        status = ("<span class='badge muted'>address not set</span>"
                  "<div class=muted style='margin-top:6px'>Set this network's address "
                  "above and Save first.</div>")
    elif stored_png and stored_addr == current_addr:
        status = ("<span class='badge ok'>✓ custom QR live</span>"
                  "<div class=muted style='margin-top:6px'>Customers scan your uploaded "
                  "QR for this network.</div>")
    elif stored_png:
        status = ("<span class='badge warn'>⚠ stale — not shown</span>"
                  "<div class=muted style='margin-top:6px'>This QR was uploaded for a "
                  "different address, so customers get an auto-generated QR instead. "
                  "Re-upload it for the current address to use it.</div>")
    else:
        status = ("<span class='badge muted'>auto-generated</span>"
                  "<div class=muted style='margin-top:6px'>No custom QR — customers get a "
                  "QR generated from the address above.</div>")
    preview_ok = bool(stored_png) or bool(current_addr and qr_png(current_addr))
    if preview_ok:
        # token_hex cache-buster so the preview refreshes right after an upload/remove
        img = (f"<img class=qrimg src='/qr/{net_key}.png?v={secrets.token_hex(3)}' "
               f"alt='{_esc(label)} deposit QR'>")
    else:
        img = ("<div class=qrimg style='display:flex;align-items:center;"
               "justify-content:center;color:var(--faint);font-size:.78rem;"
               "text-align:center;padding:8px'>No preview<br>available</div>")
    remove = ""
    if stored_png:
        remove = (f"<form method=post action=/settings/qr style='display:inline'>"
                  f"<input type=hidden name=csrf value='{_esc(csrf)}'>"
                  f"<input type=hidden name=net value='{net_key}'>"
                  f"<input type=hidden name=act value='remove'>"
                  f"<button class=ghost type=submit>Remove</button></form>")
    disabled = "" if current_addr else " disabled"
    upload = (f"<form method=post action=/settings/qr enctype=multipart/form-data>"
              f"<input type=hidden name=csrf value='{_esc(csrf)}'>"
              f"<input type=hidden name=net value='{net_key}'>"
              f"<label>Upload a QR image (PNG / JPG / WebP, max 1 MB)</label>"
              f"<input type=file name=qr accept='image/png,image/jpeg,image/webp'{disabled}>"
              f"<div class=row style='margin-top:8px'>"
              f"<button type=submit{disabled}>Upload QR</button>{remove}</div>"
              f"</form>")
    return (f"<div class=card><div class=row style='margin-bottom:10px'>"
            f"<b>{_esc(label)}</b></div>"
            f"<div class=qrbox><div>{img}</div>"
            f"<div style='flex:1;min-width:210px'>{status}"
            f"<div style='margin-top:12px'>{upload}</div></div></div></div>")


def _sniff_mime(raw: bytes) -> str | None:
    """Image type by magic bytes — so PNG/JPG/WebP uploads work even on a server
    without Pillow installed. Returns None for anything else (HEIC, PDF, …)."""
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


def _normalize_upload(raw: bytes) -> tuple[bytes, str] | None:
    """(image_bytes, mime) for an uploaded QR. With Pillow installed the image
    is re-encoded to PNG (also downsizing huge phone photos); without Pillow a
    real PNG/JPG/WebP is stored as-is — so the upload works either way."""
    try:
        from PIL import Image
    except Exception:
        mime = _sniff_mime(raw)
        return (raw, mime) if mime else None
    try:
        im = Image.open(io.BytesIO(raw))
        im.load()
        im = im.convert("RGB")
        im.thumbnail((900, 900))
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        return buf.getvalue(), "image/png"
    except Exception:
        # Pillow couldn't parse it (e.g. exotic format) — magic-byte fallback
        mime = _sniff_mime(raw)
        return (raw, mime) if mime else None


# ── routes ────────────────────────────────────────────────────────────────────

def _login_card(error: str = "") -> str:
    err = f"<p class=err>{_esc(error)}</p>" if error else ""
    return ("<div class=authwrap><div class=authcard>"
            "<div class='brand'><span class=dot></span>P2P Desk</div>"
            "<h1>Admin sign-in</h1>"
            "<p class=sub>Enter your panel password to continue.</p>"
            "<form method=post action=/login>"
            "<label>Password</label>"
            "<input type=password name=password autocomplete=current-password autofocus>"
            f"<button>Sign in</button></form>{err}</div></div>")


async def login_get(request: web.Request):
    return _page("Login", _login_card())


async def login_post(request: web.Request):
    ip = request.headers.get("X-Forwarded-For", request.remote or "?").split(",")[0].strip()
    count, first = _login_fails.get(ip, (0, time.time()))
    if count >= 5 and time.time() - first < 300:
        return _page("Login", _login_card("Too many attempts — try again in a few minutes."))
    data = await request.post()
    expected = await _panel_password()
    if expected and hmac.compare_digest(str(data.get("password", "")), expected):
        _login_fails.pop(ip, None)
        issued = int(time.time())
        resp = web.HTTPFound("/")
        secure = request.secure or \
            request.headers.get("X-Forwarded-Proto", "").lower() == "https"
        resp.set_cookie(COOKIE, await _sign(issued), httponly=True, samesite="Strict",
                        secure=secure, max_age=SESSION_TTL)
        return resp
    _login_fails[ip] = (count + 1, first if count else time.time())
    return _page("Login", _login_card("Wrong password."))


async def logout(request: web.Request):
    resp = web.HTTPFound("/login")
    resp.del_cookie(COOKIE)
    return resp


@_authed
async def dashboard(request: web.Request):
    tab = request.query.get("tab", "active")
    if tab not in TABS:
        tab = "active"
    label = TABS[tab][0]
    async with Session() as s:
        is_open, reason = await desk_state(s)
        switch_on = await get_desk_open(s)
        q = select(Order).where(_tab_filter(tab))
        q = q.order_by(Order.id.desc()).limit(30) if tab == "done" else q.order_by(Order.id)
        orders = (await s.scalars(q)).all()
    toggle = _desk_toggle_btn(switch_on, await _csrf_for(request), "/")
    if is_open:
        desk_banner = ("<div class='banner ok'><div class=row>"
                       "<span>🟢 <b>Desk is OPEN</b> — taking new sell orders.</span>"
                       f"<span class=sp></span>{toggle}</div></div>")
    else:
        desk_banner = ("<div class='banner danger'><div class=row>"
                       f"<span>🔴 <b>Desk is CLOSED</b> — {_esc(reason)}.</span>"
                       f"<span class=sp></span>{toggle}</div></div>")
    tabs_html = "<div class=tabs>" + "".join(
        f"<a class='{'on' if k == tab else ''}' href='/?tab={k}'>{lbl}</a>"
        for k, (lbl, _) in TABS.items()) + "</div>"
    export = (f"<div class='exportbar row'>Export:&nbsp;"
              f"<a href='/orders/print?tab={tab}'>📄 this tab (PDF)</a> · "
              f"<a href='/orders.csv?tab={tab}'>⬇️ CSV</a> · "
              f"<a href='/orders/print?tab=all'>📄 all (PDF)</a> · "
              f"<a href='/orders.csv?tab=all'>⬇️ all CSV</a></div>")
    rows = []
    for o in orders:
        src = ("<span class='badge accent'>🌐 web</span>" if o.user_id < 0 else "")
        rows.append(
            f"<a class='card link' href='/order/{o.id}' "
            f"style='display:block;color:inherit'>"
            f"<div class=row><b>{texts.tag(o.id)}</b>{src}"
            f"<span class=sp></span>{_badge(o.status)}</div>"
            f"<div class=row style='margin-top:8px'>"
            f"<span class=amt>{o.usd_amount:g}<span class=muted "
            f"style='font-weight:500;font-size:.8rem'> USDT</span></span>"
            f"<span class=arrow>→</span>"
            f"<span class=amt>₹{o.inr_amount:,.2f}</span>"
            f"<span class=sp></span>"
            f"<span class=muted>{_esc(SERVICES.get(o.service, o.service))}</span></div>"
            f"</a>")
    body = (_nav("orders") + desk_banner + f"<h1>Orders — {_esc(label)} "
            f"<span class=faint style='font-weight:500'>({len(orders)})</span></h1>"
            + tabs_html + export + ("".join(rows)
            or "<div class=card><span class=muted>Nothing here yet.</span></div>"))
    return _page("Orders", body)


@_authed
async def order_detail(request: web.Request):
    from .models import Ticket, BankCard, User
    oid = int(request.match_info["id"])
    async with Session() as s:
        order = await s.get(Order, oid)
        if order is None:
            return _page("Order", _nav("orders") + "<p>Order not found.</p>")
        user = await s.get(User, order.user_id)
        card = await s.get(BankCard, order.bank_card_id) if order.bank_card_id else None
    csrf = await _csrf_for(request)
    uname = f"@{_esc(user.username)}" if user and user.username else "—"
    if order.user_id < 0:
        uname = "<span class='badge accent'>🌐 website customer</span>"
    msg = request.query.get("msg", "")
    banner = (f"<div class='banner ok'>{_esc(msg)}</div>" if msg else "")
    lines = [
        _nav("orders"),
        f"<h1>{texts.tag(order.id)}</h1>",
        banner,
        f"<div class=card><div class=row style='margin-bottom:8px'>{_badge(order.status)}</div>"
        f"<b>Sell:</b> {order.usd_amount:g}$ USDT via "
        f"{_esc(SERVICES.get(order.service, order.service))} @ 1$/₹{order.rate_inr:g}<br>"
        f"<b>Pay out:</b> ₹{order.inr_amount:,.2f}<br>"
        f"<b>User:</b> {_esc(user.first_name) if user else '?'} {uname} "
        f"(id <code>{order.user_id}</code>)<br>"
        f"<b>Bank:</b> {_bank_block(card.details if card else None)}"
        f"<b>Deposit addr:</b> <code>{_esc(order.deposit_address)}</code><br>"
        f"<b>TX:</b> <code>{_esc(order.txid) or '—'}</code><br>"
        + (f"<b>↩️ Refund TXID:</b> <code>{_esc(order.refund_txid)}</code><br>"
           f"<a href='https://tronscan.org/#/transaction/{_esc(order.refund_txid)}' "
           f"target=_blank>🔎 Verify on Tronscan</a><br>"
           f"<span style='color:#f0b429'>⚠️ Refund ONLY to the address this TX came "
           f"FROM. Never a typed address.</span><br>"
           if order.refund_txid else "")
        + "</div>",
    ]
    act = "<div class=row>"
    if order.status == OrderStatus.PENDING_PAYOUT.value:
        act += (f"<form method=post action='/order/{order.id}/done'>"
                f"<input type=hidden name=csrf value='{csrf}'>"
                f"<button>✅ Done — INR sent</button></form>")
    if order.status in (OrderStatus.AWAITING_DEPOSIT.value, OrderStatus.EXPIRED.value):
        act += (f"<form method=post action='/order/{order.id}/confirm' class=row>"
                f"<input type=hidden name=csrf value='{csrf}'>"
                f"<input name=txid placeholder='tx hash (optional)' style='width:auto'>"
                f"<button class=warn>📥 Confirm deposit</button></form>"
                f"<form method=post action='/order/{order.id}/decline' "
                f"onsubmit=\"return confirm('Decline this order? Use only when no "
                f"deposit was received.')\">"
                f"<input type=hidden name=csrf value='{csrf}'>"
                f"<button class=danger>🚫 Decline (no deposit)</button></form>")
    if order.status == OrderStatus.REFUND_REQUESTED.value:
        act += (f"<form method=post action='/order/{order.id}/refund'>"
                f"<input type=hidden name=csrf value='{csrf}'>"
                f"<button>💸 Refund sent (to sender)</button></form>"
                f"<form method=post action='/order/{order.id}/reject'>"
                f"<input type=hidden name=csrf value='{csrf}'>"
                f"<button class=danger>🚫 Reject (fake / no deposit)</button></form>")
    act += "</div>"
    lines.append(act)
    lines.append(f"<p style='margin-top:10px'><a href='/order/{order.id}/print'>"
                 f"📄 Save this order as PDF</a></p>")
    return _page(f"Order {order.id}", "".join(lines))


@_authed
async def order_print(request: web.Request):
    from .models import Ticket, BankCard, User
    oid = int(request.match_info["id"])
    async with Session() as s:
        order = await s.get(Order, oid)
        if order is None:
            return _print_page("Order", "<p>Order not found.</p>")
        user = await s.get(User, order.user_id)
        card = await s.get(BankCard, order.bank_card_id) if order.bank_card_id else None
    uname = f"@{_esc(user.username)}" if user and user.username else "—"
    def kv(k, v):
        return f"<div class=kv><b>{k}</b>{v}</div>"
    body = (
        f"<h1>{texts.tag(order.id)}</h1>"
        + kv("Status", _esc(order.status))
        + kv("Sell", f"{order.usd_amount:g}$ USDT via "
             f"{_esc(SERVICES.get(order.service, order.service))} @ 1$/₹{order.rate_inr:g}")
        + kv("Payout", f"₹{order.inr_amount:,.2f}")
        + kv("User", f"{_esc(user.first_name) if user else '?'} {uname} "
             f"(id <span class=mono>{order.user_id}</span>)")
        + kv("Bank", _bank_block(card.details if card else None, copy=False))
        + kv("Deposit addr", f"<span class=mono>{_esc(order.deposit_address)}</span>")
        + kv("TXID", f"<span class=mono>{_esc(order.txid) or '—'}</span>")
        + (kv("Refund TXID", f"<span class=mono>{_esc(order.refund_txid)}</span>")
           if order.refund_txid else "")
        + kv("Created", _esc(str(order.created_at)))
    )
    return _print_page(f"Order {texts.tag(order.id)}", body)


def _tab_arg(request: web.Request) -> str:
    tab = request.query.get("tab", "all")
    return tab if tab in TABS or tab == "all" else "all"


@_authed
async def orders_print(request: web.Request):
    tab = _tab_arg(request)
    rows = await _export_rows(tab)
    CAP = 500          # a printable PDF table stays sane; CSV carries the full set
    cap_note = ""
    if len(rows) > CAP:
        cap_note = (f"<p>Showing the latest {CAP} of {len(rows)}. "
                    f"<a href='/orders.csv?tab={_esc(tab)}'>Download all {len(rows)} as CSV</a>.</p>")
        rows = rows[:CAP]
    head = ("<tr><th>Order</th><th>Status</th><th>USDT → ₹</th><th>User</th>"
            "<th>Bank</th><th>TXID</th><th>Created</th></tr>")
    trs = []
    for o, user, card in rows:
        bank = " · ".join(ln.strip() for ln in (card.details.splitlines() if card else [])
                          if ln.strip()) or "—"
        who = (f"{_esc(user.first_name) if user else '?'} "
               f"(@{_esc(user.username)})" if user and user.username
               else _esc(user.first_name) if user else "?")
        trs.append(
            f"<tr><td>{texts.tag(o.id)}</td><td>{_esc(o.status)}</td>"
            f"<td>{o.usd_amount:g}$ → ₹{o.inr_amount:,.2f}<br>"
            f"<span class=muted>@1$/₹{o.rate_inr:g} {_esc(SERVICES.get(o.service, o.service))}</span></td>"
            f"<td>{who}<br><span class=mono>{o.user_id}</span></td>"
            f"<td class=mono>{_esc(bank)}</td>"
            f"<td class=mono>{_esc(o.txid) if o.txid and o.txid != 'manual' else '—'}</td>"
            f"<td>{_esc(str(o.created_at)[:19])}</td></tr>")
    label = "All orders" if tab == "all" else TABS[tab][0]
    body = (f"<h1>{label} — {len(rows)} order(s)</h1>{cap_note}"
            f"<table>{head}{''.join(trs) or '<tr><td colspan=7>None</td></tr>'}</table>")
    return _print_page(f"Orders — {label}", body)


@_authed
async def orders_csv(request: web.Request):
    tab = _tab_arg(request)
    rows = await _export_rows(tab)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Order", "Status", "Side", "Service", "USDT", "Rate INR/$",
                "Payout INR", "User ID", "Username", "Name", "Bank details",
                "Deposit address", "TXID", "Created"])
    for o, user, card in rows:
        bank = " | ".join(ln.strip() for ln in (card.details.splitlines() if card else [])
                          if ln.strip())
        w.writerow([_csv_safe(x) for x in (
            texts.tag(o.id), o.status, o.side, o.service, f"{o.usd_amount:g}",
            f"{o.rate_inr:g}", f"{o.inr_amount:.2f}", o.user_id,
            (user.username if user else ""), (user.first_name if user else ""),
            bank, o.deposit_address, o.txid or "", str(o.created_at))])
    # UTF-8 BOM + charset so Excel reads names / regional-script bank text correctly
    return web.Response(
        body=("﻿" + buf.getvalue()).encode("utf-8"),
        content_type="text/csv", charset="utf-8",
        headers={"Content-Disposition": f'attachment; filename="orders-{tab}.csv"'})


def _order_action(fn, needs_txid=False):
    @_authed
    async def handler(request: web.Request):
        oid = int(request.match_info["id"])
        data = await request.post()
        if not await _check_csrf(request, data):
            return _page("Error", _nav("orders") + "<p>Invalid CSRF token.</p>")
        if request.app["bot"] is None:
            raise web.HTTPFound(f"/order/{oid}?msg="
                                + html.escape("Bot isn't running yet — set the bot "
                                              "token in Settings first."))
        if needs_txid:
            txid = str(data.get("txid", "")).strip() or "manual"
            ok, msg = await fn(request.app["bot"], oid, txid)
        else:
            ok, msg = await fn(request.app["bot"], oid)
        raise web.HTTPFound(f"/order/{oid}?msg={html.escape(msg)}")
    return handler


@_authed
async def desk_toggle(request: web.Request):
    data = await request.post()
    if not await _check_csrf(request, data):
        return _page("Error", _nav("orders") + "<p>Invalid CSRF token.</p>")
    async with Session() as s:
        cur = await get_desk_open(s)
        await set_setting(s, "desk_open", "0" if cur else "1")
    back = str(data.get("back", "/"))
    raise web.HTTPFound(back if back in ("/", "/settings") else "/")


@_authed
async def broadcast_get(request: web.Request):
    async with Session() as s:
        n = await s.scalar(select(func.count()).select_from(User)
                           .where(User.banned.is_(False),
                                  User.id > 0))  # skip web (negative-id) users
        no_email = await s.scalar(select(func.count()).select_from(User)
                                  .where(User.banned.is_(False), User.id > 0,
                                         or_(User.email.is_(None), User.email == "",
                                             User.email_verified.is_(False))))
    csrf = await _csrf_for(request)
    msg = request.query.get("msg", "")
    banner = (f"<div class='banner ok'>{_esc(msg)}</div>" if msg else "")
    body = (_nav("broadcast") + "<h1>📢 Broadcast</h1>" + banner
            + f"<p class=muted>Sends a message to all <b>{n or 0}</b> bot users "
            "(skipping anyone who blocked the bot).</p>"
            "<form method=post action=/broadcast><div class=card>"
            f"<input type=hidden name=csrf value='{csrf}'>"
            "<label>Message</label>"
            "<textarea name=text rows=5></textarea>"
            "<p class='muted small' style='margin:4px 0 0'>Formatting: "
            "&lt;b&gt;bold&lt;/b&gt; &lt;i&gt;italic&lt;/i&gt; "
            "&lt;code&gt;mono&lt;/code&gt; — links paste as plain URLs "
            "(Telegram makes them clickable).</p>"
            "<label style='display:flex;gap:8px;align-items:center;margin:10px 0'>"
            "<input type=checkbox name=to_proof value='1' style='width:auto'> "
            "Also post to the proof channel</label>"
            "<div class=row><button>Send broadcast</button></div>"
            "</div></form>"
            "<h2>📧 Ask for emails</h2>"
            "<form method=post action=/broadcast/emails><div class=card>"
            f"<input type=hidden name=csrf value='{csrf}'>"
            f"<p class=muted style='margin-top:0'>DMs the <b>{no_email or 0}</b> bot "
            "users who haven't added a verified email yet: get order confirmations "
            "and payout receipts by email with <code>/email</code>. One tap to "
            "dismiss; an address already verified on the website activates "
            "instantly with no code.</p>"
            "<div class=row><button>Send email request</button></div>"
            "</div></form>")
    return _page("Broadcast", body)


@_authed
async def broadcast_post(request: web.Request):
    data = await request.post()
    if not await _check_csrf(request, data):
        return _page("Error", _nav("broadcast") + "<p>Invalid CSRF token.</p>")
    text = str(data.get("text", "")).strip()
    if not text:
        raise web.HTTPFound("/broadcast?msg=" + html.escape("Message was empty."))
    if request.app["bot"] is None:
        raise web.HTTPFound("/broadcast?msg="
                            + html.escape("Bot isn't running yet — set the token first."))
    to_proof = bool(data.get("to_proof"))
    async with Session() as s:
        n = await s.scalar(select(func.count()).select_from(User)
                           .where(User.banned.is_(False),
                                  User.id > 0))  # skip web (negative-id) users
    launch_broadcast(request.app["bot"], compose_announcement(text), to_proof)
    raise web.HTTPFound("/broadcast?msg=" + html.escape(
        f"Broadcast started to {n or 0} users — you'll get a summary in Telegram."))


@_authed
async def broadcast_emails_post(request: web.Request):
    data = await request.post()
    if not await _check_csrf(request, data):
        return _page("Error", _nav("broadcast") + "<p>Invalid CSRF token.</p>")
    if request.app["bot"] is None:
        raise web.HTTPFound("/broadcast?msg="
                            + html.escape("Bot isn't running yet — set the token first."))
    from .actions import launch_email_nudge
    launch_email_nudge(request.app["bot"])
    raise web.HTTPFound("/broadcast?msg=" + html.escape(
        "Email request started — you'll get a sent/failed summary in Telegram."))


@_authed
async def pay_get(request: web.Request):
    async with Session() as s:
        rates = await get_rates(s)
    csrf = await _csrf_for(request)
    msg = request.query.get("msg", "")
    banner = (f"<div class='banner ok'>{_esc(msg)}</div>" if msg else "")
    if rates:
        opts = "".join(f"<option value='{_esc(k)}'>{_esc(SERVICES.get(k, k))} "
                       f"— ₹{v:g}/$</option>" for k, v in rates.items())
        method_field = f"<label>Method</label><select name=method>{opts}</select>"
        submit = "<div class=row><button>Record payment &amp; notify customer</button></div>"
    else:
        method_field = ("<p class=muted>⚠️ No method has a live rate yet — set one in "
                        "⚙️ Settings first, then come back.</p>")
        submit = ""
    body = (_nav("pay") + "<h1>💸 Manual pay</h1>" + banner
            + "<p class=muted>Record a settlement done outside the bot. The bot "
            "computes ₹ from the method's live rate, creates the order in the "
            "<b>Active</b> tab and DMs the customer it's confirmed. Tap <b>Done</b> "
            "on the order (here or in Telegram) once you've paid, to send the "
            "receipt + channel proof — so manual payments record just like auto ones.</p>"
            "<form method=post action=/pay><div class=card>"
            f"<input type=hidden name=csrf value='{csrf}'>"
            "<label>Customer Telegram ID</label>"
            "<input name=user_id inputmode=numeric placeholder='e.g. 123456789'>"
            "<label>Amount (USDT $)</label>"
            "<input name=usd inputmode=decimal placeholder='e.g. 100'>"
            + method_field + submit + "</div></form>"
            "<p class=muted>The customer can get their ID by sending the bot "
            "<code>/whoami</code>; it's also shown on every order card.</p>")
    return _page("Manual pay", body)


@_authed
async def pay_post(request: web.Request):
    data = await request.post()
    if not await _check_csrf(request, data):
        return _page("Error", _nav("pay") + "<p>Invalid CSRF token.</p>")
    if request.app["bot"] is None:
        raise web.HTTPFound("/pay?msg=" + quote(
            "Bot isn't running yet — set the bot token in Settings first."))
    uid_raw = str(data.get("user_id", "")).strip()
    if not uid_raw.isdigit():
        raise web.HTTPFound("/pay?msg=" + quote(
            "Enter a numeric customer Telegram ID (they can send /whoami)."))
    try:
        usd = float(str(data.get("usd", "")).strip())
    except ValueError:
        raise web.HTTPFound("/pay?msg=" + quote("Amount must be a number."))
    method = str(data.get("method", "")).upper()
    ok, msg = await record_manual_order(request.app["bot"], int(uid_raw), usd, method)
    # url-encode: the message contains '#ORD…' and '&', which break a query string
    raise web.HTTPFound("/pay?msg=" + quote(msg))


@_authed
async def settings_get(request: web.Request):
    async with Session() as s:
        is_open, reason = await desk_state(s)
        desk_switch = (await get_setting(s, "desk_open")) != "0"
        rates = {k: (await get_setting(s, f"rate_{k}") or "") for k in SERVICES}
        lims = {k: ((await get_setting(s, f"limit_min_{k}") or ""),
                    (await get_setting(s, f"limit_max_{k}") or "")) for k in SERVICES}
        addr = await get_deposit_address(s) or ""
        ttl_min = await get_deposit_ttl(s)
        bep20 = await get_bep20_address(s) or ""
        bsc_key_set = bool(await get_bscscan_key(s))   # honors /setbsckey off over an env key
        support = await get_setting(s, "support") or ""
        support_email = await get_support_email(s)
        whatsapp = await get_whatsapp(s)
        rate_auto = (await get_setting(s, "rate_email_auto")) != "0"
        seo_email = await get_setting(s, "seo_report_email")
        if seo_email is None:                     # unset ≠ deliberately blank
            seo_email = "naveenvemareddy@gmail.com"
        instant_credit = (await get_setting(s, "instant_credit")) == "1"
        admin_ids = await get_setting(s, "admin_ids")
        admin_ids = admin_ids if admin_ids is not None else settings.admin_ids
        admin_chat = await get_setting(s, "admin_chat_id") or ""
        proof = await get_setting(s, "proof_channel") or ""
        token_set = bool((await get_setting(s, "bot_token")) or settings.bot_token)
        qr_trc_png, qr_trc_addr, _ = await get_network_qr_raw(s, "TRC20")
        qr_bep_png, qr_bep_addr, _ = await get_network_qr_raw(s, "BEP20")
    csrf = await _csrf_for(request)
    msg = request.query.get("msg", "")
    msg_banner = (f"<div class='banner {'ok' if msg.startswith('✅') else 'warn'}'>"
                  f"{_esc(msg)}</div>" if msg else "")
    rate_fields = "".join(
        f"<div class=card><b>{_esc(SERVICES[k])}</b>"
        f"<label>Rate (₹/$, blank hides the service)</label>"
        f"<input name='rate_{k}' value='{_esc(rates[k])}'>"
        f"<div class=row style='gap:12px'>"
        f"<div style='flex:1'><label>Min $ (blank = default "
        f"{settings.min_usd:g})</label>"
        f"<input name='limit_min_{k}' value='{_esc(lims[k][0])}'></div>"
        f"<div style='flex:1'><label>Max $ (blank = default "
        f"{settings.max_usd:g})</label>"
        f"<input name='limit_max_{k}' value='{_esc(lims[k][1])}'></div>"
        f"</div></div>" for k in SERVICES)
    desk_banner_cls = "ok" if is_open else "danger"
    status_line = ("🟢 <b>Desk is OPEN</b>" if is_open
                   else f"🔴 <b>Desk is CLOSED</b> — {_esc(reason)}")
    tg_first = next((h.lstrip("@") for h in support.split() if h.startswith("@")), "")
    desk_toggle_html = _desk_toggle_btn(desk_switch, csrf, "/settings")
    # QR manager lives outside the main form (it has its own multipart upload forms)
    qr_cards = _qr_card("TRC20", "🔷 TRC20 (TRON)", addr, qr_trc_png, qr_trc_addr, csrf)
    if bep20:
        qr_cards += _qr_card("BEP20", "🟡 BEP20 (BSC)", bep20, qr_bep_png, qr_bep_addr, csrf)
    else:
        qr_cards += ("<div class=card><span class=muted>Set a BEP20 address above "
                     "(and Save) to add a QR for the BSC network.</span></div>")
    body = (_nav("settings") + "<h1>Settings</h1>" + msg_banner
            + f"<div class='banner {desk_banner_cls}'><div class=row>"
            f"<span>{status_line}</span><span class=sp></span>{desk_toggle_html}</div>"
            "<div class=muted style='margin-top:8px'>Toggles instantly — no Save needed. "
            "The desk also needs a deposit address and at least one rate below.</div></div>"
            "<form method=post action=/settings>"
            f"<input type=hidden name=csrf value='{csrf}'>"
            "<h2>Rates</h2>" + rate_fields
            + "<h2>Deposit &amp; payout</h2><div class=card>"
            "<label>⏳ Deposit window — minutes a quote stays live before it expires</label>"
            f"<input name=deposit_ttl inputmode=numeric value='{_esc(ttl_min)}'>"
            "<label style='display:flex;gap:10px;align-items:flex-start;margin-top:14px'>"
            f"<input type=checkbox name=instant_credit value=1 style='width:auto;margin-top:3px'"
            f"{' checked' if instant_credit else ''}>"
            "<span>⚡ <b>Instant credit</b> — credit a deposit the moment it's "
            "detected on-chain (already mined, ~1 min before final confirmation) "
            "instead of waiting for confirmation. Faster payouts, but you accept "
            "the small risk of a rare chain reversal in that window. Unique-cents "
            "keeps matching exact.</span></label>"
            "<label>🔷 TRC20 (TRON) deposit address</label>"
            f"<input name=addr value='{_esc(addr)}'>"
            "<label>🟡 BEP20 (BSC) deposit address — 0x… (blank = off)</label>"
            f"<input name=addr_bep20 value='{_esc(bep20)}'>"
            "<label>BscScan / Etherscan API key (no longer needed — BSC reads "
            f"free public RPC nodes directly{'; a key is stored' if bsc_key_set else ''})</label>"
            "<input type=password name=bscscan_key autocomplete=off placeholder='••••••'>"
            "</div>"

            # ── side-by-side: everything Telegram-bot vs everything website ──
            "<div class=duo style='margin-top:18px'>"

            "<div><h2>🤖 Bot (Telegram) settings</h2>"
            "<div class=card><b>Admins &amp; alerts</b>"
            "<p class='muted small' style='margin:4px 0 0'>Who controls the desk "
            "and where new-order alerts land.</p>"
            "<label>Admin Telegram IDs (space/comma-separated)</label>"
            f"<input name=admin_ids value='{_esc(admin_ids)}'>"
            "<label>Admin group chat id (optional, -100…; blank = DM each admin)</label>"
            f"<input name=admin_chat value='{_esc(admin_chat)}'>"
            "<label>Proof channel (@channel or -100… id, blank to disable)</label>"
            f"<input name=proof value='{_esc(proof)}'>"
            "<p class='muted small' style='margin:6px 0 0'>Completed deals post "
            "there anonymised — never a TXID or customer detail.</p></div>"
            "<div class=card><b>Bot token</b>"
            f"<p class=muted style='margin:4px 0 0'>{'A token is set.' if token_set else '⚠️ No token set.'} "
            "Changing it restarts the bot.</p>"
            "<label>New bot token (leave blank to keep current)</label>"
            "<input type=password name=bot_token autocomplete=off placeholder='••••••'>"
            "</div></div>"

            "<div><h2>🌐 Website settings</h2>"
            "<div class=card><b>Support buttons</b>"
            "<p class='muted small' style='margin:4px 0 0'>Floating contact buttons, "
            "bottom-right of every page — each shows only when filled in.</p>"
            "<label>✈️ Telegram support link (@username — also used in bot messages)</label>"
            f"<input name=support value='{_esc(support)}' placeholder='@yourdesk'>"
            + (f"<p class='muted small' style='margin:4px 0 0'>Button opens "
               f"https://t.me/{_esc(tg_first)}</p>" if tg_first else
               "<p class='muted small' style='margin:4px 0 0'>Add a @username to "
               "show the Telegram button.</p>")
            + "<label>💬 WhatsApp number (with country code; blank = hidden)</label>"
            f"<input name=whatsapp value='{_esc(whatsapp)}' placeholder='+91 98765 43210'>"
            "<label>✉️ Support email (highlighted across the site; blank = hidden)</label>"
            f"<input name=support_email type=email value='{_esc(support_email)}' "
            "placeholder='support@yourdomain.com'></div>"
            "<div class=card><b>Customer emails</b>"
            "<p class='muted small' style='margin:4px 0 0'>Sends through the SMTP "
            "configured in the ✉️ Messaging tab.</p>"
            "<label style='display:flex;gap:10px;align-items:flex-start;margin-top:12px'>"
            f"<input type=checkbox name=rate_email_auto value=1 style='width:auto;margin-top:3px'"
            f"{' checked' if rate_auto else ''}>"
            "<span>📈 <b>Auto rate updates</b> — email subscribers whenever rates "
            "change (all live rates in one box; identical rates never re-send, "
            "max one email per 30 min).</span></label>"
            "<p class='muted small' style='margin:10px 0 0'>Order confirmations and "
            "payment receipts email signed-up customers automatically.</p>"
            "<label>📊 Daily SEO report email (09:00 IST; blank = off)</label>"
            f"<input name=seo_report_email type=email value='{_esc(seo_email)}' "
            "placeholder='you@gmail.com'>"
            "<p class='muted small' style='margin:4px 0 0'>A morning summary of "
            "the onsite SEO: articles live, keyword coverage, the next target "
            "and one off-page action for the day.</p>"
            "</div></div>"

            "</div>"
            "<h2>Panel password</h2><div class=card>"
            "<p class=muted style='margin-top:0'>The panel is reachable from any device, "
            "so make this long — a 4-word phrase plus numbers is ideal.</p>"
            "<label>New panel password (blank = keep current)</label>"
            "<input type=password name=panel_password autocomplete=new-password "
            "placeholder='••••••'>"
            "</div><div class=row style='margin-top:14px'><button>Save settings</button></div>"
            "</form>"
            "<h2>Deposit QR codes</h2>"
            "<p class=muted>Customers see this QR on the deposit screen. Leave it "
            "auto-generated (it always matches the address), or upload your own — an "
            "uploaded QR is shown only while it still matches the saved address, so "
            "changing an address never leaves a wrong QR live.</p>"
            + qr_cards)
    return _page("Settings", body)


@_authed
async def settings_post(request: web.Request):
    data = await request.post()
    if not await _check_csrf(request, data):
        return _page("Error", _nav("settings") + "<p>Invalid CSRF token.</p>")
    errors: list[str] = []
    restart = False
    async with Session() as s:
        old_rates = await get_rates(s)   # pre-save snapshot for the rate email
        for k in SERVICES:
            raw = str(data.get(f"rate_{k}", "")).strip()
            if raw == "":
                await set_setting(s, f"rate_{k}", "0")
            else:
                try:
                    val = float(raw)
                    if val < 0 or val > 100_000:
                        raise ValueError
                    await set_setting(s, f"rate_{k}", str(val))
                except ValueError:
                    errors.append(f"{SERVICES[k]} rate invalid")

            # per-service min/max limits (blank = fall back to env defaults)
            lo_raw = str(data.get(f"limit_min_{k}", "")).strip()
            hi_raw = str(data.get(f"limit_max_{k}", "")).strip()
            lo_val = hi_val = None
            try:
                if lo_raw:
                    lo_val = float(lo_raw)
                    if lo_val <= 0:
                        raise ValueError
                if hi_raw:
                    hi_val = float(hi_raw)
                    if hi_val <= 0:
                        raise ValueError
                if lo_val is not None and hi_val is not None and lo_val > hi_val:
                    errors.append(f"{SERVICES[k]}: min is above max")
                    continue
                await set_setting(s, f"limit_min_{k}", str(lo_val) if lo_val else "")
                await set_setting(s, f"limit_max_{k}", str(hi_val) if hi_val else "")
            except ValueError:
                errors.append(f"{SERVICES[k]} min/max invalid")

        ttl_raw = str(data.get("deposit_ttl", "")).strip()
        if ttl_raw:
            if ttl_raw.isdigit() and 2 <= int(ttl_raw) <= 1440:
                await set_setting(s, "deposit_ttl_min", str(int(ttl_raw)))
            else:
                errors.append("deposit window must be a number of minutes (2–1440)")

        # checkbox: present in POST only when ticked
        await set_setting(s, "instant_credit",
                          "1" if data.get("instant_credit") else "0")

        addr = str(data.get("addr", "")).strip()
        if addr:
            if is_trc20(addr):
                if addr != (await get_deposit_address(s) or ""):
                    await set_setting(s, "addr_trc20", addr)
                    now_ms = int(time.time() * 1000)
                    await set_setting(s, f"addr_since:{addr}", str(now_ms))
                    await set_setting(s, f"bootstrapped:{addr}", "1")
            else:
                errors.append("deposit address is not a valid TRC20 address")

        bep = str(data.get("addr_bep20", "")).strip()
        if bep == "":
            await set_setting(s, "addr_bep20", "")          # blank = turn BEP20 off
        elif is_bep20(bep):
            if bep != (await get_bep20_address(s) or ""):
                await set_setting(s, "addr_bep20", bep)
                await set_setting(s, f"bsc_since:{bep}", str(int(time.time())))
        else:
            errors.append("BEP20 address is not a valid 0x… address")

        bkey = str(data.get("bscscan_key", "")).strip()
        if bkey:
            await set_setting(s, "bscscan_key", bkey)

        support = str(data.get("support", "")).strip()
        if support == "":
            await set_setting(s, "support", "")   # blank = hide the button
        else:
            handles = support.split()
            if all(h.startswith("@") and len(h) >= 5 for h in handles):
                await set_setting(s, "support", " ".join(handles))
            else:
                errors.append("support handles must each start with @")

        semail = str(data.get("support_email", "")).strip()
        if semail == "" or ("@" in semail and "." in semail.split("@")[-1]
                            and " " not in semail and len(semail) <= 120):
            await set_setting(s, "support_email", semail)
        else:
            errors.append("support email looks invalid")

        seo_mail = str(data.get("seo_report_email", "")).strip()
        if seo_mail == "" or ("@" in seo_mail and "." in seo_mail.split("@")[-1]
                              and " " not in seo_mail and len(seo_mail) <= 120):
            await set_setting(s, "seo_report_email", seo_mail)
        else:
            errors.append("SEO report email looks invalid")

        wa_raw = str(data.get("whatsapp", "")).strip()
        wa_digits = "".join(ch for ch in wa_raw if "0" <= ch <= "9")
        if wa_raw == "":
            await set_setting(s, "support_whatsapp", "")   # blank = hide button
        elif 8 <= len(wa_digits) <= 15:    # same bound as /setwhatsapp in chat
            await set_setting(s, "support_whatsapp", wa_digits)
        else:
            errors.append("WhatsApp number needs the country code, e.g. +919876543210")

        # checkbox: present in POST only when ticked
        await set_setting(s, "rate_email_auto",
                          "1" if data.get("rate_email_auto") else "0")

        proof = str(data.get("proof", "")).strip()
        await set_setting(s, "proof_channel",
                          proof if (proof.startswith("@") or proof.lstrip("-").isdigit())
                          else "")

        aids = str(data.get("admin_ids", "")).replace(",", " ").split()
        if all(x.isdigit() for x in aids):
            await set_setting(s, "admin_ids", " ".join(aids))
        else:
            errors.append("admin IDs must be numeric")

        chat = str(data.get("admin_chat", "")).strip()
        if chat == "" or chat.lstrip("-").isdigit():
            await set_setting(s, "admin_chat_id", chat)
        else:
            errors.append("admin chat id must be numeric")

        new_pw = str(data.get("panel_password", "")).strip()
        if new_pw:
            if len(new_pw) >= 6:
                await set_setting(s, "panel_password", new_pw)
            else:
                errors.append("panel password too short (use at least 6, longer is better)")

        token = str(data.get("bot_token", "")).strip()
        if token:
            if ":" in token and token.split(":", 1)[0].isdigit():
                await set_setting(s, "bot_token", token)
                restart = True
            else:
                errors.append("bot token format looks wrong")

    # rates may have changed — kick the auto rate email (dedupes + rate-limits
    # itself). Runs even when OTHER fields had validation errors: each rate was
    # already committed above, so the change is live either way.
    try:
        sender.spawn_rate_blast(request.app.get("bot"), prev_rates=old_rates)
    except Exception:
        log.exception("rate email check failed")
    if errors:
        return _page("Settings", _nav("settings") + "<h1>Settings</h1>"
                     + "<div class='banner danger'><b>Not saved:</b> "
                     + _esc("; ".join(errors)) + "</div>"
                     "<p><a href=/settings>← Back to settings</a></p>")
    # keep the website's floating-button support email in sync with this save
    try:
        from .website import load_support_cache
        await load_support_cache()
    except Exception:
        log.exception("support cache refresh failed")
    if restart:
        # write is committed; exit so systemd restarts with the new token
        asyncio.get_running_loop().call_later(1.0, os._exit, 0)
        return _page("Restarting", "<h1>Restarting the bot…</h1>"
                     "<div class='banner ok'>✅ Saved. The new bot token is applied on "
                     "restart — this page will be back in a few seconds.</div>"
                     "<p><a href=/settings>← Back to settings</a></p>")
    raise web.HTTPFound("/settings?msg=" + quote("✅ Settings saved."))


@_authed
async def qr_preview(request: web.Request):
    """Live QR image for a network — the uploaded one if present, otherwise an
    auto-generated QR of the current address (so the panel always shows what the
    customer would scan)."""
    net = request.match_info["net"].upper()
    if net not in ("TRC20", "BEP20"):
        raise web.HTTPNotFound()
    async with Session() as s:
        stored_img, _, stored_mime = await get_network_qr_raw(s, net)
        current = (await get_bep20_address(s) if net == "BEP20"
                   else await get_deposit_address(s)) or ""
    if stored_img:
        img, mime = stored_img, stored_mime
    else:
        img, mime = (qr_png(current) if current else None), "image/png"
    if not img:
        raise web.HTTPNotFound()
    return web.Response(body=img, content_type=mime,
                        headers={"Cache-Control": "no-store"})


@_authed
async def qr_post(request: web.Request):
    """Upload or remove a custom deposit QR for one network."""
    data = await request.post()
    if not await _check_csrf(request, data):
        return _page("Error", _nav("settings") + "<p>Invalid CSRF token.</p>")
    net = str(data.get("net", "")).upper()
    if net not in ("TRC20", "BEP20"):
        raise web.HTTPFound("/settings")
    if str(data.get("act", "")) == "remove":
        async with Session() as s:
            await clear_network_qr(s, net)
        raise web.HTTPFound("/settings?msg=" + quote(f"✅ {net} custom QR removed."))
    async with Session() as s:
        current = (await get_bep20_address(s) if net == "BEP20"
                   else await get_deposit_address(s)) or ""
    if not current:
        raise web.HTTPFound("/settings?msg=" + quote(
            f"Set the {net} address and Save it before uploading a QR."))
    field = data.get("qr")
    raw = field.file.read() if hasattr(field, "file") else b""
    if not raw:
        raise web.HTTPFound("/settings?msg=" + quote("No image selected."))
    if len(raw) > MAX_QR_BYTES:
        raise web.HTTPFound("/settings?msg=" + quote(
            "Image too large (max 1 MB) — screenshot the QR instead of a full photo."))
    norm = _normalize_upload(raw)
    if norm is None:
        raise web.HTTPFound("/settings?msg=" + quote(
            "That file type isn't supported — upload a PNG, JPG or WebP. "
            "(iPhone HEIC photos aren't: take a screenshot of the QR and upload that.)"))
    img, mime = norm
    async with Session() as s:
        await set_network_qr(s, net, img, current, mime)
    raise web.HTTPFound("/settings?msg=" + quote(
        f"✅ {net} custom QR uploaded and now live."))



@_authed
async def tickets_get(request: web.Request):
    """Support tickets from the website — full details, newest first."""
    show = request.query.get("show", "open")
    async with Session() as s:
        q = select(Ticket).order_by(Ticket.id.desc()).limit(100)
        if show == "open":
            q = q.where(Ticket.status == "open")
        tickets = (await s.scalars(q)).all()
        open_n = await s.scalar(select(func.count()).select_from(Ticket)
                                .where(Ticket.status == "open")) or 0
        orders = {}
        oids = [t.order_id for t in tickets if t.order_id]
        if oids:
            orders = {o.id: o for o in (await s.scalars(
                select(Order).where(Order.id.in_(oids)))).all()}
    csrf = await _csrf_for(request)
    cats = {"deposit": "💸 deposit not credited", "payout": "🏦 payout missing",
            "other": "💬 other"}
    cards = []
    for t in tickets:
        o = orders.get(t.order_id)
        ordline = ""
        if o:
            ordline = (f"<div><span class=muted>Order:</span> "
                       f"<a href='/order/{o.id}'>#ORD{o.id:04d}</a> — "
                       f"{o.usd_amount:g} USDT → ₹{o.inr_amount:,.2f} "
                       f"<span class=badge>{_esc(o.status.replace('_', ' '))}</span></div>")
        tx = (f"<div><span class=muted>TXID:</span> <code>{_esc(t.txid)}</code></div>"
              if t.txid else "")
        who = "🌐 web" if t.user_id < 0 else f"tg {t.user_id}"
        act = ("close" if t.status == "open" else "reopen")
        cards.append(f"""
<div class=card>
<b>#TKT{t.id:04d}</b> <span class='badge {"warn" if t.status == "open" else "ok"}'>
{_esc(t.status)}</span> <span class=badge>{cats.get(t.category, t.category)}</span>
<span class='muted small'> · {t.created_at:%d %b %Y %H:%M} UTC · {who}</span>
<div style='margin:8px 0'>{ordline}{tx}
<div><span class=muted>Contact:</span> <b>{_esc(t.contact)}</b></div></div>
<div style='background:var(--surface-2);border-radius:10px;padding:10px 12px;
 white-space:pre-wrap'>{_esc(t.message)}</div>
<form method=post action='/tickets/{t.id}/{act}' style='margin-top:10px'>
<input type=hidden name=csrf value='{csrf}'>
<button class='btn small'>{'✅ Mark resolved' if act == 'close' else '↩ Reopen'}</button>
</form></div>""")
    tab = (f"<p><a href='/tickets'{' ' if show != 'open' else ' class=on '}>"
           f"Open ({open_n})</a> · <a href='/tickets?show=all'>All</a></p>")
    body = (_nav("tickets") + f"<h1>🎫 Tickets</h1>{tab}"
            + ("".join(cards) or "<div class=card><span class=muted>No "
               + ("open " if show == "open" else "") + "tickets.</span></div>"))
    return _page("Tickets", body)


@_authed
async def ticket_act(request: web.Request):
    data = await request.post()
    if not await _check_csrf(request, data):
        return _page("Error", _nav("tickets") + "<p>Invalid CSRF token.</p>")
    tid = int(request.match_info["id"])
    act = request.match_info["act"]
    async with Session() as s:
        t = await s.get(Ticket, tid)
        if t is not None and act in ("close", "reopen"):
            t.status = "closed" if act == "close" else "open"
            await s.commit()
    raise web.HTTPFound("/tickets")


_ACCT_BASE = 1 << 48        # mirrors website._acct_uid


def _user_banks(cards) -> str:
    """Compact list of a user's saved banks (deduped by details), live from DB —
    so an add or delete on the customer's side shows here on the next load."""
    if not cards:
        return "<div class='muted small'>No bank saved</div>"
    seen, out = set(), []
    for c in cards:
        key = (c.details or "").strip()
        if key in seen:
            continue
        seen.add(key)
        det = " · ".join(ln.strip() for ln in key.splitlines() if ln.strip())
        out.append(f"<div style='font-size:.86rem;margin-top:2px'>"
                   f"<b>{_esc(c.label)}</b> — {_esc(det)}</div>")
    return "".join(out)


@_authed
async def users_get(request: web.Request):
    """Every user — website accounts and Telegram bot users — with their saved
    banks (live), contact details and trading. Banks reflect adds/deletes
    automatically since this reads the DB on each load; the page also
    auto-refreshes every 30 min."""
    from .models import BankCard, PhoneHistory
    async with Session() as s:
        accounts = (await s.scalars(
            select(Account).order_by(Account.id.desc()).limit(500))).all()
        tg_users = (await s.scalars(
            select(User).where(User.id > 0)
            .order_by(User.id.desc()).limit(500))).all()
        acct_uids = [-(_ACCT_BASE + a.id) for a in accounts]
        tg_uids = [u.id for u in tg_users]
        all_uids = acct_uids + tg_uids
        # previous mobile numbers (kept when a customer edits their number) so
        # the desk still sees every contact it once had. Bounded: this page is
        # a live view, the full set is in the phones CSV export.
        old_phones: dict[int, list[str]] = {}
        acct_ids = [a.id for a in accounts]
        if acct_ids:
            for h in (await s.scalars(
                    select(PhoneHistory)
                    .where(PhoneHistory.account_id.in_(acct_ids))
                    .order_by(PhoneHistory.id.desc()).limit(2000))).all():
                lst = old_phones.setdefault(h.account_id, [])
                if h.old_phone not in lst:
                    lst.append(h.old_phone)
        banks: dict[int, list] = {}
        stats: dict[int, tuple] = {}
        if all_uids:
            for c in (await s.scalars(select(BankCard)
                                      .where(BankCard.user_id.in_(all_uids))
                                      .order_by(BankCard.id.desc()))).all():
                banks.setdefault(c.user_id, []).append(c)
            for r in (await s.execute(
                    select(Order.user_id, func.count(Order.id),
                           func.sum(Order.usd_amount))
                    .where(Order.user_id.in_(all_uids),
                           Order.status == OrderStatus.COMPLETED.value)
                    .group_by(Order.user_id))).all():
                stats[r[0]] = (r[1], r[2] or 0.0)

    emails_n = sum(1 for a in accounts if a.email)
    phones_n = sum(1 for a in accounts if a.phone)

    def acct_card(a):
        uid = -(_ACCT_BASE + a.id)
        n, vol = stats.get(uid, (0, 0.0))
        prov = ("<span class='badge ok'>Google</span>" if a.google_sub
                else "<span class=badge>email</span>")
        stock = (f"<span class='badge warn'>{_esc(a.stock)} USDT/day</span>"
                 if a.stock else "")
        # a number they changed BACK to is current again — not "previous"
        prev = [p for p in (old_phones.get(a.id) or []) if p != (a.phone or "")]
        more = f" (+{len(prev) - 3} more in CSV)" if len(prev) > 3 else ""
        prev_html = (f"<div><span class=muted>Previous numbers:</span> "
                     f"{_esc(', '.join(prev[:3]))}{more}</div>" if prev else "")
        return f"""<div class=card>
<b>{_esc(a.email)}</b> {prov} {stock}
<span class='muted small'> · joined {a.created_at:%d %b %Y}</span>
<div style='margin:6px 0'>
<div><span class=muted>Name:</span> <b>{_esc(a.name or "—")}</b>
 · <span class=muted>Phone:</span> <b>{_esc(a.phone or "—")}</b></div>
{prev_html}
<div><span class=muted>Traded:</span> {n} completed · {vol:,.0f} USDT</div>
<div style='margin-top:6px'><span class=muted>Saved banks:</span>
{_user_banks(banks.get(uid))}</div></div></div>"""

    def tg_card(u):
        n, vol = stats.get(u.id, (0, 0.0))
        uname = f"@{_esc(u.username)}" if u.username else "—"
        ban = " <span class='badge danger'>banned</span>" if u.banned else ""
        return f"""<div class=card>
<b>{_esc(u.first_name or 'Telegram user')}</b> {uname}
<span class='muted small'> · id {u.id}{ban}</span>
<div style='margin:6px 0'>
<div><span class=muted>Traded:</span> {n} completed · {vol:,.0f} USDT</div>
<div style='margin-top:6px'><span class=muted>Saved banks:</span>
{_user_banks(banks.get(u.id))}</div></div></div>"""

    _cur_ph = {a.id: (a.phone or "") for a in accounts}
    prev_n = sum(1 for aid, v in old_phones.items()
                 for p in v if p != _cur_ph.get(aid, ""))
    prev_blurb = (f" · {prev_n} previous number(s) kept" if prev_n else "")
    phones_label = ("⬇ Phone numbers CSV (incl. previous)" if prev_n
                    else "⬇ Phone numbers CSV")
    body = (_nav("users") + "<h1>👥 Users</h1>"
            f"<p class=muted>{len(accounts)} website accounts "
            f"({emails_n} emails · {phones_n} phones{prev_blurb}) · "
            f"{len(tg_users)} Telegram users. Live — bank adds/deletes show on "
            "refresh; this page reloads itself every 30 min.</p>"
            "<div class=row style='gap:8px;margin-bottom:8px'>"
            "<a class='btn small' href='/messaging'>✉️ Bulk email &amp; SMS</a>"
            "<a class='btn small' href='/users/export/emails'>⬇ Emails CSV</a>"
            f"<a class='btn small' href='/users/export/phones'>{phones_label}</a></div>"
            "<div class=duo>"
            f"<div><h2>🌐 Website users ({len(accounts)})</h2>"
            + ("".join(acct_card(a) for a in accounts)
               or "<div class=card><span class=muted>No signups yet.</span></div>")
            + f"</div><div><h2>✈️ Telegram users ({len(tg_users)})</h2>"
            + ("".join(tg_card(u) for u in tg_users)
               or "<div class=card><span class=muted>No Telegram users yet.</span></div>")
            + "</div></div>"
            "<script>setTimeout(function(){location.reload()},1800000)</script>")
    return _page("Users", body)


@_authed
async def users_export(request: web.Request):
    """CSV of account emails or phone numbers — for a bulk email tool or a
    WhatsApp Business broadcast list. The phones export also carries every
    number a customer has moved away from (status=previous), so a number an
    account once used is never dropped from a bulk list."""
    from .models import BankCard, PhoneHistory, User
    kind = request.match_info["kind"]
    if kind not in ("emails", "phones"):
        raise web.HTTPNotFound()
    _ACCT_BASE = 1 << 48                # website account → its stable user id
    async with Session() as s:
        accounts = (await s.scalars(select(Account)
                                    .order_by(Account.id.desc()))).all()
        # bot users who verified an email in Telegram (no account row)
        bot_users = (await s.scalars(select(User).where(
            User.id > 0, User.email != "", User.email_verified.is_(True))
            .order_by(User.id.desc()))).all()
        # newest saved bank per customer, one query for everyone
        uids = ([-( _ACCT_BASE + a.id) for a in accounts]
                + [u.id for u in bot_users])
        banks: dict[int, str] = {}
        if uids:
            for c in (await s.scalars(select(BankCard).where(
                    BankCard.user_id.in_(uids)).order_by(BankCard.id))).all():
                banks[c.user_id] = c.details.replace("\n", " | ")
        history = []
        if kind == "phones":
            names = {a.id: (a.name or "") for a in accounts}
            history = (await s.scalars(
                select(PhoneHistory).order_by(PhoneHistory.id.desc()))).all()

    def acct_bank(a) -> str:
        return banks.get(-(_ACCT_BASE + a.id), "")

    buf = io.StringIO()
    w = csv.writer(buf)
    if kind == "emails":
        w.writerow(["email", "name", "phone", "daily_stock", "bank",
                    "verified", "source"])
        for a in accounts:
            if a.email:
                w.writerow([_csv_safe(x) for x in
                            (a.email, a.name or "", a.phone or "",
                             a.stock or "", acct_bank(a),
                             "yes" if a.email_verified else "no", "website")])
        for u in bot_users:
            w.writerow([_csv_safe(x) for x in
                        (u.email, u.first_name or "", "", "",
                         banks.get(u.id, ""), "yes", "telegram")])
    else:
        w.writerow(["phone", "name", "email", "daily_stock", "bank",
                    "status", "changed_at"])
        seen: set[str] = set()
        for a in accounts:
            if a.phone:
                seen.add(a.phone)
                w.writerow([_csv_safe(x) for x in
                            (a.phone, a.name or "", a.email or "",
                             a.stock or "", acct_bank(a), "current", "")])
        # previous numbers, most-recent first; skip any that's still someone's
        # current number so the list has no duplicates
        by_id = {a.id: a for a in accounts}
        for h in history:
            if h.old_phone and h.old_phone not in seen:
                seen.add(h.old_phone)
                owner = by_id.get(h.account_id)
                w.writerow([_csv_safe(x) for x in
                            (h.old_phone, names.get(h.account_id, ""),
                             owner.email if owner else "",
                             owner.stock if owner else "",
                             acct_bank(owner) if owner else "",
                             "previous", f"{h.changed_at:%Y-%m-%d}")])
    return web.Response(
        body=("﻿" + buf.getvalue()).encode("utf-8"),
        content_type="text/csv", charset="utf-8",
        headers={"Content-Disposition": f'attachment; filename="{kind}.csv"'})


@_authed
async def marketing_get(request: web.Request):
    """Paste-the-full-code marketing pixels: Meta, Google, and any extra head
    snippet. Injected verbatim on public website pages (never on private ones)."""
    async with Session() as s:
        meta = await get_setting(s, "track_meta_code") or ""
        google = await get_setting(s, "track_google_code") or ""
        custom = await get_setting(s, "track_custom_code") or ""
    csrf = await _csrf_for(request)
    on = [n for n, v in [("Meta Pixel", meta), ("Google tag", google),
                         ("Extra", custom)] if v.strip()]
    status = (f"<div class='banner ok'>✅ Live: {', '.join(on)}</div>" if on
              else "<div class=banner>No tracking code set yet — paste your "
                   "snippets below and Save.</div>")
    saved_banner = ("<div class='banner ok'>✅ Saved — the code is live on the "
                    "website now.</div>" if request.query.get("saved") else "")
    body = (_nav("marketing") + "<h1>📈 Marketing pixels</h1>" + saved_banner
            + "<p class=muted>Paste each provider's <b>full code</b> exactly as "
            "they give it to you (the whole <code>&lt;script&gt;…&lt;/script&gt;</code> "
            "block). It loads in the &lt;head&gt; of every <b>public</b> page — "
            "never on customer order or account pages. Leave a box blank to turn "
            "that one off. The site's privacy policy updates automatically to "
            "disclose whatever you enable.</p>"
            + status
            + "<form method=post action=/marketing>"
            f"<input type=hidden name=csrf value='{csrf}'>"
            "<div class=card><label>Meta (Facebook) Pixel code</label>"
            "<textarea name=meta_code rows=8 spellcheck=false "
            "placeholder='&lt;!-- Meta Pixel Code --&gt; …'>"
            f"{_esc(meta)}</textarea></div>"
            "<div class=card><label>Google tag code (gtag.js / GA4 / Ads)</label>"
            "<textarea name=google_code rows=8 spellcheck=false "
            "placeholder='&lt;!-- Global site tag (gtag.js) --&gt; …'>"
            f"{_esc(google)}</textarea></div>"
            "<div class=card><label>Any other head code (TikTok, etc.) — optional</label>"
            "<textarea name=custom_code rows=6 spellcheck=false>"
            f"{_esc(custom)}</textarea></div>"
            "<div class=row style='margin-top:14px'><button>Save &amp; go live</button></div>"
            "</form>")
    return _page("Marketing", body)


@_authed
async def marketing_post(request: web.Request):
    data = await request.post()
    if not await _check_csrf(request, data):
        return _page("Error", _nav("marketing") + "<p>Invalid CSRF token.</p>")
    cap = 20000
    async with Session() as s:
        await set_setting(s, "track_meta_code", str(data.get("meta_code", ""))[:cap])
        await set_setting(s, "track_google_code", str(data.get("google_code", ""))[:cap])
        await set_setting(s, "track_custom_code", str(data.get("custom_code", ""))[:cap])
    try:
        from .website import load_tracking
        await load_tracking()
    except Exception:
        log.exception("tracking cache refresh failed")
    raise web.HTTPFound("/marketing?saved=1")


# ── Messaging: bulk email (Brevo SMTP) + bulk SMS (MSG91) ────────────────────

def _job_banner(job: dict, label: str) -> str:
    if job.get("running"):
        done = job["sent"] + job["failed"]
        return (f"<div class='banner'>⏳ {label} sending… {done}/{job['total']} "
                f"({job['sent']} ok, {job['failed']} failed)</div>")
    if job.get("total") or job.get("finished"):
        cls = "ok" if job["failed"] == 0 else "warn"
        err = (f" · first issue: {_esc(job['errors'][0])}"
               if job.get("errors") else "")
        return (f"<div class='banner {cls}'>Last {label} run: {job['sent']} sent, "
                f"{job['failed']} failed of {job['total']}{err}</div>")
    return ""


@_authed
async def messaging_get(request: web.Request):
    e = await sender.email_config()
    sm = await sender.sms_config()
    e_ready = sender.email_ready(e)
    s_ready = sender.sms_ready(sm)
    e_recips = await sender.email_recipients()
    s_recips = await sender.sms_recipients()
    e_job = sender.email_status()
    s_job = sender.sms_status()
    csrf = await _csrf_for(request)
    msg = request.query.get("msg", "")
    ok = request.query.get("ok") == "1"
    banner = (f"<div class='banner {'ok' if ok else 'warn'}'>{_esc(msg)}</div>"
              if msg else "")

    e_badge = ("<span class='badge ok'>✓ ready</span>" if e_ready
               else "<span class='badge warn'>needs setup</span>")
    s_badge = ("<span class='badge ok'>✓ ready</span>" if s_ready
               else "<span class='badge warn'>needs setup</span>")
    pw_ph = "•••••• (set — blank keeps it)" if e["pw"] else "your Brevo SMTP key"
    key_ph = "•••••• (set — blank keeps it)" if sm["authkey"] else "your MSG91 auth key"
    site_warn = ("" if settings.site_url else
                 "<div class='banner warn'>⚠️ Set the site URL (P2P_SITE_URL) so "
                 "emails can carry a working one-click unsubscribe link — Gmail and "
                 "Yahoo require it for bulk mail.</div>")

    email_card = (
        f"<h2>📧 Email — Brevo SMTP {e_badge}</h2>"
        "<p class=muted>Cloudflare only receives mail — to send, plug in Brevo "
        "(or any SMTP). Verify your domain in Brevo and add its SPF/DKIM records "
        "to Cloudflare DNS so you don't land in spam.</p>"
        + site_warn
        + _job_banner(e_job, "Email")
        + "<form method=post action=/messaging/email><div class=card>"
        f"<input type=hidden name=csrf value='{csrf}'>"
        "<label>SMTP host</label>"
        f"<input name=host value='{_esc(e['host'])}' placeholder='smtp-relay.brevo.com'>"
        "<div class=row style='gap:12px'>"
        f"<div style='flex:1'><label>Port</label>"
        f"<input name=port value='{_esc(e['port'])}' placeholder='587'></div>"
        f"<div style='flex:2'><label>SMTP login</label>"
        f"<input name=user value='{_esc(e['user'])}' "
        "placeholder='your Brevo login / SMTP user'></div></div>"
        "<label>SMTP key / password</label>"
        f"<input type=password name=pw autocomplete=off placeholder='{pw_ph}'>"
        "<div class=row style='gap:12px'>"
        f"<div style='flex:2'><label>From email (verified sender)</label>"
        f"<input name=from_addr type=email value='{_esc(e['from_addr'])}' "
        "placeholder='support@yourdomain.com'></div>"
        f"<div style='flex:1'><label>From name</label>"
        f"<input name=from_name value='{_esc(e['from_name'])}' "
        "placeholder='IndiaXchange'></div></div>"
        "<p class='muted small' style='margin:12px 0 0'><b>Split senders "
        "(optional, recommended)</b> — a separate address per email type keeps "
        "marketing complaints from hurting your codes and receipts. Blank = "
        "everything sends from the main address. Replies always route to the "
        "main address. Add each in Cloudflare Email Routing (or enable "
        "catch-all) so replies to them don't bounce.</p>"
        "<div class=row style='gap:12px'>"
        f"<div style='flex:1'><label>Order emails from</label>"
        f"<input name=from_tx type=email value='{_esc(e['from_tx'])}' "
        "placeholder='orders@yourdomain.com'></div>"
        f"<div style='flex:1'><label>Verification codes from</label>"
        f"<input name=from_otp type=email value='{_esc(e['from_otp'])}' "
        "placeholder='verify@yourdomain.com'></div>"
        f"<div style='flex:1'><label>Campaigns &amp; rates from</label>"
        f"<input name=from_mkt type=email value='{_esc(e['from_mkt'])}' "
        "placeholder='updates@yourdomain.com'></div></div>"
        "<div class=row style='margin-top:12px'>"
        "<button name=op value=creds>Save email settings</button></div>"
        "</div></form>"
        "<form method=post action=/messaging/email><div class=card>"
        f"<input type=hidden name=csrf value='{csrf}'>"
        f"<p class=muted style='margin-top:0'>Sends to <b>{len(e_recips)}</b> "
        "subscribed email(s). Every message carries a one-click unsubscribe link.</p>"
        "<label>Subject</label><input name=subject maxlength=200>"
        "<label>Message</label><textarea name=body rows=8></textarea>"
        "<label style='display:flex;gap:8px;align-items:center;margin:8px 0'>"
        "<input type=checkbox name=html value=1 style='width:auto'> "
        "Body is HTML (leave off for plain text)</label>"
        "<div class=row style='gap:8px;align-items:flex-end'>"
        "<div style='flex:1'><label>Send a test to yourself first</label>"
        "<input name=test_to type=email placeholder='you@example.com'></div>"
        "<button name=op value=test class='btn small'>Send test</button></div>"
        "<label style='display:flex;gap:8px;align-items:center;margin:12px 0 8px'>"
        f"<input type=checkbox name=confirm value=1 style='width:auto'> "
        f"I understand this emails all {len(e_recips)} subscribers</label>"
        f"<div class=row><button name=op value=all>📨 Send to all "
        f"{len(e_recips)}</button></div>"
        "</div></form>")

    sms_card = (
        f"<h2>📱 SMS — MSG91 {s_badge}</h2>"
        "<p class=muted>India requires <b>DLT registration</b> first: register your "
        "business, a 6-char sender header, and each message template on the DLT "
        "portal, then paste the MSG91 auth key + your approved template ID below. "
        "SMS text must match the registered template — the box below fills its "
        "variable.</p>"
        + _job_banner(s_job, "SMS")
        + "<form method=post action=/messaging/sms><div class=card>"
        f"<input type=hidden name=csrf value='{csrf}'>"
        "<label>MSG91 auth key</label>"
        f"<input type=password name=authkey autocomplete=off placeholder='{key_ph}'>"
        "<div class=row style='gap:12px'>"
        f"<div style='flex:1'><label>Sender ID (DLT header)</label>"
        f"<input name=sender value='{_esc(sm['sender'])}' placeholder='IXCHNG'></div>"
        f"<div style='flex:1'><label>Template ID</label>"
        f"<input name=template value='{_esc(sm['template'])}' "
        "placeholder='DLT/MSG91 template id'></div>"
        f"<div style='flex:1'><label>Variable name</label>"
        f"<input name=var value='{_esc(sm['var'])}' placeholder='var1'></div>"
        "</div>"
        "<div class=row style='margin-top:12px'>"
        "<button name=op value=creds>Save SMS settings</button></div>"
        "</div></form>"
        "<form method=post action=/messaging/sms><div class=card>"
        f"<input type=hidden name=csrf value='{csrf}'>"
        f"<p class=muted style='margin-top:0'>Sends to <b>{len(s_recips)}</b> "
        "valid mobile number(s).</p>"
        "<label>Message (fills your template's variable)</label>"
        "<textarea name=message rows=4 maxlength=1000></textarea>"
        "<div class=row style='gap:8px;align-items:flex-end'>"
        "<div style='flex:1'><label>Send a test to a number</label>"
        "<input name=test_to placeholder='9876543210'></div>"
        "<button name=op value=test class='btn small'>Send test</button></div>"
        "<label style='display:flex;gap:8px;align-items:center;margin:12px 0 8px'>"
        f"<input type=checkbox name=confirm value=1 style='width:auto'> "
        f"I understand this texts all {len(s_recips)} numbers</label>"
        f"<div class=row><button name=op value=all>📲 Send to all "
        f"{len(s_recips)}</button></div>"
        "</div></form>")

    refresh = ("<script>setTimeout(function(){location.reload()},8000)</script>"
               if (e_job["running"] or s_job["running"]) else "")
    body = (_nav("messaging") + "<h1>✉️ Messaging</h1>" + banner
            + "<p class=muted>Reach your signups by email and SMS. Telegram bot "
            "users have their own free <a href='/broadcast'>Broadcast</a>.</p>"
            "<div class=duo>"
            f"<div>{email_card}</div><div>{sms_card}</div></div>"
            + refresh)
    return _page("Messaging", body)


def _redir_msg(path: str, text: str, ok: bool = False):
    raise web.HTTPFound(f"{path}?ok={'1' if ok else '0'}&msg=" + quote(text))


@_authed
async def messaging_email_post(request: web.Request):
    data = await request.post()
    if not await _check_csrf(request, data):
        return _page("Error", _nav("messaging") + "<p>Invalid CSRF token.</p>")
    op = str(data.get("op", ""))
    if op == "creds":
        port = str(data.get("port", "")).strip() or "587"
        if not port.isdigit():
            _redir_msg("/messaging", "Port must be a number.")
        async with Session() as s:
            await set_setting(s, "email_smtp_host", str(data.get("host", "")).strip()[:120])
            await set_setting(s, "email_smtp_port", port)
            await set_setting(s, "email_smtp_user", str(data.get("user", "")).strip()[:190])
            await set_setting(s, "email_from", str(data.get("from_addr", "")).strip()[:190])
            await set_setting(s, "email_from_name", str(data.get("from_name", "")).strip()[:120])
            # optional per-stream senders (blank = use the main From)
            for field, key in (("from_tx", "email_from_tx"),
                               ("from_otp", "email_from_otp"),
                               ("from_mkt", "email_from_mkt")):
                val = str(data.get(field, "")).strip()[:190]
                if val == "" or ("@" in val and "." in val.split("@")[-1]
                                 and " " not in val):
                    await set_setting(s, key, val)
            pw = str(data.get("pw", ""))
            if pw.strip():                      # blank keeps the current key
                await set_setting(s, "email_smtp_pass", pw)
        _redir_msg("/messaging", "✅ Email settings saved.", ok=True)

    subject = str(data.get("subject", "")).strip()
    body = str(data.get("body", ""))
    is_html = bool(data.get("html"))
    if op == "test":
        ok, detail = await sender.send_test_email(
            str(data.get("test_to", "")), subject, body, is_html)
        _redir_msg("/messaging", detail, ok=ok)
    if op == "all":
        if not bool(data.get("confirm")):
            _redir_msg("/messaging", "Tick the confirm box before sending to everyone.")
        if not subject or not body.strip():
            _redir_msg("/messaging", "Add a subject and a message first.")
        ok, detail = await sender.start_email_broadcast(
            subject, body, is_html, bot=request.app.get("bot"))
        _redir_msg("/messaging", detail, ok=ok)
    _redir_msg("/messaging", "Unknown action.")


@_authed
async def messaging_sms_post(request: web.Request):
    data = await request.post()
    if not await _check_csrf(request, data):
        return _page("Error", _nav("messaging") + "<p>Invalid CSRF token.</p>")
    op = str(data.get("op", ""))
    if op == "creds":
        async with Session() as s:
            await set_setting(s, "sms_msg91_sender", str(data.get("sender", "")).strip()[:16])
            await set_setting(s, "sms_msg91_template", str(data.get("template", "")).strip()[:64])
            await set_setting(s, "sms_msg91_var", str(data.get("var", "")).strip()[:32] or "var1")
            key = str(data.get("authkey", ""))
            if key.strip():                     # blank keeps the current key
                await set_setting(s, "sms_msg91_authkey", key.strip())
        _redir_msg("/messaging", "✅ SMS settings saved.", ok=True)

    message = str(data.get("message", "")).strip()
    if op == "test":
        ok, detail = await sender.send_test_sms(str(data.get("test_to", "")), message)
        _redir_msg("/messaging", detail, ok=ok)
    if op == "all":
        if not bool(data.get("confirm")):
            _redir_msg("/messaging", "Tick the confirm box before texting everyone.")
        if not message:
            _redir_msg("/messaging", "Type the message first.")
        ok, detail = await sender.start_sms_broadcast(
            message, bot=request.app.get("bot"))
        _redir_msg("/messaging", detail, ok=ok)
    _redir_msg("/messaging", "Unknown action.")


# ── Backups: rotating DB snapshots ───────────────────────────────────────────

def _fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n/1:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


@_authed
async def backups_get(request: web.Request):
    from . import backup
    rows = backup.list_backups()
    msg = request.query.get("msg", "")
    banner = (f"<div class='banner ok'>{_esc(msg)}</div>" if msg else "")
    csrf = await _csrf_for(request)
    if rows:
        from datetime import datetime, timezone, timedelta
        items = "".join(
            "<tr><td><code>{n}</code></td><td>{s}</td><td class=muted>{t}</td>"
            "<td><a class='btn small' href='/backups/download/{n}'>⬇ Download</a></td></tr>"
            .format(n=_esc(r["name"]), s=_fmt_size(r["size"]),
                    t=_esc((datetime.fromtimestamp(r["mtime"], timezone.utc)
                            + timedelta(hours=5, minutes=30)).strftime("%d %b %Y, %I:%M %p") + " IST"))
            for r in rows)
        table = ("<table class=tbl style='width:100%'><tr><th>File</th><th>Size</th>"
                 f"<th>Created</th><th></th></tr>{items}</table>")
    else:
        table = ("<div class=card><span class=muted>No backups yet — the first "
                 "one is written on the next daily cycle, or tap “Back up now”.</span></div>")
    body = (_nav("backups") + "<h1>💾 Backups</h1>" + banner
            + "<p class=muted>Your whole desk — accounts, orders, bank cards and "
            "every setting — is one SQLite file. These are consistent snapshots, "
            f"taken automatically once a day and kept for the last {backup.BACKUP_KEEP}. "
            "Download one anytime; to restore, stop the bot, replace "
            "<code>p2p.sqlite3</code> with the backup, and start it again.</p>"
            "<form method=post action=/backups/run style='margin:0 0 16px'>"
            f"<input type=hidden name=csrf value='{csrf}'>"
            "<button>Back up now</button></form>"
            + table
            + "<p class='muted small' style='margin-top:14px'>💡 For off-server "
            "safety, download a copy to your own machine or cloud drive "
            "periodically — these live on the same server as the bot.</p>")
    return _page("Backups", body)


@_authed
async def backups_run(request: web.Request):
    data = await request.post()
    if not await _check_csrf(request, data):
        return _page("Error", _nav("backups") + "<p>Invalid CSRF token.</p>")
    from . import backup
    from datetime import datetime, timezone
    try:
        name = await backup.make_backup(
            datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"))
        msg = f"✅ Backup created: {name}" if name else "⚠️ No database file to back up yet."
    except Exception:
        log.exception("manual backup failed")
        msg = "⚠️ Backup failed — check the server logs."
    raise web.HTTPFound("/backups?msg=" + quote(msg))


@_authed
async def backups_download(request: web.Request):
    from . import backup
    p = backup.backup_path(request.match_info["name"])
    if p is None:
        raise web.HTTPNotFound()
    return web.FileResponse(p, headers={
        "Content-Disposition": f'attachment; filename="{p.name}"',
        "Content-Type": "application/octet-stream",
        "Cache-Control": "private, no-store"})


async def start_panel(bot):
    """Start the web panel if a password is configured; returns the AppRunner
    (or None when disabled) so main() can clean it up."""
    if not await _panel_password():
        log.info("web panel disabled (no P2P_PANEL_PASSWORD set)")
        return None
    app = web.Application()
    app["bot"] = bot
    app.add_routes([
        web.get("/login", login_get),
        web.post("/login", login_post),
        web.get("/logout", logout),
        web.get("/", dashboard),
        web.post("/desk/toggle", desk_toggle),
        web.get("/pay", pay_get),
        web.get("/tickets", tickets_get),
        web.post("/tickets/{id:\\d+}/{act}", ticket_act),
        web.get("/users", users_get),
        web.get("/users/export/{kind}", users_export),
        web.get("/marketing", marketing_get),
        web.post("/marketing", marketing_post),
        web.get("/messaging", messaging_get),
        web.post("/messaging/email", messaging_email_post),
        web.post("/messaging/sms", messaging_sms_post),
        web.post("/pay", pay_post),
        web.get("/broadcast", broadcast_get),
        web.post("/broadcast", broadcast_post),
        web.post("/broadcast/emails", broadcast_emails_post),
        web.get("/backups", backups_get),
        web.post("/backups/run", backups_run),
        web.get("/backups/download/{name}", backups_download),
        web.get("/settings", settings_get),
        web.post("/settings", settings_post),
        web.post("/settings/qr", qr_post),
        web.get("/qr/{net}.png", qr_preview),
        web.get("/orders/print", orders_print),
        web.get("/orders.csv", orders_csv),
        web.get("/order/{id:\\d+}", order_detail),
        web.get("/order/{id:\\d+}/print", order_print),
        web.post("/order/{id:\\d+}/done", _order_action(complete_order)),
        web.post("/order/{id:\\d+}/refund", _order_action(refund_order)),
        web.post("/order/{id:\\d+}/reject", _order_action(reject_refund)),
        web.post("/order/{id:\\d+}/confirm", _order_action(confirm_deposit, needs_txid=True)),
        web.post("/order/{id:\\d+}/decline", _order_action(decline_order)),
    ])
    ssl_context = None
    scheme = "http"
    if settings.panel_tls_cert and settings.panel_tls_key:
        import ssl
        ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ssl_context.load_cert_chain(settings.panel_tls_cert, settings.panel_tls_key)
        scheme = "https"
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, settings.panel_host, settings.panel_port,
                       ssl_context=ssl_context)
    await site.start()
    log.info("web panel on %s://%s:%s", scheme, settings.panel_host, settings.panel_port)
    if settings.panel_host not in ("127.0.0.1", "localhost") and ssl_context is None:
        log.warning("⚠️ panel is on a public interface WITHOUT TLS — password and "
                    "bot token travel in clear. Set P2P_PANEL_TLS_CERT/KEY and lock "
                    "the port to your IP with a firewall.")
    return runner
