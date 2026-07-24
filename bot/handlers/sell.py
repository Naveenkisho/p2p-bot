import re

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select

from .. import texts
from ..config import SERVICES, settings
from ..db import (
    Session,
    get_deposit_address,
    get_desk_open,
    get_lang,
    get_or_create_user,
    get_rates,
    get_service_limits,
    get_support,
)
from ..flow import notify_deposit_received
from ..helpers import (
    TRC20_RE,
    notify_admins,
    post_order_card,
    queue_position,
    strip_kb,
    try_transition,
    update_order_cards,
)
from ..keyboards import (
    OrderCb,
    PickBankCb,
    PreBankCb,
    admin_order_kb,
    cancel_kb,
    deposit_kb,
    hide_kb,
    pre_bank_chooser_kb,
    services_kb,
)
from ..models import BankCard, Order, OrderStatus, User
from ..states import BankForOrder, RefundFlow, SellFlow
from .start import bank_details_error, make_bank_label

router = Router(name="sell")

AMOUNT_RE = re.compile(r"^\$?\s*(\d{1,7}(?:\.\d{1,2})?)\s*\$?$")
TRC20_PATTERN = TRC20_RE.pattern

_warned_no_address = False


async def _warn_no_address_once(bot) -> None:
    global _warned_no_address
    if not _warned_no_address:
        _warned_no_address = True
        await notify_admins(bot, "⚠️ A user tried to sell but no deposit address "
                                 "is set — run /setaddress T…")


async def _ctx(session, tg_user) -> tuple[str, str]:
    """(lang, trust footer) for the acting user."""
    support = await get_support(session)
    lang = await get_lang(session, tg_user.id)
    return lang, texts.trust_footer(tg_user.first_name, tg_user.id, support, lang)


@router.callback_query(F.data == "menu:sell")
async def sell_menu(callback: CallbackQuery, state: FSMContext) -> None:
    async with Session() as session:
        user = await get_or_create_user(session, callback.from_user.id,
                                        callback.from_user.username, callback.from_user.first_name)
        rates = await get_rates(session)
        address = await get_deposit_address(session)
        desk_open = await get_desk_open(session)
        lang, footer = await _ctx(session, callback.from_user)
    if user.banned:
        await callback.answer(texts.BANNED, show_alert=True)
        return
    if not desk_open or not rates or not address:
        if not address and desk_open:
            await _warn_no_address_once(callback.bot)
        await callback.answer(texts.DESK_CLOSED, show_alert=True)
        return
    await state.clear()
    await callback.message.answer(texts.services_header(rates, lang) + footer,
                                  reply_markup=services_kb(rates))
    await callback.answer()


@router.callback_query(F.data.startswith("svc:"))
async def sell_service(callback: CallbackQuery, state: FSMContext) -> None:
    key = callback.data.split(":", 1)[1]
    async with Session() as session:
        rates = await get_rates(session)
        lang, footer = await _ctx(session, callback.from_user)
    if key not in rates:
        await callback.answer("That service is unavailable right now.", show_alert=True)
        return
    async with Session() as session:
        lo, hi = await get_service_limits(session, key)
    await state.clear()
    await state.update_data(service=key, rate=rates[key], lo=lo, hi=hi)
    await state.set_state(SellFlow.amount)
    await callback.message.answer(
        texts.ask_amount(SERVICES[key], rates[key], lo, hi, lang) + footer,
        reply_markup=cancel_kb())
    await callback.answer()


@router.message(SellFlow.amount, F.text)
async def sell_amount(message: Message, state: FSMContext) -> None:
    m = AMOUNT_RE.match(message.text.replace(",", "").strip())
    if not m:
        await message.answer("Please send just the amount as a number, e.g. <code>100</code>.")
        return
    usd = float(m.group(1))
    data = await state.get_data()
    lo = data.get("lo", settings.min_usd)
    hi = data.get("hi", settings.max_usd)
    if not (lo <= usd <= hi):
        await message.answer(f"Amount must be between {lo:g}$ and {hi:g}$ "
                             f"for this service.")
        return
    rate = data.get("rate")
    if rate is None:
        await message.answer("That session expired — tap 💵 USDT Sell to start over.",
                             reply_markup=hide_kb())
        return
    await state.update_data(usd=usd)
    async with Session() as session:
        cards = (await session.scalars(
            select(BankCard).where(BankCard.user_id == message.from_user.id)
            .order_by(BankCard.id)
        )).all()
        lang, footer = await _ctx(session, message.from_user)
    if cards:
        await state.set_state(SellFlow.choose_bank)
        await message.answer(texts.choose_bank(usd, usd * rate, lang) + footer,
                             reply_markup=pre_bank_chooser_kb(cards))
    else:
        await state.set_state(SellFlow.bank_details)
        await message.answer(texts.choose_bank(usd, usd * rate, lang) + "\n\n"
                             + texts.ask_bank_new(lang) + footer,
                             reply_markup=cancel_kb())


@router.message(SellFlow.amount)
async def sell_amount_not_text(message: Message) -> None:
    await message.answer("Please type the amount as a number, e.g. <code>100</code>.")


async def _create_order(message_target, state: FSMContext, tg_user,
                        card: BankCard) -> None:
    """All money-moment checks + order creation, AFTER the bank is chosen.
    Shows the deposit address as the LAST message — the admin card is only
    posted later, when the deposit is verified on-chain."""
    data = await state.get_data()
    await state.clear()
    usd = data.get("usd")
    service = data.get("service")
    if usd is None or service is None:
        await message_target.answer("That session expired — tap 💵 USDT Sell "
                                    "to start over.", reply_markup=hide_kb())
        return
    async with Session() as session:
        address = await get_deposit_address(session)
        user = await get_or_create_user(session, tg_user.id,
                                        tg_user.username, tg_user.first_name)
        lang, footer = await _ctx(session, tg_user)
        if user.banned:
            await message_target.answer(texts.BANNED, reply_markup=hide_kb())
            return
        if not await get_desk_open(session) or not address:
            if not address:
                await _warn_no_address_once(message_target.bot)
            await message_target.answer(texts.DESK_CLOSED, reply_markup=hide_kb())
            return
        open_count = await session.scalar(
            select(func.count()).select_from(Order).where(
                Order.user_id == user.id,
                Order.status.in_([OrderStatus.AWAITING_DEPOSIT.value,
                                  OrderStatus.DEPOSIT_RECEIVED.value,
                                  OrderStatus.PENDING_PAYOUT.value])))
        if (open_count or 0) >= settings.open_orders_max:
            await message_target.answer(
                f"⚠️ You already have {open_count} open order(s). Please finish or "
                "cancel them before starting another.", reply_markup=hide_kb())
            return
        # re-check the live rate at the money moment
        rates = await get_rates(session)
        rate = data.get("rate")
        if service not in rates:
            await message_target.answer(texts.DESK_CLOSED, reply_markup=hide_kb())
            return
        rate_note = ""
        if rates[service] != rate:
            rate = rates[service]
            rate_note = texts.rate_updated_note(rate, lang)
        order = Order(
            user_id=user.id,
            side="sell",
            service=service,
            usd_amount=usd,
            rate_inr=rate,
            inr_amount=usd * rate,
            bank_card_id=card.id,
            deposit_address=address,
        )
        session.add(order)
        await session.commit()
        await message_target.answer(f"🏦 {card.label} ✅", reply_markup=hide_kb())
        await message_target.answer(
            texts.deposit_request(order.id, order.usd_amount, order.inr_amount,
                                  SERVICES[service], address, rate, rate_note,
                                  card.label, lang) + footer,
            reply_markup=deposit_kb(order.id),
        )


@router.callback_query(SellFlow.choose_bank, PreBankCb.filter())
async def pre_pick_bank(callback: CallbackQuery, callback_data: PreBankCb,
                        state: FSMContext) -> None:
    if callback_data.card_id == 0:
        async with Session() as session:
            lang = await get_lang(session, callback.from_user.id)
        await state.set_state(SellFlow.bank_details)
        await callback.message.answer(texts.ask_bank_new(lang), reply_markup=cancel_kb())
        await callback.answer()
        return
    async with Session() as session:
        card = await session.get(BankCard, callback_data.card_id)
    if card is None or card.user_id != callback.from_user.id:
        await callback.answer("Bank not found.", show_alert=True)
        return
    await strip_kb(callback.message)
    await _create_order(callback.message, state, callback.from_user, card)
    await callback.answer()


@router.message(SellFlow.bank_details, F.text)
async def sell_new_bank(message: Message, state: FSMContext) -> None:
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
    await _create_order(message, state, message.from_user, card)


@router.message(SellFlow.bank_details)
async def sell_new_bank_not_text(message: Message) -> None:
    await message.answer("Please <b>type</b> your bank details as text — "
                         "not a photo or file.")


@router.callback_query(PreBankCb.filter())
async def pre_bank_stale(callback: CallbackQuery) -> None:
    await callback.answer("That session expired — tap 💵 USDT Sell to start over.",
                          show_alert=True)


@router.callback_query(OrderCb.filter())
async def order_action(callback: CallbackQuery, callback_data: OrderCb,
                       state: FSMContext) -> None:
    async with Session() as session:
        order = await session.get(Order, callback_data.order_id)
        if order is None or order.user_id != callback.from_user.id:
            await callback.answer("Order not found.", show_alert=True)
            return

        if callback_data.action == "check":
            status = order.status
            if status == OrderStatus.AWAITING_DEPOSIT:
                if order.admin_note != "sent_claimed":
                    order.admin_note = "sent_claimed"
                    await session.commit()
                    await notify_admins(callback.bot,
                                        f"🔍 Order {texts.tag(order.id)}: user says the USDT "
                                        f"is sent but it's not detected yet "
                                        f"({order.usd_amount:g}$). If it doesn't confirm, "
                                        f"check manually and use /received {order.id}.")
                await callback.answer(
                    "We're watching the blockchain every few seconds — you'll get "
                    "a message the moment it lands. If nothing in ~5 minutes, "
                    "message support.", show_alert=True)
            elif status == OrderStatus.DEPOSIT_RECEIVED:
                # deposit just verified — re-drive the next step (payout queue,
                # or the bank chooser for legacy orders without a bank)
                await callback.answer("Deposit verified ✅", show_alert=True)
                await notify_deposit_received(callback.bot, order.id)
            elif status == OrderStatus.PENDING_PAYOUT:
                await callback.answer("✅ Verified — your payout is in the queue.",
                                      show_alert=True)
            else:
                await callback.answer("This order is already in processing.", show_alert=True)

        elif callback_data.action == "cancel":
            updated = await try_transition(session, order.id,
                                           (OrderStatus.AWAITING_DEPOSIT,),
                                           OrderStatus.CANCELLED)
            if updated is None:
                fresh = await session.get(Order, order.id)
                st = fresh.status if fresh else None
                if st in (OrderStatus.DEPOSIT_RECEIVED, OrderStatus.PENDING_PAYOUT):
                    msg = ("Your deposit is already in — choose your bank / it's being "
                           "paid. Contact support to change anything.")
                elif st == OrderStatus.EXPIRED:
                    msg = "This order already expired — start a fresh one anytime."
                elif st in (OrderStatus.CANCELLED, OrderStatus.REFUND_REQUESTED,
                            OrderStatus.REFUNDED):
                    msg = "This order is already cancelled."
                else:
                    msg = "This order can no longer be cancelled."
                await callback.answer(msg, show_alert=True)
                return
            lang, footer = await _ctx(session, callback.from_user)
            await strip_kb(callback.message)
            await state.clear()
            await state.set_state(RefundFlow.address)
            await callback.message.answer(texts.order_cancelled(order.id, lang) + footer,
                                          reply_markup=cancel_kb())
            await notify_admins(callback.bot,
                                f"🚫 Order {texts.tag(order.id)} cancelled by the user "
                                f"(no deposit detected).")
            card = await session.get(BankCard, order.bank_card_id) if order.bank_card_id else None
            db_user = await session.get(User, order.user_id)
            await update_order_cards(callback.bot, session, updated, db_user, card,
                                     admin_order_kb(order.id, "cancelled"))
            await callback.answer("Cancelled")

        else:
            await callback.answer()


async def _attach_bank(bot, order_id: int, user_id: int, card_id: int):
    """CAS deposit_received → pending_payout with the chosen bank; returns
    (order, card) on success, None if the order moved on already."""
    async with Session() as session:
        card = await session.get(BankCard, card_id)
        if card is None or card.user_id != user_id:
            return None
        order = await try_transition(session, order_id,
                                     (OrderStatus.DEPOSIT_RECEIVED,),
                                     OrderStatus.PENDING_PAYOUT,
                                     bank_card_id=card_id)
        if order is None:
            return None
        return order, card


async def _finish_bank_step(message_target, bot, order: Order, card: BankCard,
                            tg_user) -> None:
    async with Session() as session:
        lang, footer = await _ctx(session, tg_user)
        q_note = texts.queue_note(await queue_position(session, order.id), lang)
        user = await session.get(User, order.user_id)
        await message_target.answer("✅ Bank selected.", reply_markup=hide_kb())
        await message_target.answer(
            texts.order_submitted(order.id, card.details, q_note, lang) + footer)
        posted = await post_order_card(bot, session, order, user, card,
                                       admin_order_kb(order.id, "pending_payout"))
        await update_order_cards(bot, session, order, user, card,
                                 admin_order_kb(order.id, "pending_payout"))
        if not posted:
            await notify_admins(bot, f"⚠️ Couldn't post the payout card for Order "
                                     f"{texts.tag(order.id)} — run /order {order.id}.")


@router.callback_query(PickBankCb.filter())
async def pick_bank(callback: CallbackQuery, callback_data: PickBankCb,
                    state: FSMContext) -> None:
    async with Session() as session:
        order = await session.get(Order, callback_data.order_id)
    if order is None or order.user_id != callback.from_user.id:
        await callback.answer("Order not found.", show_alert=True)
        return
    if callback_data.card_id == 0:
        if order.status != OrderStatus.DEPOSIT_RECEIVED:
            await callback.answer("This order is already in processing.", show_alert=True)
            return
        async with Session() as session:
            lang = await get_lang(session, callback.from_user.id)
        await state.clear()
        await state.set_state(BankForOrder.details)
        await state.update_data(order_id=order.id)
        await callback.message.answer(texts.ask_bank_new(lang), reply_markup=cancel_kb())
        await callback.answer()
        return
    result = await _attach_bank(callback.bot, order.id, callback.from_user.id,
                                callback_data.card_id)
    if result is None:
        await callback.answer("This order is already in processing.", show_alert=True)
        return
    order, card = result
    await strip_kb(callback.message)
    await _finish_bank_step(callback.message, callback.bot, order, card,
                            callback.from_user)
    await callback.answer("Bank locked in ✅")


@router.message(BankForOrder.details, F.text)
async def bank_for_order_details(message: Message, state: FSMContext) -> None:
    details = message.text.strip()
    error = bank_details_error(details)
    if error:
        await message.answer(error)
        return
    data = await state.get_data()
    await state.clear()
    order_id = data.get("order_id")
    if order_id is None:
        await message.answer("That session expired — tap the bank button on your "
                             "order message again.")
        return
    async with Session() as session:
        card = BankCard(user_id=message.from_user.id,
                        label=make_bank_label(details), details=details)
        session.add(card)
        await session.commit()
        card_id = card.id
    result = await _attach_bank(message.bot, order_id, message.from_user.id, card_id)
    if result is None:
        await message.answer("This order is already in processing — your bank was "
                             "saved to My Bank Cards.")
        return
    order, card = result
    await _finish_bank_step(message, message.bot, order, card, message.from_user)


@router.message(BankForOrder.details)
async def bank_for_order_not_text(message: Message) -> None:
    await message.answer("Please <b>type</b> your bank details as text — "
                         "not a photo or file.")


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
        card = await session.get(BankCard, order.bank_card_id) if order.bank_card_id else None
        lang, footer = await _ctx(session, message.from_user)
        note = ""
        if len(cancelled) > 1:
            if lang == "hi":
                note = (f"\n\n⚠️ Aapka ek aur cancelled order hai "
                        f"{texts.tag(cancelled[1].id)} — uska refund address bhi bhejein.")
            else:
                note = (f"\n\n⚠️ You have another cancelled order "
                        f"{texts.tag(cancelled[1].id)} — send its refund address too.")
        await message.answer(
            texts.refund_noted(order.id, order.usd_amount, address, lang) + note + footer,
            reply_markup=hide_kb())
        await post_order_card(message.bot, session, order, user, card,
                              admin_order_kb(order.id, "refund_requested"))
        await update_order_cards(message.bot, session, order, user, card,
                                 admin_order_kb(order.id, "refund_requested"))


@router.message(RefundFlow.address, F.text)
async def refund_address_prompted(message: Message, state: FSMContext) -> None:
    address = message.text.strip()
    if not TRC20_RE.fullmatch(address):
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