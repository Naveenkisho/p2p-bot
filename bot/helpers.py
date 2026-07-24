import html
import logging
import re
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from . import texts
from .config import SERVICES, settings
from .models import BankCard, Order, OrderMsg, OrderStatus, SeenTx, User

log = logging.getLogger(__name__)

TRC20_RE = re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33}$")
TXID_RE = re.compile(r"^[0-9a-fA-F]{64}$")   # TRON transaction hash


def tronscan_tx(txid: str) -> str:
    return f"https://tronscan.org/#/transaction/{txid}"


async def txid_used_elsewhere(session: AsyncSession, txid: str,
                              exclude_order_id: int) -> int | None:
    """Return the id of another order this TXID is already tied to — as a
    confirmed deposit (Order.txid), a pending claim (claim_txid), or a refund
    (refund_txid), or via the scanner's seen-tx ledger (a deposit already
    credited to some order). Returns None if the TXID is free to use for
    `exclude_order_id`. This is what stops one on-chain transfer from ever
    being cashed out twice — e.g. a user cancelling a fresh order and pasting a
    TXID that already paid out an earlier one."""
    row = await session.scalar(
        select(Order.id).where(
            or_(Order.txid == txid, Order.claim_txid == txid, Order.refund_txid == txid),
            Order.id != exclude_order_id).limit(1))
    if row is not None:
        return row
    seen = await session.get(SeenTx, txid)
    if seen is not None and seen.order_id is not None and seen.order_id != exclude_order_id:
        return seen.order_id
    return None


class IsAdmin(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        if event.from_user is None:
            return False
        from .db import is_admin
        return await is_admin(event.from_user.id)


def esc(text: str | None) -> str:
    return html.escape(text or "")


def is_trc20(address: str) -> bool:
    return bool(TRC20_RE.fullmatch(address.strip()))


async def strip_kb(message) -> None:
    """Remove an inline keyboard, tolerating >48h-old (inaccessible) messages."""
    try:
        await message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


async def edit_or_send(callback, text, reply_markup=None) -> None:
    """Edit the tapped message in place (SPA-style navigation), falling back to
    a fresh message when it can't be edited (too old / inaccessible)."""
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup)
    except Exception:
        await callback.message.answer(text, reply_markup=reply_markup)


async def try_transition(session: AsyncSession, order_id: int,
                         from_statuses: tuple[OrderStatus, ...],
                         to_status: OrderStatus, **extra) -> Order | None:
    """Atomic compare-and-swap on Order.status so two concurrent taps (two
    admins, or a user racing an admin) can never both fire the same
    transition. Returns the updated Order, or None if the guard lost."""
    result = await session.execute(
        update(Order)
        .where(Order.id == order_id,
               Order.status.in_([s.value for s in from_statuses]))
        .values(status=to_status.value, **extra)
    )
    await session.commit()
    if result.rowcount == 0:
        return None
    order = await session.get(Order, order_id)
    if order is not None:
        # the Core UPDATE bypassed the identity map — reload the instance
        await session.refresh(order)
    return order


def status_str(order: Order) -> str:
    return order.status.value if hasattr(order.status, "value") else str(order.status)


def ist_now_str() -> str:
    ist = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    return ist.strftime("%d %b %Y, %I:%M %p") + " IST"


def ist_time_str() -> str:
    ist = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    return ist.strftime("%I:%M:%S %p") + " IST"


async def queue_position(session: AsyncSession, order_id: int) -> int:
    """1-based position in the payout queue (funds in, bank chosen)."""
    ahead = await session.scalar(
        select(func.count()).select_from(Order).where(
            Order.status == OrderStatus.PENDING_PAYOUT.value,
            Order.id < order_id))
    return (ahead or 0) + 1


def age_str(created_at) -> str:
    from .models import utcnow
    mins = max(0, int((utcnow() - created_at).total_seconds() // 60))
    return f"{mins}m" if mins < 60 else f"{mins // 60}h{mins % 60:02d}m"


def user_line(user: User) -> str:
    name_link = f'<a href="tg://user?id={user.id}">{esc(user.first_name) or "user"}</a>'
    handle = f' · <a href="https://t.me/{esc(user.username)}">@{esc(user.username)}</a>' \
        if user.username else " · no username"
    return f"{name_link}{handle}"


def order_card(order: Order, user: User, bank: BankCard | None) -> str:
    """Admin-side order card in copy-paste mode: every field an admin needs
    to paste into a banking app sits in its own tap-to-copy block."""
    service = SERVICES.get(order.service, order.service)
    lines = [
        f"🆕 <b>Order {texts.tag(order.id)}</b> — SELL <b>{order.usd_amount:g}$</b> via {service}",
        f"👤 {user_line(user)}",
        f'🆔 Chat ID: <code>{user.id}</code> · 💬 <a href="tg://user?id={user.id}">Open DM</a>',
    ]
    if user.banned:
        lines.append("🚫 <b>BANNED USER — do not pay without checking!</b>")
    lines.append(f"💱 1$/₹{order.rate_inr:g} → pay <b>₹{order.inr_amount:,.2f}</b>")
    if bank is not None:
        lines.append(f"🏦 Payout bank:\n<code>{esc(bank.details)}</code>")
    elif order.admin_note == "manual settlement":
        lines.append("🏦 Payout: ✍️ manual settlement (off-bot)")
    elif status_str(order) in ("awaiting_deposit", "deposit_received"):
        lines.append("🏦 Payout bank: not chosen yet")
    else:
        lines.append("🏦 Payout bank: ⚠️ deleted by the user")
    lines.append(f"📥 Deposit: <code>{esc(order.deposit_address)}</code>")
    if order.txid:
        lines.append(f"🔗 TX: <code>{esc(order.txid)}</code>")
    if order.refund_txid:
        lines.append("")
        lines.append("↩️ <b>REFUND REQUEST</b>")
        lines.append(f"TXID: <code>{esc(order.refund_txid)}</code>")
        lines.append(f"🔎 Verify: {tronscan_tx(esc(order.refund_txid))}")
        lines.append(f"⚠️ <b>Refund ONLY to the address this TX came FROM</b> "
                     f"(shown on Tronscan). Never to a typed address. "
                     f"Order was {order.usd_amount:g}$.")
    lines.append(f"Status: <b>{status_str(order)}</b>")
    lines.append("💬 Reply to this message to DM the user (text or screenshot).")
    return "\n".join(lines)


async def post_order_card(bot: Bot, session: AsyncSession, order: Order,
                          user: User, bank: BankCard | None,
                          reply_markup: InlineKeyboardMarkup | None) -> bool:
    """Send/refresh the order card to the admin group (or every admin DM) and
    remember the message ids so replies to any card reach the user.
    Returns False when NO admin target received the card."""
    from .db import get_admin_targets
    text = order_card(order, user, bank)
    targets = await get_admin_targets(session)
    delivered = False
    for chat_id in targets:
        try:
            msg = await bot.send_message(chat_id, text, reply_markup=reply_markup)
            session.add(OrderMsg(order_id=order.id, chat_id=chat_id, message_id=msg.message_id))
            delivered = True
        except Exception:
            log.exception("failed to post order card to %s", chat_id)
    await session.commit()
    return delivered


async def update_order_cards(bot: Bot, session: AsyncSession, order: Order,
                             user: User, bank: BankCard | None,
                             reply_markup: InlineKeyboardMarkup | None) -> None:
    """Edit every previously posted admin card for this order so stale cards
    never show an outdated status (e.g. a live Done button after a cancel)."""
    text = order_card(order, user, bank)
    rows = (await session.scalars(
        select(OrderMsg).where(OrderMsg.order_id == order.id))).all()
    for row in rows:
        try:
            await bot.edit_message_text(text, chat_id=row.chat_id,
                                        message_id=row.message_id,
                                        reply_markup=reply_markup)
        except Exception:
            pass  # message too old to edit, deleted, or unchanged — all fine


async def notify_admins(bot: Bot, text: str) -> None:
    from .db import Session, get_admin_targets
    async with Session() as session:
        targets = await get_admin_targets(session)
    for chat_id in targets:
        try:
            await bot.send_message(chat_id, text)
        except Exception:
            log.exception("failed to notify admin chat %s", chat_id)


async def notify_user(bot: Bot, user_id: int, text: str,
                      reply_markup: InlineKeyboardMarkup | None = None) -> bool:
    """Returns False when the message could not be delivered (e.g. the user
    blocked the bot) so callers can surface that instead of claiming success."""
    try:
        await bot.send_message(user_id, text, reply_markup=reply_markup)
        return True
    except Exception:
        log.exception("failed to notify user %s", user_id)
        return False
