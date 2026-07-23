import html
import logging
import re

from aiogram import Bot
from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from .config import SERVICES, settings
from .models import BankCard, Order, OrderMsg, User

log = logging.getLogger(__name__)

TRC20_RE = re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33}$")


class IsAdmin(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        return event.from_user is not None and event.from_user.id in settings.admin_id_list


def esc(text: str | None) -> str:
    return html.escape(text or "")


def is_trc20(address: str) -> bool:
    return bool(TRC20_RE.fullmatch(address.strip()))


def user_line(user: User) -> str:
    handle = f"@{user.username}" if user.username else "no username"
    return f"{esc(user.first_name)} ({esc(handle)}) · id <code>{user.id}</code>"


def status_str(order: Order) -> str:
    return order.status.value if hasattr(order.status, "value") else str(order.status)


def order_card(order: Order, user: User, bank: BankCard | None) -> str:
    """Admin-side order card in copy-paste mode: every field an admin needs
    to paste into a banking app sits in its own tap-to-copy block."""
    service = SERVICES.get(order.service, order.service)
    lines = [
        f"🆕 <b>Order #{order.id}</b> — SELL <b>{order.usd_amount:g}$</b> via {service}",
        f"👤 {user_line(user)}",
        f"💱 1$/₹{order.rate_inr:g} → pay <b>₹{order.inr_amount:,.2f}</b>",
    ]
    if bank is not None:
        lines.append(f"🏦 Payout bank:\n<code>{esc(bank.details)}</code>")
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
                      reply_markup: InlineKeyboardMarkup | None = None) -> None:
    try:
        await bot.send_message(user_id, text, reply_markup=reply_markup)
    except Exception:
        log.exception("failed to notify user %s", user_id)
