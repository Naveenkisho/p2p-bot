import html
import logging
import re
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from . import texts
from .config import SERVICES, settings
from .models import BankCard, Order, OrderMsg, OrderStatus, User

log = logging.getLogger(__name__)

TRC20_RE = re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33}$")


class IsAdmin(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        return event.from_user is not None and event.from_user.id in settings.admin_id_list


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
    else:
        lines.append("🏦 Payout bank: ⚠️ deleted by the user")
    lines.append(f"📥 Deposit: <code>{esc(order.deposit_address)}</code>")
    if order.refund_address:
        lines.append(f"↩️ Refund <b>{order.usd_amount:g} USDT</b> to:\n"
                     f"<code>{esc(order.refund_address)}</code>")
    lines.append(f"Status: <b>{status_str(order)}</b>")
    lines.append("💬 Reply to this message to DM the user (text or screenshot).")
    return "\n".join(lines)


async def post_order_card(bot: Bot, session: AsyncSession, order: Order,
                          user: User, bank: BankCard | None,
                          reply_markup: InlineKeyboardMarkup | None) -> None:
    """Send/refresh the order card to the admin group (or every admin DM) and
    remember the message ids so replies to any card reach the user."""
    text = order_card(order, user, bank)
    targets = [settings.admin_chat_id] if settings.admin_chat_id else settings.admin_id_list
    for chat_id in targets:
        try:
            msg = await bot.send_message(chat_id, text, reply_markup=reply_markup)
            session.add(OrderMsg(order_id=order.id, chat_id=chat_id, message_id=msg.message_id))
        except Exception:
            log.exception("failed to post order card to %s", chat_id)
    await session.commit()


async def notify_admins(bot: Bot, text: str) -> None:
    targets = [settings.admin_chat_id] if settings.admin_chat_id else settings.admin_id_list
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
