"""Deposit-confirmed step shared by the auto-scanner and the manual
/received fallback.

New flow: the bank is chosen BEFORE the deposit, so when the transfer is
verified the order advances straight to the payout queue and the admin card
is posted — the first time admins see the order. The bank chooser branch
remains for legacy orders that reached deposit_received without a bank."""

import logging

from aiogram import Bot
from sqlalchemy import select

from . import texts
from .db import Session, get_support
from .helpers import (
    dm_note,
    notify_admins,
    notify_user,
    post_order_card,
    queue_position,
    try_transition,
    update_order_cards,
)
from .keyboards import admin_order_kb, bank_chooser_kb
from .models import BankCard, Order, OrderStatus, User

log = logging.getLogger(__name__)


async def notify_deposit_received(bot: Bot, order_id: int) -> None:
    async with Session() as session:
        order = await session.get(Order, order_id)
        if order is None:
            return
        user = await session.get(User, order.user_id)
        support = await get_support(session)
        lang = user.lang if user and user.lang else "en"

        if order.bank_card_id:
            # bank already chosen at checkout → verified deposit goes straight
            # to the payout queue and the admins get their first card
            card = await session.get(BankCard, order.bank_card_id)
            advanced = await try_transition(session, order.id,
                                            (OrderStatus.DEPOSIT_RECEIVED,),
                                            OrderStatus.PENDING_PAYOUT)
            if advanced is None:
                return  # already moved on (double notify) — nothing to do
            position = await queue_position(session, order.id)
            text = texts.deposit_verified(order.id, order.usd_amount,
                                          order.inr_amount, order.txid or "",
                                          card.label if card else "your bank",
                                          position, lang)
            if user is not None:
                text += texts.trust_footer(user.first_name, user.id, support, lang)
            delivered = await notify_user(bot, order.user_id, text)
            posted = await post_order_card(bot, session, advanced, user, card,
                                           admin_order_kb(order.id, "pending_payout"))
            await update_order_cards(bot, session, advanced, user, card,
                                     admin_order_kb(order.id, "pending_payout"))
            note = (f"📥 <b>Deposit verified</b> for Order {texts.tag(order.id)} — "
                    f"{order.usd_amount:g} USDT "
                    f"(tx <code>{(order.txid or '')[:12]}…</code>). "
                    f"Pay ₹{order.inr_amount:,.2f}.")
            note += dm_note(order.user_id, delivered,
                            " ⚠️ Couldn't DM the user (blocked bot?).")
            if not posted:
                note += f" ⚠️ Card post failed — run /order {order.id}."
            await notify_admins(bot, note)
            # emailed "deposit received" for any customer with a verified
            # email — web accounts and Telegram users alike (fire-and-forget)
            try:
                from . import sender
                from .config import SERVICES, settings
                email, name = await sender.email_for_uid(order.user_id)
                if email:
                    site = (settings.site_url or "").rstrip("/")
                    track = (site + f"/o/{order.web_token}"
                             if order.web_token else site or "/")
                    subj, inner = sender.deposit_received_email(
                        texts.tag(order.id), order.usd_amount, order.inr_amount,
                        SERVICES.get(order.service, order.service),
                        card.label if card else "", position, track)
                    await sender.send_transactional(
                        email, name, subj, inner,
                        fail_bot=bot, fail_uid=max(order.user_id, 0))
            except Exception:
                log.exception("deposit-received email failed")
            return

        # legacy: no bank yet → ask for it now
        cards = (await session.scalars(
            select(BankCard).where(BankCard.user_id == order.user_id)
            .order_by(BankCard.id)
        )).all()
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
           else dm_note(order.user_id, delivered,
                        "⚠️ Couldn't DM the user (blocked bot?).").strip()))
