"""Money-moving order actions, shared by the Telegram admin handlers and the
web panel so both go through the exact same state machine and notifications.
Each returns (ok: bool, message: str) for the caller to surface."""

import asyncio
import html
import logging

from aiogram import Bot
from sqlalchemy import select

from . import texts
from .config import SERVICES
from .db import Session, get_setting, get_support
from .flow import notify_deposit_received
from .helpers import (
    ist_now_str,
    notify_admins,
    notify_user,
    try_transition,
    update_order_cards,
)
from .models import BankCard, Order, OrderStatus, SeenTx, User, utcnow

log = logging.getLogger(__name__)


async def post_proof(bot: Bot, order: Order) -> None:
    """Anonymized completion post to the public proof channel, if configured."""
    async with Session() as session:
        channel = await get_setting(session, "proof_channel")
    if not channel:
        return
    target: int | str = int(channel) if channel.lstrip("-").isdigit() else channel
    minutes = max(1, int((utcnow() - order.created_at).total_seconds() // 60))
    try:
        await bot.send_message(target, texts.proof_post(
            order.id, order.usd_amount, order.rate_inr, order.inr_amount,
            SERVICES.get(order.service, order.service), minutes))
    except Exception:
        await notify_admins(bot, "⚠️ Couldn't post to the proof channel — "
                                 "is the bot still admin there?")


async def complete_order(bot: Bot, order_id: int) -> tuple[bool, str]:
    async with Session() as session:
        order = await session.get(Order, order_id)
        if order is None:
            return False, "Order not found."
        card = await session.get(BankCard, order.bank_card_id) if order.bank_card_id else None
        updated = await try_transition(
            session, order.id, (OrderStatus.PENDING_PAYOUT,), OrderStatus.COMPLETED)
        if updated is None:
            return False, "Already handled (not awaiting payout)."
        user = await session.get(User, order.user_id)
        support = await get_support(session)
        lang = user.lang if user and user.lang else "en"
        receipt = texts.order_completed(
            order.id, order.usd_amount, order.rate_inr, order.inr_amount,
            SERVICES.get(order.service, order.service),
            card.details if card else "", ist_now_str(), lang)
        if user is not None:
            receipt += texts.trust_footer(user.first_name, user.id, support, lang)
        delivered = await notify_user(bot, order.user_id, receipt)
        await notify_admins(bot, f"✅ Order {texts.tag(order.id)} completed."
                            + ("" if delivered else " ⚠️ User DM failed (blocked bot?)."))
        if user is not None:
            await update_order_cards(bot, session, updated, user, card, None)
    await post_proof(bot, updated)
    return True, ("Done — user notified ✅" if delivered
                  else "Done, but couldn't DM the user ⚠️")


async def refund_order(bot: Bot, order_id: int) -> tuple[bool, str]:
    async with Session() as session:
        order = await session.get(Order, order_id)
        if order is None:
            return False, "Order not found."
        card = await session.get(BankCard, order.bank_card_id) if order.bank_card_id else None
        if order.status == OrderStatus.CANCELLED:
            return False, "No refund address from the user yet."
        updated = await try_transition(
            session, order.id, (OrderStatus.REFUND_REQUESTED,), OrderStatus.REFUNDED)
        if updated is None:
            return False, "Already handled."
        user = await session.get(User, order.user_id)
        lang = user.lang if user and user.lang else "en"
        delivered = await notify_user(
            bot, order.user_id,
            texts.refund_sent(order.id, order.usd_amount, order.refund_address, lang))
        await notify_admins(bot, f"💸 Order {texts.tag(order.id)} refunded.")
        if user is not None:
            await update_order_cards(bot, session, updated, user, card, None)
    return True, ("Refund marked sent ✅" if delivered
                  else "Refund marked, but couldn't DM the user ⚠️")


def compose_announcement(raw_text: str) -> str:
    """Wrap an admin's plain message as a safe HTML announcement."""
    return "📢 <b>Announcement</b>\n\n" + html.escape(raw_text.strip())


async def broadcast(bot: Bot, text: str, to_proof: bool = False) -> tuple[int, int]:
    """Send `text` to every non-banned user (throttled), and optionally to the
    proof channel. Returns (sent, failed)."""
    async with Session() as session:
        user_ids = (await session.scalars(
            select(User.id).where(User.banned.is_(False)))).all()
    sent = failed = 0
    for uid in user_ids:
        try:
            await bot.send_message(uid, text)
            sent += 1
        except Exception:
            failed += 1  # user blocked the bot, deactivated, etc.
        await asyncio.sleep(0.05)  # ~20/sec, comfortably under Telegram's limit
    if to_proof:
        async with Session() as session:
            channel = await get_setting(session, "proof_channel")
        if channel:
            target: int | str = int(channel) if channel.lstrip("-").isdigit() else channel
            try:
                await bot.send_message(target, text)
            except Exception:
                log.exception("broadcast to proof channel failed")
    return sent, failed


_bg_tasks: set = set()


def launch_broadcast(bot: Bot, text: str, to_proof: bool) -> None:
    """Fire-and-forget broadcast that DMs the admins a summary when done."""
    async def _run():
        sent, failed = await broadcast(bot, text, to_proof)
        extra = " · posted to proof channel" if to_proof else ""
        await notify_admins(bot, f"📢 Broadcast done — sent {sent}, failed {failed}"
                                 f"{extra}.")
    task = asyncio.create_task(_run())
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


async def confirm_deposit(bot: Bot, order_id: int, txid: str = "manual") -> tuple[bool, str]:
    async with Session() as session:
        if txid != "manual":
            seen = await session.get(SeenTx, txid)
            if seen is not None and seen.order_id is None:
                seen.order_id = order_id
                await session.commit()
        order = await try_transition(
            session, order_id,
            (OrderStatus.AWAITING_DEPOSIT, OrderStatus.EXPIRED),
            OrderStatus.DEPOSIT_RECEIVED, txid=txid, deposit_detected_at=utcnow())
    if order is None:
        return False, "That order isn't awaiting (or expired)."
    await notify_deposit_received(bot, order_id)
    return True, "Deposit confirmed — the user is choosing their bank."
