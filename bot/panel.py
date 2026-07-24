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
import hashlib
import hmac
import html
import logging
import os
import secrets
import time

from aiohttp import web

from . import texts
from .actions import (
    complete_order,
    compose_announcement,
    confirm_deposit,
    launch_broadcast,
    refund_order,
    reject_refund,
)
from .config import SERVICES, settings
from .db import (
    Session,
    desk_state,
    get_deposit_address,
    get_desk_open,
    get_setting,
    get_support,
    set_setting,
)
from .helpers import is_trc20
from .models import Order, OrderStatus, User
from sqlalchemy import func, select

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


def _page(title: str, body: str) -> web.Response:
    doc = f"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>{_esc(title)} · P2P Desk</title><style>
:root{{color-scheme:light dark}}
body{{font-family:system-ui,sans-serif;max-width:900px;margin:0 auto;padding:16px;
background:#0d1117;color:#e6edf3}}
a{{color:#58a6ff}} h1,h2{{font-weight:650}}
.tabs a{{display:inline-block;padding:8px 14px;margin:2px;border-radius:8px;
background:#161b22;text-decoration:none}}
.tabs a.on{{background:#1f6feb;color:#fff}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:12px;margin:10px 0}}
.muted{{color:#8b949e;font-size:.9em}}
input,select{{width:100%;box-sizing:border-box;padding:8px;margin:4px 0 12px;
border-radius:8px;border:1px solid #30363d;background:#0d1117;color:#e6edf3}}
button{{padding:9px 16px;border:0;border-radius:8px;background:#238636;color:#fff;
font-size:1em;cursor:pointer}}
button.warn{{background:#9e6a03}} button.danger{{background:#da3633}}
code{{background:#0d1117;padding:1px 5px;border-radius:5px}}
.row{{display:flex;gap:8px;flex-wrap:wrap;align-items:center}}
.nav{{display:flex;gap:14px;margin-bottom:14px}}
</style></head><body>{body}</body></html>"""
    return web.Response(text=doc, content_type="text/html")


def _desk_toggle_btn(switch_on: bool, csrf: str, back: str) -> str:
    label = "🔴 Close desk now" if switch_on else "🟢 Open desk now"
    cls = "warn" if switch_on else ""
    return (f"<form method=post action=/desk/toggle style='display:inline'>"
            f"<input type=hidden name=csrf value='{csrf}'>"
            f"<input type=hidden name=back value='{_esc(back)}'>"
            f"<button class='{cls}'>{label}</button></form>")


def _nav(active: str) -> str:
    def link(href, label, key):
        on = " style='font-weight:700'" if key == active else ""
        return f"<a href='{href}'{on}>{label}</a>"
    return ("<div class=nav>" + link("/", "📋 Orders", "orders")
            + link("/broadcast", "📢 Broadcast", "broadcast")
            + link("/settings", "⚙️ Settings", "settings")
            + "<a href='/logout' style='margin-left:auto'>Logout</a></div>")


# ── routes ────────────────────────────────────────────────────────────────────

async def login_get(request: web.Request):
    return _page("Login", "<h1>P2P Desk — Login</h1>"
                 "<form method=post action=/login>"
                 "<label>Password</label><input type=password name=password autofocus>"
                 "<button>Sign in</button></form>")


async def login_post(request: web.Request):
    ip = request.headers.get("X-Forwarded-For", request.remote or "?").split(",")[0].strip()
    count, first = _login_fails.get(ip, (0, time.time()))
    if count >= 5 and time.time() - first < 300:
        return _page("Login", "<h1>Too many attempts</h1><p>Try again in a few minutes.</p>")
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
    return _page("Login", "<h1>P2P Desk — Login</h1><p style='color:#f85149'>"
                 "Wrong password.</p><form method=post action=/login>"
                 "<input type=password name=password autofocus>"
                 "<button>Sign in</button></form>")


async def logout(request: web.Request):
    resp = web.HTTPFound("/login")
    resp.del_cookie(COOKIE)
    return resp


@_authed
async def dashboard(request: web.Request):
    tab = request.query.get("tab", "active")
    if tab not in TABS:
        tab = "active"
    label, statuses = TABS[tab]
    async with Session() as s:
        is_open, reason = await desk_state(s)
        switch_on = await get_desk_open(s)
        q = select(Order).where(Order.status.in_(statuses))
        q = q.order_by(Order.id.desc()).limit(30) if tab == "done" else q.order_by(Order.id)
        orders = (await s.scalars(q)).all()
    toggle = _desk_toggle_btn(switch_on, await _csrf_for(request), "/")
    if is_open:
        desk_banner = ("<div class=card style='border-color:#238636'>"
                       "🟢 <b>Desk is OPEN</b> — taking new sell orders.  "
                       f"{toggle}</div>")
    else:
        desk_banner = ("<div class=card style='border-color:#da3633'>"
                       f"🔴 <b>Desk is CLOSED</b> — {_esc(reason)}.  {toggle}</div>")
    tabs_html = "<div class=tabs>" + "".join(
        f"<a class='{'on' if k == tab else ''}' href='/?tab={k}'>{lbl}</a>"
        for k, (lbl, _) in TABS.items()) + "</div>"
    rows = []
    for o in orders:
        emoji = texts.STATUS_EMOJI.get(o.status, "•")
        rows.append(
            f"<div class=card><div class=row><b>{emoji} {texts.tag(o.id)}</b>"
            f"<span class=muted>{_esc(o.status)}</span></div>"
            f"{o.usd_amount:g}$ · {_esc(SERVICES.get(o.service, o.service))} → "
            f"₹{o.inr_amount:,.2f}<br>"
            f"<a href='/order/{o.id}'>Open →</a></div>")
    body = (_nav("orders") + desk_banner + f"<h1>Orders — {label} ({len(orders)})</h1>"
            + tabs_html + ("".join(rows) or "<p class=muted>Nothing here.</p>"))
    return _page("Orders", body)


@_authed
async def order_detail(request: web.Request):
    from .models import BankCard, User
    oid = int(request.match_info["id"])
    async with Session() as s:
        order = await s.get(Order, oid)
        if order is None:
            return _page("Order", _nav("orders") + "<p>Order not found.</p>")
        user = await s.get(User, order.user_id)
        card = await s.get(BankCard, order.bank_card_id) if order.bank_card_id else None
    csrf = await _csrf_for(request)
    uname = f"@{_esc(user.username)}" if user and user.username else "—"
    msg = request.query.get("msg", "")
    banner = (f"<div class=card style='border-color:#238636'>{_esc(msg)}</div>"
              if msg else "")
    lines = [
        _nav("orders"),
        f"<h1>{texts.STATUS_EMOJI.get(order.status,'•')} {texts.tag(order.id)}</h1>",
        banner,
        f"<div class=card><b>Status:</b> {_esc(order.status)}<br>"
        f"<b>Sell:</b> {order.usd_amount:g}$ USDT via "
        f"{_esc(SERVICES.get(order.service, order.service))} @ 1$/₹{order.rate_inr:g}<br>"
        f"<b>Pay out:</b> ₹{order.inr_amount:,.2f}<br>"
        f"<b>User:</b> {_esc(user.first_name) if user else '?'} {uname} "
        f"(id <code>{order.user_id}</code>)<br>"
        f"<b>Bank:</b><br><code>{_esc(card.details) if card else '—'}</code><br>"
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
                f"<button class=warn>📥 Confirm deposit</button></form>")
    if order.status == OrderStatus.REFUND_REQUESTED.value:
        act += (f"<form method=post action='/order/{order.id}/refund'>"
                f"<input type=hidden name=csrf value='{csrf}'>"
                f"<button>💸 Refund sent (to sender)</button></form>"
                f"<form method=post action='/order/{order.id}/reject'>"
                f"<input type=hidden name=csrf value='{csrf}'>"
                f"<button class=danger>🚫 Reject (fake / no deposit)</button></form>")
    act += "</div>"
    lines.append(act)
    return _page(f"Order {order.id}", "".join(lines))


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
                           .where(User.banned.is_(False)))
    csrf = await _csrf_for(request)
    msg = request.query.get("msg", "")
    banner = (f"<div class=card style='border-color:#238636'>{_esc(msg)}</div>"
              if msg else "")
    body = (_nav("broadcast") + "<h1>📢 Broadcast</h1>" + banner
            + f"<p class=muted>Sends a message to all <b>{n or 0}</b> bot users "
            "(skipping anyone who blocked the bot).</p>"
            "<form method=post action=/broadcast>"
            f"<input type=hidden name=csrf value='{csrf}'>"
            "<label>Message</label>"
            "<textarea name=text rows=5 style='width:100%;box-sizing:border-box;"
            "padding:8px;border-radius:8px;border:1px solid #30363d;"
            "background:#0d1117;color:#e6edf3'></textarea>"
            "<label style='display:flex;gap:8px;align-items:center;margin:10px 0'>"
            "<input type=checkbox name=to_proof value='1' style='width:auto'> "
            "Also post to the proof channel</label>"
            "<div class=row><button>Send broadcast</button></div>"
            "</form>")
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
                           .where(User.banned.is_(False)))
    launch_broadcast(request.app["bot"], compose_announcement(text), to_proof)
    raise web.HTTPFound("/broadcast?msg=" + html.escape(
        f"Broadcast started to {n or 0} users — you'll get a summary in Telegram."))


@_authed
async def settings_get(request: web.Request):
    async with Session() as s:
        is_open, reason = await desk_state(s)
        desk_switch = (await get_setting(s, "desk_open")) != "0"
        rates = {k: (await get_setting(s, f"rate_{k}") or "") for k in SERVICES}
        lims = {k: ((await get_setting(s, f"limit_min_{k}") or ""),
                    (await get_setting(s, f"limit_max_{k}") or "")) for k in SERVICES}
        addr = await get_deposit_address(s) or ""
        support = await get_setting(s, "support") or ""
        admin_ids = await get_setting(s, "admin_ids")
        admin_ids = admin_ids if admin_ids is not None else settings.admin_ids
        admin_chat = await get_setting(s, "admin_chat_id") or ""
        proof = await get_setting(s, "proof_channel") or ""
        token_set = bool((await get_setting(s, "bot_token")) or settings.bot_token)
    csrf = await _csrf_for(request)
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
    status_line = ("🟢 Desk is OPEN" if is_open
                   else f"🔴 Desk is CLOSED — {_esc(reason)}")
    desk_toggle_html = _desk_toggle_btn(desk_switch, csrf, "/settings")
    body = (_nav("settings") + "<h1>Settings</h1>"
            f"<div class=card>{status_line}<br>"
            f"<div style='margin-top:8px'>{desk_toggle_html}</div>"
            "<span class=muted>Toggles instantly — no Save needed. The desk also "
            "needs a deposit address and at least one rate below.</span></div>"
            "<form method=post action=/settings>"
            f"<input type=hidden name=csrf value='{csrf}'>"
            "<h2>Rates</h2>" + rate_fields
            + "<h2>Deposit & payout</h2>"
            "<label>TRC20 deposit address</label>"
            f"<input name=addr value='{_esc(addr)}'>"
            "<label>Support usernames (space-separated, e.g. @a @b)</label>"
            f"<input name=support value='{_esc(support)}'>"
            "<label>Proof channel (@channel or -100… id, blank to disable)</label>"
            f"<input name=proof value='{_esc(proof)}'>"
            "<h2>Admins</h2>"
            "<label>Admin Telegram IDs (space/comma-separated)</label>"
            f"<input name=admin_ids value='{_esc(admin_ids)}'>"
            "<label>Admin group chat id (optional, -100…; blank = DM each admin)</label>"
            f"<input name=admin_chat value='{_esc(admin_chat)}'>"
            "<h2>Bot token</h2>"
            f"<p class=muted>{'A token is set.' if token_set else '⚠️ No token set.'} "
            "Changing it restarts the bot.</p>"
            "<label>New bot token (leave blank to keep current)</label>"
            "<input type=password name=bot_token autocomplete=off placeholder='••••••'>"
            "<h2>Panel password</h2>"
            "<p class=muted>The panel is reachable from any device, so make this "
            "long — a 4-word phrase plus numbers is ideal.</p>"
            "<label>New panel password (blank = keep current)</label>"
            "<input type=password name=panel_password autocomplete=new-password "
            "placeholder='••••••'>"
            "<div class=row><button>Save settings</button></div>"
            "</form>")
    return _page("Settings", body)


@_authed
async def settings_post(request: web.Request):
    data = await request.post()
    if not await _check_csrf(request, data):
        return _page("Error", _nav("settings") + "<p>Invalid CSRF token.</p>")
    errors: list[str] = []
    restart = False
    async with Session() as s:
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

        support = str(data.get("support", "")).strip()
        if support:
            handles = support.split()
            if all(h.startswith("@") and len(h) >= 5 for h in handles):
                await set_setting(s, "support", " ".join(handles))
            else:
                errors.append("support handles must each start with @")

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

    if errors:
        return _page("Settings", _nav("settings")
                     + "<p style='color:#f85149'>Not saved: "
                     + _esc("; ".join(errors)) + "</p><p><a href=/settings>Back</a></p>")
    if restart:
        # write is committed; exit so systemd restarts with the new token
        asyncio.get_running_loop().call_later(1.0, os._exit, 0)
        return _page("Restarting", "<h1>✅ Saved — restarting the bot…</h1>"
                     "<p>The new bot token is applied on restart. This page will be "
                     "back in a few seconds.</p><p><a href=/settings>Back to settings</a></p>")
    raise web.HTTPFound("/settings")


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
        web.get("/broadcast", broadcast_get),
        web.post("/broadcast", broadcast_post),
        web.get("/settings", settings_get),
        web.post("/settings", settings_post),
        web.get("/order/{id:\\d+}", order_detail),
        web.post("/order/{id:\\d+}/done", _order_action(complete_order)),
        web.post("/order/{id:\\d+}/refund", _order_action(refund_order)),
        web.post("/order/{id:\\d+}/reject", _order_action(reject_refund)),
        web.post("/order/{id:\\d+}/confirm", _order_action(confirm_deposit, needs_txid=True)),
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
