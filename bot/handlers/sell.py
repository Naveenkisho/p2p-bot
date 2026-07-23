import re

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from .. import texts
from ..config import SERVICES, settings
from ..db import Session, get_deposit_address, get_or_create_user, get_rates, get_support
from ..helpers import (
    TRC20_RE,
    is_trc20,
    notify_admins,
    post_order_card,
    strip_kb,
    try_transition,
    update_order_cards,
)
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
from .start import bank_details_error, make_bank_label

router = Router(name="sell")

AMOUNT_RE = re.compile(r"^\$?\s*(\d{1,7}(?:\.\d{1,2})?)\s*\$?$")
TRC20_PATTERN = TRC20_RE.pattern

# one admin ping per process for the "no deposit address" misconfig,
# so users tapping Sell can't spam the admin chat
_warned_no_address = False


async def _warn_no_address_once(bot) -> None:
    global _warned_no_address
    if not _warned_no_address:
        _warned_no_address = True
        await notify_admins(bot, "⚠️ A user tried to sell but no deposit address "
                                 "is set — run /setaddress T…")


async def _footer(session, tg_user) -> str:
    """Trust footer (name + ID + support) appended to every flow step."""
    support = await get_support(session)
    return texts.trust_footer(tg_user.first_name, tg_user.id, support)


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
            await _warn_no_address_once(callback.bot)
        await callback.answer(texts.DESK_CLOSED, show_alert=True)
        return
    async with Session() as session:
        footer = await _footer(session, callback.from_user)
    await state.clear()
    await callback.message.answer(texts.services_header(rates) + footer,
                                  reply_markup=services_kb(rates))
    await callback.answer()


@router.callback_query(F.data.startswith("svc:"))
async def sell_service(callback: CallbackQuery, state: FSMContext) -> None:
    key = callback.data.split(":", 1)[1]
    async with Session() as session:
        rates = await get_rates(session)
        footer = await _footer(session, callback.from_user)
    if key not in rates:
        await callback.answer("That service is unavailable right now.", show_alert=True)
        return
    await state.clear()
    await state.update_data(service=key, rate=rates[key])
    await state.set_state(SellFlow.amount)
    await callback.message.answer(texts.ask_amount(SERVICES[key], rates[key]) + footer)
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
    inr = usd * data["rate"]
    await state.update_data(usd=usd, inr=inr)
    async with Session() as session:
        cards = (await session.scalars(
            select(BankCard).where(BankCard.user_id == message.from_user.id)
            .order_by(BankCard.id)
        )).all()
        footer = await _footer(session, message.from_user)
    quote = texts.quote_block(usd, inr, SERVICES[data["service"]], data["rate"])
    if not cards:
        await state.set_state(SellFlow.bank_details)
        await message.answer(quote + "\n\n" + texts.ASK_BANK_FIRST + footer)
    else:
        await state.set_state(SellFlow.choose_bank)
        await message.answer(quote + "\n\n" + texts.CHOOSE_BANK + footer,
                             reply_markup=choose_bank_kb(cards))


@router.message(SellFlow.bank_details, F.text)
async def sell_bank_details(message: Message, state: FSMContext) -> None:
    details = message.text.strip()
    error = bank_details_error(details)
    if error:
        await message.answer(error)
        return
    async with Session() as session:
        card = BankCard(user_id=message.from_user.id,
                        label=make_bank_label(details), details=details)
        session.add(card)
        await session.commit()
        card_id = card.id
    await _place_order(message, state, card_id=card_id, user_id=message.from_user.id)


@router.message(SellFlow.amount)
async def sell_amount_not_text(message: Message) -> None:
    await message.answer("Please type the amount as a number, e.g. <code>100</code>.")


@router.message(SellFlow.bank_details)
async def sell_bank_not_text(message: Message) -> None:
    await message.answer("Please <b>type</b> your bank details as text — "
                         "not a photo or file.")


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
    await strip_kb(callback.message)
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
            await _warn_no_address_once(message.bot)
            return
        card = await session.get(BankCard, card_id)
        user = await session.get(User, user_id)
        if card is None or user is None:
            await message.answer("That session expired — tap 💵 USDT Sell to start over.")
            return
        if user.banned:
            await message.answer(texts.BANNED)
            return
        # the quote was locked at service pick, but a user can park mid-flow
        # for hours — re-check the live rate at the money moment
        rates = await get_rates(session)
        rate = data["rate"]
        rate_note = ""
        if data["service"] not in rates:
            await message.answer(texts.DESK_CLOSED)
            return
        if rates[data["service"]] != rate:
            rate = rates[data["service"]]
            rate_note = f"📈 Rate updated since your quote: <b>1$ / ₹{rate:g}</b>\n\n"
        order = Order(
            user_id=user_id,
            side="sell",
            service=data["service"],
            usd_amount=data["usd"],
            rate_inr=rate,
            inr_amount=data["usd"] * rate,
            bank_card_id=card_id,
            deposit_address=address,
        )
        session.add(order)
        await session.commit()
        footer = await _footer(session, user)
        await message.answer(
            texts.order_placed(order.id, order.usd_amount, order.inr_amount,
                               SERVICES[order.service], card.label, address,
                               rate, rate_note) + footer,
            reply_markup=order_placed_kb(order.id),
        )
        posted = await post_order_card(message.bot, session, order, user, card,
                                       admin_order_kb(order.id, "submitted"))
        if not posted:
            await notify_admins(message.bot,
                                f"⚠️ Couldn't post the card for Order {texts.tag(order.id)} "
                                f"— run /order {order.id}.")


@router.callback_query(OrderCb.filter())
async def order_action(callback: CallbackQuery, callback_data: OrderCb,
                       state: FSMContext) -> None:
    async with Session() as session:
        order = await session.get(Order, callback_data.order_id)
        if order is None or order.user_id != callback.from_user.id:
            await callback.answer("Order not found.", show_alert=True)
            return

        if callback_data.action == "sent":
            updated = await try_transition(session, order.id,
                                           (OrderStatus.SUBMITTED,), OrderStatus.USDT_SENT)
            if updated is None:
                await callback.answer("This order is already in processing.", show_alert=True)
                return
            card = await session.get(BankCard, order.bank_card_id)
            db_user = await session.get(User, order.user_id)
            footer = await _footer(session, callback.from_user)
            try:
                await callback.message.edit_reply_markup(
                    reply_markup=order_sent_kb(order.id))
            except Exception:
                pass
            await callback.message.answer(
                texts.order_submitted(order.id,
                                      card.details if card else "your saved bank") + footer)
            await notify_admins(callback.bot,
                                f"📤 Order {texts.tag(order.id)}: user says the USDT is sent "
                                f"({order.usd_amount:g}$).")
            await update_order_cards(callback.bot, session, updated, db_user, card,
                                     admin_order_kb(order.id, "usdt_sent"))
            await callback.answer()

        elif callback_data.action == "cancel":
            age = (utcnow() - order.created_at).total_seconds()
            if age > settings.cancel_window_sec:
                support = await get_support(session)
                await callback.answer(texts.cancel_window_over(support), show_alert=True)
                return
            updated = await try_transition(
                session, order.id,
                (OrderStatus.SUBMITTED, OrderStatus.USDT_SENT), OrderStatus.CANCELLED)
            if updated is None:
                await callback.answer("This order can no longer be cancelled.", show_alert=True)
                return
            footer = await _footer(session, callback.from_user)
            await strip_kb(callback.message)
            await state.clear()
            await state.set_state(RefundFlow.address)
            await callback.message.answer(texts.order_cancelled(order.id) + footer)
            await notify_admins(callback.bot,
                                f"🚫 Order {texts.tag(order.id)} CANCELLED by the user — "
                                f"awaiting their refund address.")
            card = await session.get(BankCard, order.bank_card_id)
            db_user = await session.get(User, order.user_id)
            await update_order_cards(callback.bot, session, updated, db_user, card,
                                     admin_order_kb(order.id, "cancelled"))
            await callback.answer("Cancelled")

        else:
            await callback.answer()


async def _record_refund_address(message: Message, address: str) -> None:
    """Attach a refund address to the user's oldest cancelled order.

    Driven by the DB, not FSM state, so it survives /start, restarts, and a
    second cancel — as long as ANY cancelled order is waiting, a TRC20
    address message from that user gets recorded.
    """
    async with Session() as session:
        cancelled = (await session.scalars(
            select(Order).where(Order.user_id == message.from_user.id,
                                Order.status == OrderStatus.CANCELLED.value)
            .order_by(Order.id)
        )).all()
        support = await get_support(session)
        if not cancelled:
            await message.answer("You have no refund pending right now. "
                                 f"Message {support} if you think that's wrong.")
            return
        order = await try_transition(session, cancelled[0].id,
                                     (OrderStatus.CANCELLED,), OrderStatus.REFUND_REQUESTED,
                                     refund_address=address)
        if order is None:
            await message.answer(f"That refund is already being processed — "
                                 f"message {support} if anything is off.")
            return
        user = await session.get(User, order.user_id)
        card = await session.get(BankCard, order.bank_card_id)
        footer = await _footer(session, message.from_user)
        note = ""
        if len(cancelled) > 1:
            note = (f"\n\n⚠️ You have another cancelled order "
                    f"{texts.tag(cancelled[1].id)} — send its refund address too.")
        await message.answer(
            texts.refund_noted(order.id, order.usd_amount, address) + note + footer)
        await post_order_card(message.bot, session, order, user, card,
                              admin_order_kb(order.id, "refund_requested"))
        await update_order_cards(message.bot, session, order, user, card,
                                 admin_order_kb(order.id, "refund_requested"))


@router.message(RefundFlow.address, F.text)
async def refund_address_prompted(message: Message, state: FSMContext) -> None:
    address = message.text.strip()
    if not is_trc20(address):
        await message.answer("That doesn't look like a TRC20 address — it starts with "
                             "<code>T</code> and is 34 characters. Try again, or /cancel.")
        return
    await state.clear()
    await _record_refund_address(message, address)


@router.message(RefundFlow.address)
async def refund_address_not_text(message: Message) -> None:
    await message.answer("Please send your TRC20 address as <b>text</b> "
                         "(it starts with <code>T</code>), not a photo.")


@router.message(F.text.regexp(TRC20_PATTERN))
async def refund_address_any_state(message: Message, state: FSMContext) -> None:
    # A TRC20 address arriving outside the prompt (after /start, a restart,
    # or a second cancel) still reaches the oldest waiting refund.
    await _record_refund_address(message, message.text.strip())


@router.callback_query(BankCb.filter())
async def stale_bank_tap(callback: CallbackQuery) -> None:
    # A bank button tapped outside the checkout flow (state expired/cleared).
    await callback.answer("That session expired — tap 💵 USDT Sell to start over.",
                          show_alert=True)
