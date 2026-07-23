import math

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from .. import texts
from ..config import SERVICES, settings
from ..db import Session, get_deposit_address, get_rates, set_setting
from ..helpers import (
    IsAdmin,
    esc,
    is_trc20,
    notify_admins,
    notify_user,
    order_card,
    status_str,
    strip_kb,
    try_transition,
)
from ..keyboards import AdminCb, admin_order_kb
from ..models import OPEN_STATUSES, BankCard, Order, OrderMsg, OrderStatus, User

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
        "/orders — list open orders\n"
        "/order 12 — reshow an order card with its buttons\n"
        "/setstatus 12 completed — force an order's status (repair tool)\n"
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
    await message.answer(f"✅ Deposit address set:\n<code>{esc(address)}</code>")


@router.message(Command("orders"))
async def cmd_orders(message: Message) -> None:
    async with Session() as session:
        orders = (await session.scalars(
            select(Order).where(Order.status.in_(OPEN_STATUSES)).order_by(Order.id)
        )).all()
    if not orders:
        await message.answer("No open orders. 🎉")
        return
    lines = ["<b>Open orders</b>"]
    for o in orders:
        status = o.status.value if hasattr(o.status, "value") else str(o.status)
        lines.append(f"#{o.id} — {o.usd_amount:g}$ via {SERVICES.get(o.service, o.service)} "
                     f"→ ₹{o.inr_amount:,.2f} — <i>{status}</i>")
    lines.append("\nUse /order &lt;id&gt; for the card + buttons.")
    await message.answer("\n".join(lines))


@router.message(Command("order"))
async def cmd_order(message: Message, command: CommandObject) -> None:
    try:
        order_id = int((command.args or "").strip())
    except ValueError:
        await message.answer("Usage: <code>/order 12</code>")
        return
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
    await message.answer(f"✅ Order #{order.id} forced to <b>{parts[1].lower()}</b>. "
                         f"Use /order {order.id} for its buttons.")


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
                (OrderStatus.SUBMITTED, OrderStatus.USDT_SENT), OrderStatus.COMPLETED)
            if updated is None:
                await callback.answer("Already handled.", show_alert=True)
                return
            delivered = await notify_user(
                callback.bot, order.user_id,
                texts.order_completed(order.id, order.inr_amount,
                                      SERVICES.get(order.service, order.service),
                                      card.details if card else ""))
            await callback.answer("Done — user notified ✅" if delivered
                                  else "Done, but couldn't DM the user ⚠️", show_alert=not delivered)
            await notify_admins(callback.bot, f"✅ Order #{order.id} completed."
                                + ("" if delivered else " ⚠️ User DM failed (blocked bot?)."))

        elif callback_data.action == "refunded":
            if order.status == OrderStatus.CANCELLED:
                await callback.answer("No refund address from the user yet.", show_alert=True)
                return
            updated = await try_transition(
                session, order.id, (OrderStatus.REFUND_REQUESTED,), OrderStatus.REFUNDED)
            if updated is None:
                await callback.answer("Already handled.", show_alert=True)
                return
            delivered = await notify_user(
                callback.bot, order.user_id,
                texts.refund_sent(order.id, order.usd_amount, order.refund_address))
            await callback.answer("Refund marked sent ✅" if delivered
                                  else "Refund marked, but couldn't DM the user ⚠️",
                                  show_alert=not delivered)
            await notify_admins(callback.bot, f"💸 Order #{order.id} refunded.")

        else:
            await callback.answer()

    await strip_kb(callback.message)


@router.message(StateFilter(None), F.reply_to_message)
async def relay_to_user(message: Message) -> None:
    """Admin replies to an order card → the bot forwards that reply (text or
    screenshot) straight to the order's user."""
    async with Session() as session:
        link = await session.scalar(
            select(OrderMsg).where(OrderMsg.chat_id == message.chat.id,
                                   OrderMsg.message_id == message.reply_to_message.message_id))
        if link is None:
            return
        order = await session.get(Order, link.order_id)
    if order is None:
        return
    try:
        await message.bot.copy_message(chat_id=order.user_id,
                                       from_chat_id=message.chat.id,
                                       message_id=message.message_id)
        await message.reply(f"📨 Delivered to the user of order #{order.id}.")
    except Exception:
        await message.reply("⚠️ Couldn't deliver — the user may have blocked the bot.")
