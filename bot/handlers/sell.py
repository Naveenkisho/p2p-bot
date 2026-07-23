import re

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from .. import texts
from ..config import SERVICES, settings
from ..db import Session, get_deposit_address, get_or_create_user, get_rates
from ..helpers import is_trc20, notify_admins, post_order_card
from ..keyboards import (
    BankCb,
    OrderCb,
    admin_order_kb,
    choose_bank_kb,
    order_placed_kb,
    order_sent_kb,
    services_kb,
)
from ..models import BankCard, Order, OrderStatus, User, utcnow
from ..states import RefundFlow, SellFlow
from .start import make_bank_label

router = Router(name="sell")

AMOUNT_RE = re.compile(r"^\$?\s*(\d{1,7}(?:\.\d{1,2})?)\s*\$?$")


@router.callback_query(F.data == "menu:sell")
async def sell_menu(callback: CallbackQuery, state: FSMContext) -> None:
    async with Session() as session:
        user = await get_or_create_user(session, callback.from_user.id,
                                        callback.from_user.username, callback.from_user.first_name)
        rates = await get_rates(session)
        address = await get_deposit_address(session)
    if user.banned:
        await callback.answer(texts.BANNED, show_alert=True)
        return
    if not rates or not address:
        if not address:
            await notify_admins(callback.bot,
                                "⚠️ A user tried to sell but no deposit address is set — "
                                "run /setaddress T…")
        await callback.answer(texts.DESK_CLOSED, show_alert=True)
        return
    await state.clear()
    await callback.message.answer(texts.services_header(rates), reply_markup=services_kb(rates))
    await callback.answer()


@router.callback_query(F.data.startswith("svc:"))
async def sell_service(callback: CallbackQuery, state: FSMContext) -> None:
    key = callback.data.split(":", 1)[1]
    async with Session() as session:
        rates = await get_rates(session)
    if key not in rates:
        await callback.answer("That service is unavailable right now.", show_alert=True)
        return
    await state.clear()
    await state.update_data(service=key, rate=rates[key])
    await state.set_state(SellFlow.amount)
    await callback.message.answer(texts.ask_amount(SERVICES[key], rates[key]))
    await callback.answer()


@router.message(SellFlow.amount, F.text)
async def sell_amount(message: Message, state: FSMContext) -> None:
    m = AMOUNT_RE.match(message.text.replace(",", "").strip())
    if not m:
        await message.answer("Please send just the amount as a number, e.g. <code>100</code>.")
        return
    usd = float(m.group(1))
    if not (settings.min_usd <= usd <= settings.max_usd):
        await message.answer(f"Amount must be between {settings.min_usd:g}$ "
                             f"and {settings.max_usd:g}$.")
        return
    data = await state.get_data()
    await state.update_data(usd=usd, inr=usd * data["rate"])
    async with Session() as session:
        cards = (await session.scalars(
            select(BankCard).where(BankCard.user_id == message.from_user.id)
            .order_by(BankCard.id)
        )).all()
    if not cards:
        await state.set_state(SellFlow.bank_details)
        await message.answer(texts.ASK_BANK_FIRST)
    else:
        await state.set_state(SellFlow.choose_bank)
        await message.answer(texts.CHOOSE_BANK, reply_markup=choose_bank_kb(cards))


@router.message(SellFlow.bank_details, F.text)
async def sell_bank_details(message: Message, state: FSMContext) -> None:
    details = message.text.strip()
    if len(details.splitlines()) < 3:
        await message.answer("Please send bank name, account holder, account number "
                             "and IFSC — one per line.")
        return
    async with Session() as session:
        card = BankCard(user_id=message.from_user.id,
                        label=make_bank_label(details), details=details)
        session.add(card)
        await session.commit()
        card_id = card.id
    await _place_order(message, state, card_id=card_id, user_id=message.from_user.id)


@router.callback_query(SellFlow.choose_bank, BankCb.filter())
async def sell_choose_bank(callback: CallbackQuery, callback_data: BankCb,
                           state: FSMContext) -> None:
    if callback_data.card_id == 0:
        await state.set_state(SellFlow.bank_details)
        await callback.message.answer(texts.ASK_BANK_NEW)
        await callback.answer()
        return
    async with Session() as session:
        card = await session.get(BankCard, callback_data.card_id)
    if card is None or card.user_id != callback.from_user.id:
        await callback.answer("Bank not found.", show_alert=True)
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    await _place_order(callback.message, state, card_id=card.id,
                       user_id=callback.from_user.id)
    await callback.answer()


async def _place_order(message: Message, state: FSMContext,
                       card_id: int, user_id: int) -> None:
    """Create the order, show the user the deposit address, post the admin card.

    `message` may be the bot's own message (callback path), so the acting user
    always comes in via `user_id`.
    """
    data = await state.get_data()
    await state.clear()
    if "usd" not in data:
        await message.answer("That session expired — tap 💵 USDT Sell to start over.")
        return
    async with Session() as session:
        address = await get_deposit_address(session)
        if not address:
            await message.answer(texts.DESK_CLOSED)
            await notify_admins(message.bot, "⚠️ Order blocked — no deposit address set "
                                             "(/setaddress T…).")
            return
        card = await session.get(BankCard, card_id)
        user = await session.get(User, user_id)
        order = Order(
            user_id=user_id,
            side="sell",
            service=data["service"],
            usd_amount=data["usd"],
            rate_inr=data["rate"],
            inr_amount=data["inr"],
            bank_card_id=card_id,
            deposit_address=address,
        )
        session.add(order)
        await session.commit()
        await message.answer(
            texts.order_placed(order.id, order.usd_amount, order.inr_amount,
                               SERVICES[order.service], card.label, address),
            reply_markup=order_placed_kb(order.id),
        )
        await post_order_card(message.bot, session, order, user, card,
                              admin_order_kb(order.id, "submitted"))


@router.callback_query(OrderCb.filter())
async def order_action(callback: CallbackQuery, callback_data: OrderCb,
                       state: FSMContext) -> None:
    async with Session() as session:
        order = await session.get(Order, callback_data.order_id)
        if order is None or order.user_id != callback.from_user.id:
            await callback.answer("Order not found.", show_alert=True)
            return

        if callback_data.action == "sent":
            if order.status != OrderStatus.SUBMITTED:
                await callback.answer("This order is already in processing.", show_alert=True)
                return
            order.status = OrderStatus.USDT_SENT
            await session.commit()
            card = await session.get(BankCard, order.bank_card_id)
            try:
                await callback.message.edit_reply_markup(
                    reply_markup=order_sent_kb(order.id))
            except Exception:
                pass
            await callback.message.answer(texts.order_submitted(card.details))
            await notify_admins(callback.bot,
                                f"📤 Order #{order.id}: user says the USDT is sent "
                                f"({order.usd_amount:g}$).")
            await callback.answer()

        elif callback_data.action == "cancel":
            if order.status not in (OrderStatus.SUBMITTED, OrderStatus.USDT_SENT):
                await callback.answer("This order can no longer be cancelled.", show_alert=True)
                return
            age = (utcnow() - order.created_at).total_seconds()
            if age > settings.cancel_window_sec:
                await callback.answer(texts.CANCEL_WINDOW_OVER, show_alert=True)
                return
            order.status = OrderStatus.CANCELLED
            await session.commit()
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            await state.clear()
            await state.set_state(RefundFlow.address)
            await state.update_data(order_id=order.id)
            await callback.message.answer(texts.order_cancelled(order.id))
            await notify_admins(callback.bot,
                                f"🚫 Order #{order.id} CANCELLED by the user — "
                                f"awaiting their refund address.")
            await callback.answer("Cancelled")

        else:
            await callback.answer()


@router.message(RefundFlow.address, F.text)
async def refund_address(message: Message, state: FSMContext) -> None:
    address = message.text.strip()
    if not is_trc20(address):
        await message.answer("That doesn't look like a TRC20 address — it starts with "
                             "<code>T</code> and is 34 characters. Try again, or /cancel.")
        return
    data = await state.get_data()
    await state.clear()
    async with Session() as session:
        order = await session.get(Order, data["order_id"])
        if order is None or order.user_id != message.from_user.id:
            await message.answer("Order not found — contact support.")
            return
        order.refund_address = address
        order.status = OrderStatus.REFUND_REQUESTED
        await session.commit()
        user = await session.get(User, order.user_id)
        card = await session.get(BankCard, order.bank_card_id)
        await message.answer(texts.refund_noted(order.usd_amount, address))
        await post_order_card(message.bot, session, order, user, card,
                              admin_order_kb(order.id, "refund_requested"))


@router.callback_query(BankCb.filter())
async def stale_bank_tap(callback: CallbackQuery) -> None:
    # A bank button tapped outside the checkout flow (state expired/cleared).
    await callback.answer("That session expired — tap 💵 USDT Sell to start over.",
                          show_alert=True)
