"""Deposit-confirmed step shared by the auto-scanner and the manual
/received fallback: tell the user their USDT landed and ask for the bank."""

import logging

from aiogram import Bot
from sqlalchemy import select

from . import texts
from .db import Session, get_support
from .helpers import notify_admins, notify_user
from .keyboards import bank_chooser_kb
from .models import BankCard, Order, User

log = logging.getLogger(__name__)


async def notify_deposit_received(bot: Bot, order_id: int) -> None:
    async with Session() as session:
        order = await session.get(Order, order_id)
        if order is None:
            return
        user = await session.get(User, order.user_id)
        cards = (await session.scalars(
            select(BankCard).where(BankCard.user_id == order.user_id)
            .order_by(BankCard.id)
        )).all()
        support = await get_support(session)
    lang = user.lang if user and user.lang else "en"
    text = texts.deposit_received(order.id, order.usd_amount, order.inr_amount,
                                  order.txid or "", lang)
    if user is not None:
        text += texts.trust_footer(user.first_name, user.id, support, lang)
    delivered = await notify_user(bot, order.user_id, text,
                                  reply_markup=bank_chooser_kb(order.id, cards))
    await notify_admins(
        bot,
        f"📥 Deposit confirmed for Order {texts.tag(order.id)} — "
        f"{order.usd_amount:g} USDT (tx <code>{(order.txid or '')[:12]}…</code>). "
        + ("Waiting for the user's bank choice." if delivered
           else "⚠️ Couldn't DM the user (blocked bot?)."))
