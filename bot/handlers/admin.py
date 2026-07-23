import math

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from .. import texts
from ..config import SERVICES, settings
from ..db import Session, get_deposit_address, get_rates, get_setting, get_support, set_setting
from ..flow import notify_deposit_received
from ..helpers import (
    IsAdmin,
    age_str,
    esc,
    is_trc20,
    ist_now_str,
    ist_time_str,
    notify_admins,
    notify_user,
    order_card,
    status_str,
    strip_kb,
    try_transition,
    update_order_cards,
)
from ..keyboards import PANEL_TABS, AdminCb, admin_order_kb, panel_kb
from ..models import OPEN_STATUSES, BankCard, Order, OrderMsg, OrderStatus, SeenTx, User, utcnow

router = Router(name="admin")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


@router.message(Command("admin"))
async def admin_help(message: Message) -> None:
    await message.answer(
        "<b>Admin commands</b>\n"
        "/setrate CDM 91 — set a service's ₹/$ rate live (0 hides the service)\n"
        "/rates — show all live rates\n"
        "/setaddress T… — set the TRC20 deposit address\n"
        "/setsupport @help1 @help2 — set the support contact(s) users see\n"
        "/setchannel @channel — proof channel for completed orders (off = disable)\n"
        "/panel (or /orders) — tabbed live order panel\n"
        "/orders — list open orders\n"
        "/order 12 — reshow an order card with its buttons\n"
        "/received 12 — manually confirm a deposit (auto-scan fallback)\n"
        "/setstatus 12 completed — force an order's status (repair tool)\n"
        "/setrefund 12 T… — record a refund address for a cancelled order\n"
        "/ban 123456789 · /unban 123456789\n\n"
        "💬 Reply to any order card to DM that user through the bot "
        "(text or screenshot)."
    )


@router.message(Command("setrate"))
async def cmd_setrate(message: Message, command: CommandObject) -> None:
    parts = (command.args or "").split()
    key = parts[0].upper() if parts else ""
    if len(parts) != 2 or key not in SERVICES:
        await message.answer("Usage: <code>/setrate CDM 91</code>\n"
                             f"Services: {', '.join(SERVICES)}")
        return
    try:
        rate = float(parts[1])
    except ValueError:
        rate = -1.0
    if not math.isfinite(rate) or rate < 0 or rate > 100_000:
        await message.answer("The rate must be a normal number, e.g. <code>91</code> "
                             "(<code>0</code> hides the service).")
        return
    async with Session() as session:
        await set_setting(session, f"rate_{key}", str(rate))
    if rate == 0:
        await message.answer(f"✅ {SERVICES[key]} hidden from the sell menu.")
    else:
        await message.answer(f"✅ {SERVICES[key]} rate is now live: 1$ / ₹{rate:g}.")


@router.message(Command("rates"))
async def cmd_rates(message: Message) -> None:
    async with Session() as session:
        rates = await get_rates(session)
        address = await get_deposit_address(session)
    lines = ["<b>Live rates</b>"]
    for key, label in SERVICES.items():
        lines.append(f"{label}: " + (f"₹{rates[key]:g}/$" if key in rates else "—"))
    lines.append(f"\nDeposit address: " +
                 (f"<code>{esc(address)}</code>" if address else "⚠️ not set"))
    await message.answer("\n".join(lines))


@router.message(Command("setaddress"))
async def cmd_setaddress(message: Message, command: CommandObject) -> None:
    address = (command.args or "").strip()
    if not is_trc20(address):
        await message.answer("That's not a valid TRC20 address. "
                             "Usage: <code>/setaddress TX…</code> (34 chars, starts with T)")
        return
    async with Session() as session:
        await set_setting(session, "addr_trc20", address)
        # activation watermark: only transfers AFTER this instant can credit an
        # order, so pointing at an existing wallet never replays its history.
        now_ms = int(utcnow().replace(microsecond=0).timestamp() * 1000)
        await set_setting(session, f"addr_since:{address}", str(now_ms))
        await set_setting(session, f"bootstrapped:{address}", "1")
    await message.answer(f"✅ Deposit address set:\n<code>{esc(address)}</code>\n\n"
                         "Only deposits from now on will be auto-detected on this "
                         "address (its past history is ignored).")


@router.message(Command("setsupport"))
async def cmd_setsupport(message: Message, command: CommandObject) -> None:
    handles = (command.args or "").split()
    async with Session() as session:
        if not handles:
            current = await get_support(session)
            await message.answer("Usage: <code>/setsupport @help1 @help2</code>\n"
                                 f"Current: {esc(current)}")
            return
        if not all(h.startswith("@") and len(h) >= 5 for h in handles):
            await message.answer("Each contact must be a @username, e.g. "
                                 "<code>/setsupport @desk_help @desk_help2</code>")
            return
        await set_setting(session, "support", " ".join(handles))
    await message.answer(f"✅ Support contact(s) now shown to users: {esc(' '.join(handles))}")


@router.message(Command("setchannel"))
async def cmd_setchannel(message: Message, command: CommandObject) -> None:
    arg = (command.args or "").strip()
    if not arg:
        async with Session() as session:
            current = await get_setting(session, "proof_channel")
        await message.answer("Usage: <code>/setchannel @yourchannel</code> (bot must be "
                             "admin there) or <code>/setchannel off</code>\n"
                             f"Current: {esc(current) if current else '— none —'}")
        return
    if arg.lower() == "off":
        async with Session() as session:
            await set_setting(session, "proof_channel", "")
        await message.answer("✅ Proof channel disabled.")
        return
    if not (arg.startswith("@") or arg.lstrip("-").isdigit()):
        await message.answer("Send the channel as <code>@username</code> or a numeric "
                             "<code>-100…</code> ID.")
        return
    target: int | str = int(arg) if arg.lstrip("-").isdigit() else arg
    try:
        await message.bot.send_message(target, "✅ Proof channel connected — completed "
                                               "orders will be posted here.")
    except Exception:
        await message.answer("⚠️ Couldn't post there. Add the bot as an <b>admin</b> of "
                             "the channel first, then run /setchannel again.")
        return
    async with Session() as session:
        await set_setting(session, "proof_channel", arg)
    await message.answer(f"✅ Proof channel set to {esc(arg)} — every completed order "
                         "posts an anonymized proof card there.")


async def _post_proof(bot, order: Order) -> None:
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


TAB_STATUSES = {
    "active": (OrderStatus.AWAITING_DEPOSIT.value, OrderStatus.DEPOSIT_RECEIVED.value,
               OrderStatus.PENDING_PAYOUT.value),
    "refunds": (OrderStatus.CANCELLED.value, OrderStatus.REFUND_REQUESTED.value),
    "done": (OrderStatus.COMPLETED.value, OrderStatus.REFUNDED.value,
             OrderStatus.EXPIRED.value),
}


async def _panel_text(tab: str) -> str:
    async with Session() as session:
        query = select(Order).where(Order.status.in_(TAB_STATUSES[tab]))
        if tab == "done":
            query = query.order_by(Order.id.desc()).limit(10)
        else:
            query = query.order_by(Order.id)
        orders = (await session.scalars(query)).all()
    lines = [f"🗂 <b>Orders — {PANEL_TABS[tab]}</b> ({len(orders)})", ""]
    if not orders:
        lines.append("Nothing here. 🎉")
    for o in orders:
        status = o.status.value if hasattr(o.status, "value") else str(o.status)
        emoji = texts.STATUS_EMOJI.get(status, "•")
        lines.append(f"{emoji} {texts.tag(o.id)} — {o.usd_amount:g}$ "
                     f"{SERVICES.get(o.service, o.service)} → ₹{o.inr_amount:,.2f} "
                     f"— {age_str(o.created_at)}")
    lines.append("")
    lines.append("Open one: /order &lt;id&gt;")
    lines.append(f"🔄 Updated: {ist_time_str()}")
    return "\n".join(lines)


@router.message(Command("orders"))
@router.message(Command("panel"))
async def cmd_orders(message: Message) -> None:
    await message.answer(await _panel_text("active"), reply_markup=panel_kb("active"))


@router.callback_query(F.data.startswith("tab:"))
async def panel_tab(callback: CallbackQuery) -> None:
    tab = callback.data.split(":", 1)[1]
    if tab not in TAB_STATUSES:
        await callback.answer()
        return
    try:
        await callback.message.edit_text(await _panel_text(tab),
                                         reply_markup=panel_kb(tab))
    except Exception:
        pass  # unmodified or too old — the refresh timestamp makes this rare
    await callback.answer("Updated")


@router.message(Command("order"))
async def cmd_order(message: Message, command: CommandObject) -> None:
    raw = (command.args or "").strip().lstrip("#").upper().removeprefix("ORD")
    if not raw.isdigit():
        await message.answer("Usage: <code>/order 12</code> or <code>/order #ORD12</code>")
        return
    order_id = int(raw)
    async with Session() as session:
        order = await session.get(Order, order_id)
        if order is None:
            await message.answer("No such order.")
            return
        user = await session.get(User, order.user_id)
        card = await session.get(BankCard, order.bank_card_id) if order.bank_card_id else None
        status = order.status.value if hasattr(order.status, "value") else str(order.status)
        msg = await message.answer(order_card(order, user, card),
                                   reply_markup=admin_order_kb(order.id, status))
        session.add(OrderMsg(order_id=order.id, chat_id=msg.chat.id, message_id=msg.message_id))
        await session.commit()


@router.message(Command("setstatus"))
async def cmd_setstatus(message: Message, command: CommandObject) -> None:
    """Escape hatch for stuck orders — force a status, no guards."""
    parts = (command.args or "").split()
    valid = [s.value for s in OrderStatus]
    if len(parts) != 2 or not parts[0].isdigit() or parts[1].lower() not in valid:
        await message.answer("Usage: <code>/setstatus 12 completed</code>\n"
                             f"Statuses: {', '.join(valid)}")
        return
    async with Session() as session:
        order = await session.get(Order, int(parts[0]))
        if order is None:
            await message.answer("No such order.")
            return
        order.status = parts[1].lower()
        await session.commit()
    await message.answer(f"✅ Order {texts.tag(order.id)} forced to <b>{parts[1].lower()}</b>. "
                         f"Use /order {order.id} for its buttons.")


@router.message(Command("received"))
async def cmd_received(message: Message, command: CommandObject) -> None:
    """Manually confirm a deposit — the fallback for the ambiguous-amount hold,
    a late deposit on an already-expired order, or TronGrid being down.
    Usage: /received <id> [txid]"""
    parts = (command.args or "").split()
    raw = parts[0].lstrip("#").upper().removeprefix("ORD") if parts else ""
    if not raw.isdigit():
        await message.answer("Usage: <code>/received 12</code> or "
                             "<code>/received 12 &lt;txid&gt;</code> — confirms the "
                             "deposit and asks the user for their bank.")
        return
    txid = parts[1] if len(parts) > 1 else "manual"
    async with Session() as session:
        # if this txid was recorded as unmatched/ambiguous, tie it to this order
        if txid != "manual":
            seen = await session.get(SeenTx, txid)
            if seen is not None and seen.order_id is None:
                seen.order_id = int(raw)
                await session.commit()
        order = await try_transition(
            session, int(raw),
            (OrderStatus.AWAITING_DEPOSIT, OrderStatus.EXPIRED),
            OrderStatus.DEPOSIT_RECEIVED, txid=txid, deposit_detected_at=utcnow())
    if order is None:
        await message.answer("That order isn't awaiting (or expired) — check /order "
                             f"{raw} first.")
        return
    await notify_deposit_received(message.bot, order.id)
    await message.answer(f"✅ Deposit confirmed manually for {texts.tag(order.id)} — "
                         "the user is choosing their bank.")


@router.message(Command("setrefund"))
async def cmd_setrefund(message: Message, command: CommandObject) -> None:
    """Record a refund address on the user's behalf (e.g. received via DM)."""
    parts = (command.args or "").split()
    order_raw = parts[0].lstrip("#").upper().removeprefix("ORD") if parts else ""
    if len(parts) != 2 or not order_raw.isdigit() or not is_trc20(parts[1]):
        await message.answer("Usage: <code>/setrefund 12 T…</code> "
                             "(34-char TRC20 address)")
        return
    async with Session() as session:
        order = await session.get(Order, int(order_raw))
        if order is None:
            await message.answer("No such order.")
            return
        order.refund_address = parts[1]
        if order.status == OrderStatus.CANCELLED:
            order.status = OrderStatus.REFUND_REQUESTED.value
        await session.commit()
    await message.answer(f"✅ Refund address recorded for {texts.tag(order.id)} — "
                         f"use /order {order.id} for the Refund-sent button.")


@router.message(Command("ban"))
@router.message(Command("unban"))
async def cmd_ban(message: Message, command: CommandObject) -> None:
    try:
        user_id = int((command.args or "").strip())
    except ValueError:
        await message.answer("Usage: <code>/ban 123456789</code>")
        return
    banned = command.command == "ban"
    async with Session() as session:
        user = await session.get(User, user_id)
        if user is None:
            await message.answer("Unknown user id.")
            return
        user.banned = banned
        await session.commit()
    await message.answer(f"{'🚫 Banned' if banned else '✅ Unbanned'} user {user_id}.")


@router.callback_query(AdminCb.filter())
async def admin_order_action(callback: CallbackQuery, callback_data: AdminCb) -> None:
    async with Session() as session:
        order = await session.get(Order, callback_data.order_id)
        if order is None:
            await callback.answer("Order not found.", show_alert=True)
            return
        card = await session.get(BankCard, order.bank_card_id) if order.bank_card_id else None

        if callback_data.action == "done":
            updated = await try_transition(
                session, order.id,
                (OrderStatus.PENDING_PAYOUT,), OrderStatus.COMPLETED)
            if updated is None:
                await callback.answer("Already handled.", show_alert=True)
                return
            user = await session.get(User, order.user_id)
            support = await get_support(session)
            lang = user.lang if user and user.lang else "en"
            receipt = texts.order_completed(
                order.id, order.usd_amount, order.rate_inr, order.inr_amount,
                SERVICES.get(order.service, order.service),
                card.details if card else "", ist_now_str(), lang)
            if user is not None:
                receipt += texts.trust_footer(user.first_name, user.id, support, lang)
            delivered = await notify_user(callback.bot, order.user_id, receipt)
            await callback.answer("Done — user notified ✅" if delivered
                                  else "Done, but couldn't DM the user ⚠️", show_alert=not delivered)
            await notify_admins(callback.bot, f"✅ Order {texts.tag(order.id)} completed."
                                + ("" if delivered else " ⚠️ User DM failed (blocked bot?)."))
            if user is not None:
                await update_order_cards(callback.bot, session, updated, user, card, None)
            await _post_proof(callback.bot, updated)

        elif callback_data.action == "refunded":
            if order.status == OrderStatus.CANCELLED:
                await callback.answer("No refund address from the user yet.", show_alert=True)
                return
            updated = await try_transition(
                session, order.id, (OrderStatus.REFUND_REQUESTED,), OrderStatus.REFUNDED)
            if updated is None:
                await callback.answer("Already handled.", show_alert=True)
                return
            user = await session.get(User, order.user_id)
            lang = user.lang if user and user.lang else "en"
            delivered = await notify_user(
                callback.bot, order.user_id,
                texts.refund_sent(order.id, order.usd_amount, order.refund_address, lang))
            await callback.answer("Refund marked sent ✅" if delivered
                                  else "Refund marked, but couldn't DM the user ⚠️",
                                  show_alert=not delivered)
            await notify_admins(callback.bot, f"💸 Order {texts.tag(order.id)} refunded.")
            if user is not None:
                await update_order_cards(callback.bot, session, updated, user, card, None)

        else:
            await callback.answer()

    await strip_kb(callback.message)


@router.message(StateFilter(None), F.reply_to_message,
                lambda m: not (m.text or "").startswith("/"))
async def relay_to_user(message: Message) -> None:
    """Admin replies to an order card → the bot forwards that reply (text or
    screenshot) straight to the order's user. Commands pass through untouched;
    replies to non-bot messages (admins talking to each other) are ignored."""
    target = message.reply_to_message
    if target.from_user is None or target.from_user.id != message.bot.id:
        return
    async with Session() as session:
        link = await session.scalar(
            select(OrderMsg).where(OrderMsg.chat_id == message.chat.id,
                                   OrderMsg.message_id == target.message_id))
        if link is None:
            await message.reply("⚠️ This message isn't linked to an order — reply "
                                "directly to an order card, or run /order &lt;id&gt; "
                                "to print one.")
            return
        order = await session.get(Order, link.order_id)
        user = await session.get(User, order.user_id) if order else None
    if order is None:
        return
    try:
        if message.photo and not message.caption:
            # a bare screenshot becomes a labeled payment proof
            await message.bot.copy_message(
                chat_id=order.user_id, from_chat_id=message.chat.id,
                message_id=message.message_id,
                caption=f"🧾 Payment proof — order {texts.tag(order.id)}")
        else:
            await message.bot.copy_message(chat_id=order.user_id,
                                           from_chat_id=message.chat.id,
                                           message_id=message.message_id)
        who = f"{esc(user.first_name)} (@{esc(user.username)})" if user and user.username \
            else (esc(user.first_name) if user else "the user")
        await message.reply(f"📨 Delivered to {who} — order {texts.tag(order.id)}.")
    except Exception:
        await message.reply("⚠️ Couldn't deliver — the user may have blocked the bot.")
