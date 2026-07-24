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
from .db import Session, get_rates, get_setting, get_support
from .flow import notify_deposit_received
from .scanner import _ms
from .keyboards import admin_order_kb, bot_link_kb
from .helpers import (
    ist_now_str,
    notify_admins,
    notify_user,
    post_order_card,
    queue_position,
    try_transition,
    txid_used_elsewhere,
    update_order_cards,
)
from .models import BankCard, Order, OrderStatus, SeenTx, User, utcnow

log = logging.getLogger(__name__)

_bot_username: str | None = None


async def bot_username(bot: Bot) -> str | None:
    """Cached bot @username, for the 'open bot' button on proof posts."""
    global _bot_username
    if _bot_username is None:
        try:
            _bot_username = (await bot.get_me()).username
        except Exception:
            return None
    return _bot_username


async def post_proof(bot: Bot, order: Order) -> None:
    """Anonymized completion post to the public proof channel, if configured."""
    async with Session() as session:
        channel = await get_setting(session, "proof_channel")
    if not channel:
        return
    target: int | str = int(channel) if channel.lstrip("-").isdigit() else channel
    minutes = max(1, int((utcnow() - order.created_at).total_seconds() // 60))
    kb = bot_link_kb(await bot_username(bot))
    try:
        await bot.send_message(target, texts.proof_post(
            order.id, order.usd_amount, order.rate_inr, order.inr_amount,
            SERVICES.get(order.service, order.service), minutes), reply_markup=kb)
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


async def record_manual_order(bot: Bot, user_id: int, usd: float,
                              method: str) -> tuple[bool, str]:
    """Record a settlement done outside the normal flow. The admin picks the
    user, the $ amount and the method; the bot computes INR from that method's
    live rate, marks a completed order, DMs the customer their receipt and posts
    the anonymized proof — so manual payments are recorded exactly like the
    auto-scanned ones. Returns (ok, message)."""
    method = (method or "").upper()
    if method not in SERVICES:
        return False, f"Unknown method '{method}'. Use one of: {', '.join(SERVICES)}."
    try:
        usd = float(usd)
    except (TypeError, ValueError):
        return False, "Amount must be a number."
    if not (usd > 0):
        return False, "Amount must be greater than 0."
    async with Session() as session:
        user = await session.get(User, user_id)
        if user is None:
            return False, (f"No user with id {user_id} has started the bot yet — "
                           "they must open it once (/start) so it can DM them.")
        rates = await get_rates(session)
        if method not in rates:
            return False, (f"{method} has no live rate. Set it first with "
                           f"/setrate {method} <₹>.")
        rate = rates[method]
        order = Order(user_id=user_id, side="sell", service=method,
                      usd_amount=usd, rate_inr=rate, inr_amount=usd * rate,
                      status=OrderStatus.PENDING_PAYOUT.value,
                      deposit_address="manual", txid="manual",
                      deposit_detected_at=utcnow(), admin_note="manual settlement")
        session.add(order)
        await session.commit()
        order_id = order.id
    # move it into ACTIVE (the payout queue) with the same notifications an
    # auto-detected deposit gets — the admin then taps Done to complete it
    # (which is when the receipt + channel proof go out).
    async with Session() as session:
        order = await session.get(Order, order_id)
        user = await session.get(User, user_id)
        support = await get_support(session)
        lang = user.lang if user and user.lang else "en"
        position = await queue_position(session, order_id)
        text = texts.deposit_verified(order_id, order.usd_amount, order.inr_amount,
                                      "manual", SERVICES.get(method, method), position, lang)
        if user is not None:
            text += texts.trust_footer(user.first_name, user.id, support, lang)
        delivered = await notify_user(bot, user_id, text)
        posted = await post_order_card(bot, session, order, user, None,
                                       admin_order_kb(order_id, "pending_payout"))
    tail = ""
    if not delivered:
        tail += " ⚠️ Couldn't DM the customer."
    if not posted:
        tail += f" ⚠️ Card post failed — run /order {order_id}."
    return True, (f"✅ Manual order {texts.tag(order_id)} created — "
                  f"₹{usd * rate:,.2f} via {SERVICES.get(method, method)}. "
                  f"It's in the Active tab; tap Done when you've paid.{tail}")


async def refund_order(bot: Bot, order_id: int) -> tuple[bool, str]:
    async with Session() as session:
        order = await session.get(Order, order_id)
        if order is None:
            return False, "Order not found."
        card = await session.get(BankCard, order.bank_card_id) if order.bank_card_id else None
        if order.status == OrderStatus.CANCELLED:
            return False, "No refund request from the user yet (no TXID submitted)."
        updated = await try_transition(
            session, order.id, (OrderStatus.REFUND_REQUESTED,), OrderStatus.REFUNDED)
        if updated is None:
            return False, "Already handled."
        user = await session.get(User, order.user_id)
        lang = user.lang if user and user.lang else "en"
        delivered = await notify_user(bot, order.user_id,
                                      texts.refund_sent(order.id, lang))
        await notify_admins(bot, f"💸 Order {texts.tag(order.id)} refunded.")
        if user is not None:
            await update_order_cards(bot, session, updated, user, card, None)
    return True, ("Refund marked sent ✅" if delivered
                  else "Refund marked, but couldn't DM the user ⚠️")


async def reject_refund(bot: Bot, order_id: int) -> tuple[bool, str]:
    async with Session() as session:
        order = await session.get(Order, order_id)
        if order is None:
            return False, "Order not found."
        card = await session.get(BankCard, order.bank_card_id) if order.bank_card_id else None
        updated = await try_transition(
            session, order.id, (OrderStatus.REFUND_REQUESTED,), OrderStatus.REFUND_REJECTED)
        if updated is None:
            return False, "Already handled."
        user = await session.get(User, order.user_id)
        support = await get_support(session)
        lang = user.lang if user and user.lang else "en"
        delivered = await notify_user(bot, order.user_id,
                                      texts.refund_rejected(order.id, support, lang))
        await notify_admins(bot, f"🚫 Order {texts.tag(order.id)} refund rejected "
                                 "(no verified deposit).")
        if user is not None:
            await update_order_cards(bot, session, updated, user, card, None)
    return True, ("Refund rejected — user notified 🚫" if delivered
                  else "Refund rejected, but couldn't DM the user ⚠️")


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
    """Manually confirm a deposit. When a real TXID is given the amount is
    reconciled to what ACTUALLY landed on-chain — a sender's platform can deduct
    a network fee (30.13 sent → 28.63 received), so we credit and pay out the
    received amount at the order's locked method rate, never the ordered amount."""
    async with Session() as session:
        order = await session.get(Order, order_id)
        if order is None:
            return False, "Order not found."
        address, rate = order.deposit_address, order.rate_inr
        since_ms = _ms(order.created_at)
    actual = None
    warn = ""
    if txid and txid != "manual":
        from .scanner import lookup_claim_tx
        info = await lookup_claim_tx(txid, address, since_ms)
        if info.get("found") and info.get("to_ok") and (info.get("amount") or 0) > 0:
            actual = round(info["amount"], 6)
        elif info.get("found") and not info.get("to_ok"):
            return False, "⚠️ That TXID isn't a USDT transfer to your deposit address — not confirming."
        elif info.get("error"):
            warn = " ⚠️ Couldn't reach TronGrid — credited the ORDERED amount; check the actual on Tronscan."
        else:
            warn = " ⚠️ TXID not found on-chain — credited the ORDERED amount; verify on Tronscan."
    async with Session() as session:
        if txid != "manual":
            seen = await session.get(SeenTx, txid)
            if seen is not None and seen.order_id is None:
                seen.order_id = order_id
                await session.commit()
        extra = {"txid": txid, "deposit_detected_at": utcnow()}
        if actual is not None:
            extra["usd_amount"] = actual
            extra["inr_amount"] = round(actual * rate, 2)
        order = await try_transition(
            session, order_id,
            (OrderStatus.AWAITING_DEPOSIT, OrderStatus.EXPIRED, OrderStatus.CANCELLED),
            OrderStatus.DEPOSIT_RECEIVED, **extra)
    if order is None:
        return False, "That order can't be confirmed (already processing or refunded)."
    await notify_deposit_received(bot, order_id)
    if actual is not None:
        return True, (f"✅ Confirmed — actual {texts.usd_str(actual)} USDT received "
                      f"→ pay ₹{actual * rate:,.2f}. Queued for payout.")
    return True, "Deposit confirmed — order queued for payout." + warn


async def confirm_claim(bot: Bot, order_id: int) -> tuple[bool, str]:
    """Admin approves a user's payment claim (a deposit auto-detect missed or an
    order that expired before the transfer confirmed). Reuses the exact deposit
    path, so the order lands in the payout queue and the user gets the same
    'verified — funds on the way' DM as an auto-detected deposit."""
    async with Session() as session:
        order = await session.get(Order, order_id)
        if order is None:
            return False, "Order not found."
        txid = order.claim_txid
        if not txid:
            return False, "No payment claim on this order."
        # re-check at confirm time: the scanner or another claim may have tied
        # this TXID to a different order since the claim was submitted
        used = await txid_used_elsewhere(session, txid, order_id)
        if used is not None:
            return False, (f"🚫 That TXID is already tied to order {texts.tag(used)} — "
                           "not confirming (an on-chain transfer can't pay out twice).")
    ok, msg = await confirm_deposit(bot, order_id, txid)
    if ok:
        async with Session() as session:
            o = await session.get(Order, order_id)
            if o is not None:
                o.claim_txid = None      # claim resolved → clear the marker
                await session.commit()
        # msg already reports the ACTUAL amount reconciled on-chain
        return True, f"{texts.tag(order_id)} — {msg}"
    return ok, msg


async def reject_claim(bot: Bot, order_id: int) -> tuple[bool, str]:
    async with Session() as session:
        order = await session.get(Order, order_id)
        if order is None:
            return False, "Order not found."
        if not order.claim_txid:
            return False, "No pending claim on this order."
        order.claim_txid = None
        await session.commit()
        user = await session.get(User, order.user_id)
        support = await get_support(session)
        lang = user.lang if user and user.lang else "en"
    delivered = await notify_user(bot, order.user_id,
                                  texts.claim_rejected(order_id, support, lang))
    await notify_admins(bot, f"🚫 Payment claim for {texts.tag(order_id)} rejected.")
    return True, ("Claim rejected — user notified 🚫" if delivered
                  else "Claim rejected, but couldn't DM the user ⚠️")
