import re

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy import func, select

from .. import texts
from ..qr import qr_png
from ..config import SERVICES, settings
from ..db import (
    Session,
    bep20_active,
    get_admin_targets,
    get_bep20_address,
    get_deposit_address,
    get_deposit_ttl,
    get_network_qr,
    get_desk_open,
    get_lang,
    get_or_create_user,
    get_rates,
    get_service_limits,
    get_support,
)
from ..flow import notify_deposit_received
from ..scanner import _ms, launch_order_check
from ..helpers import (
    TXID_RE,
    _txid_variants,
    esc,
    explorer_tx,
    norm_txid,
    notify_admins,
    order_display_address,
    post_order_card,
    queue_position,
    strip_kb,
    try_transition,
    txid_used_elsewhere,
    update_order_cards,
)
from ..keyboards import (
    ClaimReqCb,
    OrderCb,
    PickBankCb,
    PreBankCb,
    RefundReqCb,
    admin_order_kb,
    cancel_kb,
    claim_review_kb,
    deposit_kb,
    expired_kb,
    hide_kb,
    network_kb,
    pre_bank_chooser_kb,
    services_kb,
    start_fresh_kb,
)
from ..models import BankCard, Order, OrderMsg, OrderStatus, SeenTx, User, utcnow
from ..states import BankForOrder, ClaimFlow, RefundFlow, SellFlow
from .start import bank_details_error, make_bank_label

router = Router(name="sell")

AMOUNT_RE = re.compile(r"^\$?\s*(\d{1,7}(?:\.\d{1,2})?)\s*\$?$")

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


def _tag_amount(order_id: int, requested: float) -> float:
    """Give the order a unique, non-round deposit amount derived from its OWN
    order id (unique cents) — so the amount alone identifies it to the scanner,
    with no cross-order bookkeeping and no way to conflict with anyone else.

    A whole-dollar request like $100 becomes 100.01, 100.02, 100.03… as the id
    advances; consecutive orders always get different cents, so two open orders
    can never share a tag. Expired orders are simply gone — we never look at them.

    The user isn't charged the cents — they send that exact amount and receive the
    matching INR (inr = usd × rate), so it's a fractionally larger trade, not a
    fee. Deviation from the requested amount is at most a couple of cents."""
    base = round(requested, 2)
    if not settings.unique_cents:
        return base
    whole = int(base)
    cents = (order_id % 99) + 1          # 1..99 — never a round .00
    amt = round(whole + cents / 100, 2)
    if amt < base:                       # user typed finer decimals than our tag
        amt = round(whole + 1 + cents / 100, 2)
    return amt


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
    async with Session() as session:
        two_chains = await bep20_active(session)
    await state.clear()
    if two_chains:
        # both chains live → pick the network FIRST (right after USDT Sell), then the
        # payout method; the address we later show follows this choice.
        await state.set_state(SellFlow.network)
        await callback.message.answer(texts.ask_network(lang) + footer,
                                      reply_markup=network_kb())
    else:
        await callback.message.answer(texts.services_header(rates, lang) + footer,
                                      reply_markup=services_kb(rates))
    await callback.answer()


@router.callback_query(SellFlow.network, F.data.startswith("net:"))
async def sell_network(callback: CallbackQuery, state: FSMContext) -> None:
    """Network picked (only reached when BEP20 is live). Store it, then show the
    payout methods — the service pick continues into the amount step."""
    net = callback.data.split(":", 1)[1]
    if net not in ("TRC20", "BEP20"):
        await callback.answer("Pick a network.", show_alert=True)
        return
    async with Session() as session:
        rates = await get_rates(session)
        lang, footer = await _ctx(session, callback.from_user)
    if not rates:
        await callback.answer(texts.DESK_CLOSED, show_alert=True)
        return
    await state.update_data(network=net)
    await state.set_state(None)              # keep data; the svc:* pick isn't state-gated
    await strip_kb(callback.message)
    await callback.message.answer(texts.services_header(rates, lang) + footer,
                                  reply_markup=services_kb(rates))
    await callback.answer()


@router.message(SellFlow.network)
async def sell_network_not_tap(message: Message) -> None:
    await message.answer("Tap 🔷 <b>TRC20</b> or 🟡 <b>BEP20</b> above to pick your network.")


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
    # network was chosen first (BEP20 live) or defaults to TRC20 (single chain)
    data = await state.get_data()
    network = data.get("network", "TRC20")
    await state.update_data(service=key, rate=rates[key], lo=lo, hi=hi, network=network)
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
    service_label = SERVICES.get(data.get("service"), data.get("service"))
    await state.update_data(usd=usd)
    async with Session() as session:
        cards = (await session.scalars(
            select(BankCard).where(BankCard.user_id == message.from_user.id)
            .order_by(BankCard.id)
        )).all()
        lang, footer = await _ctx(session, message.from_user)
    if cards:
        await state.set_state(SellFlow.choose_bank)
        await message.answer(
            texts.choose_bank(usd, usd * rate, service_label, rate, True, lang) + footer,
            reply_markup=pre_bank_chooser_kb(cards))
    else:
        await state.set_state(SellFlow.bank_details)
        await message.answer(
            texts.choose_bank(usd, usd * rate, service_label, rate, False, lang)
            + "\n\n" + texts.ask_bank_new(lang) + footer,
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
    network = data.get("network", "TRC20")
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
        # Cap PAID / in-flight settlements (deposit already detected, awaiting the
        # INR payout) so a user can't stack unlimited orders. Unpaid awaiting quotes
        # are NOT counted here — starting a new order simply voids the old one below.
        inflight = await session.scalar(
            select(func.count()).select_from(Order).where(
                Order.user_id == user.id,
                Order.status.in_([OrderStatus.DEPOSIT_RECEIVED.value,
                                  OrderStatus.PENDING_PAYOUT.value])))
        if (inflight or 0) >= settings.open_orders_max:
            await message_target.answer(
                f"⚠️ You already have {inflight} order(s) being processed. Please wait "
                "for those payouts before starting another.", reply_markup=hide_kb())
            return
        # One live deposit session per user: instantly expire any earlier UNPAID
        # quote, so there's never two "please send $X" screens live at once (a lone
        # quote still expires on its own after the TTL).
        voided: list[int] = []
        for p in (await session.scalars(select(Order).where(
                Order.user_id == user.id,
                Order.status == OrderStatus.AWAITING_DEPOSIT.value))).all():
            gone = await try_transition(session, p.id,
                                        (OrderStatus.AWAITING_DEPOSIT,),
                                        OrderStatus.EXPIRED)
            if gone is not None:
                voided.append(gone.id)
                pcard = (await session.get(BankCard, gone.bank_card_id)
                         if gone.bank_card_id else None)
                await update_order_cards(message_target.bot, session, gone,
                                         user, pcard, None)
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
        # Which chain the customer actually gets an address for (BEP20 only when it's
        # live) — decided BEFORE the order is saved so we can persist exactly what the
        # customer is shown. deposit_address stays the TRC20 desk address (for the
        # amount-based scanner); network + display_address let every LATER screen
        # (reminder, recovery, admin card) re-show the same address/QR.
        bep20 = await get_bep20_address(session) if await bep20_active(session) else ""
        if network == "BEP20" and bep20:
            show_addr, net_label, net_key = bep20, "BEP20 (BSC)", "BEP20"
        else:
            show_addr, net_label, net_key = address, "TRC20 (TRON)", "TRC20"
        order = Order(
            user_id=user.id,
            side="sell",
            service=service,
            usd_amount=usd,
            rate_inr=rate,
            inr_amount=usd * rate,
            bank_card_id=card.id,
            deposit_address=address,
            network=net_key,
            display_address=show_addr,
        )
        session.add(order)
        await session.flush()          # assigns order.id, which seeds the tag
        order.usd_amount = _tag_amount(order.id, usd)
        order.inr_amount = order.usd_amount * rate
        await session.commit()
        order_id, usd_amount, inr_amount = order.id, order.usd_amount, order.inr_amount
        created_at = order.created_at
        support = await get_support(session)
        ttl_min = await get_deposit_ttl(session)

    await message_target.answer(f"🏦 {card.label} ✅", reply_markup=hide_kb())
    if voided:
        await message_target.answer(texts.quote_superseded(voided, lang))
    # No trust footer appended here on purpose: the address is the LAST line of the
    # deposit screen so it stays visible above the keyboard — support is inline instead.
    msg = texts.deposit_request(order_id, usd_amount, inr_amount, SERVICES[service],
                                show_addr, net_label, rate, created_at=created_at,
                                rate_note=rate_note, bank_label=card.label,
                                support=support, ttl_min=ttl_min, lang=lang)
    # Prefer an admin-uploaded QR for this network, but only when it still encodes
    # the live address (get_network_qr enforces that); otherwise auto-generate one.
    async with Session() as session:
        png = await get_network_qr(session, net_key, show_addr)
    png = png or qr_png(show_addr)
    if png and len(msg) <= 1024:
        await message_target.answer_photo(
            BufferedInputFile(png, "deposit-qr.png"), caption=msg,
            reply_markup=deposit_kb(order_id))
    elif png:
        await message_target.answer_photo(
            BufferedInputFile(png, "deposit-qr.png"),
            caption=f"📷 Scan to pay on {net_label}")
        await message_target.answer(msg, reply_markup=deposit_kb(order_id),
                                    disable_web_page_preview=True)
    else:
        await message_target.answer(msg, reply_markup=deposit_kb(order_id),
                                    disable_web_page_preview=True)


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
                lang = await get_lang(session, callback.from_user.id)
                started = launch_order_check(callback.bot, order.id)
                if started:
                    await callback.answer(texts.checking_now(lang), show_alert=True)
                else:
                    await callback.answer("Already checking — one moment…",
                                          show_alert=True)
            elif status == OrderStatus.DEPOSIT_RECEIVED:
                # deposit just verified — re-drive the next step (payout queue,
                # or the bank chooser for legacy orders without a bank)
                await callback.answer("Deposit verified ✅", show_alert=True)
                await notify_deposit_received(callback.bot, order.id)
            elif status == OrderStatus.PENDING_PAYOUT:
                await callback.answer("✅ Verified — your payout is in the queue.",
                                      show_alert=True)
            elif status == OrderStatus.EXPIRED:
                ttl_min = await get_deposit_ttl(session)
                await callback.answer(
                    f"⌛ This {ttl_min}-minute payment window "
                    "expired — please start a fresh payout.", show_alert=True)
                await strip_kb(callback.message)
                lang, footer = await _ctx(session, callback.from_user)
                support = await get_support(session)
                await callback.message.answer(
                    texts.order_expired(order.id, support, lang, ttl_min=ttl_min),
                    reply_markup=expired_kb(order.id))
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
            # Cancel is now an explicit "I'm not paid" — so end cleanly with just a
            # "start fresh" option. No claim/refund buttons: the customer declared
            # they didn't pay (if they actually did, the support footer points them there).
            await callback.message.answer(texts.order_cancelled(order.id, lang) + footer,
                                          reply_markup=start_fresh_kb())
            # No admin ping: a user-cancel is always on an UNPAID awaiting order, so
            # it's just abandonment. Only a submitted TXID (claim) — which the bot
            # verifies on-chain — reaches the admin. Strip any card, silently.
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


@router.callback_query(RefundReqCb.filter())
async def request_refund(callback: CallbackQuery, callback_data: RefundReqCb,
                         state: FSMContext) -> None:
    async with Session() as session:
        order = await session.get(Order, callback_data.order_id)
        if order is None or order.user_id != callback.from_user.id:
            await callback.answer("Order not found.", show_alert=True)
            return
        if order.status in (OrderStatus.REFUND_REQUESTED.value, OrderStatus.REFUNDED.value):
            await callback.answer("A refund is already being processed for this order.",
                                  show_alert=True)
            return
        if order.status != OrderStatus.CANCELLED.value:
            await callback.answer("This order can't be refunded.", show_alert=True)
            return
        lang = await get_lang(session, callback.from_user.id)
    await state.clear()
    await state.set_state(RefundFlow.txid)
    await state.update_data(order_id=order.id)
    await callback.message.answer(texts.ask_refund_txid(order.id, lang),
                                  reply_markup=cancel_kb())
    await callback.answer()


@router.message(RefundFlow.txid, F.text)
async def refund_txid(message: Message, state: FSMContext) -> None:
    txid = norm_txid(message.text)
    if not TXID_RE.fullmatch(txid):
        await message.answer("That doesn't look like a TXID — it's 64 letters/numbers "
                             "(the transaction hash). Paste it again, or tap ❌ Cancel.")
        return
    data = await state.get_data()
    await state.clear()
    order_id = data.get("order_id")
    async with Session() as session:
        order = await session.get(Order, order_id) if order_id else None
        if order is None or order.user_id != message.from_user.id:
            await message.answer("Order not found — contact support.", reply_markup=hide_kb())
            return
        lang, footer = await _ctx(session, message.from_user)
        address = order.deposit_address
        # a TXID can only ever back ONE refund
        dup = await session.scalar(
            select(Order).where(Order.refund_txid.in_(_txid_variants(txid)),
                                Order.id != order.id,
                                Order.status.in_([OrderStatus.REFUND_REQUESTED.value,
                                                  OrderStatus.REFUNDED.value])).limit(1))
        if dup is not None:
            await message.answer("That TXID is already linked to another refund. "
                                 "Contact support if this is a mistake.",
                                 reply_markup=hide_kb())
            return
        # a TXID already credited to / claimed by another order can't be refunded here
        used = await txid_used_elsewhere(session, txid, order.id)
        if used is not None:
            await message.answer(
                f"🚫 That TXID is already tied to order {texts.tag(used)} — an on-chain "
                "transfer can only be used once. Contact support if this is a mistake.",
                reply_markup=hide_kb())
            return

    # verify on-chain BEFORE accepting — a refund TXID that never reached our address
    # (or is far too old) is declined on the user's face, with proof
    ok, reject, _info = await _verify_deposit_tx(txid, address, order, lang)
    if not ok:
        await message.answer(reject + footer, reply_markup=hide_kb(),
                             disable_web_page_preview=True)
        return

    async with Session() as session:
        updated = await try_transition(session, order_id, (OrderStatus.CANCELLED,),
                                       OrderStatus.REFUND_REQUESTED, refund_txid=txid)
        if updated is None:
            await message.answer("This order's refund is already being processed.",
                                 reply_markup=hide_kb())
            return
        user = await session.get(User, updated.user_id)
        card = await session.get(BankCard, updated.bank_card_id) if updated.bank_card_id else None
        seen = await session.get(SeenTx, txid)
        await message.answer(texts.refund_submitted(order_id, lang) + footer,
                             reply_markup=hide_kb())
        await post_order_card(message.bot, session, updated, user, card,
                              admin_order_kb(order_id, "refund_requested"))
        await update_order_cards(message.bot, session, updated, user, card,
                                 admin_order_kb(order_id, "refund_requested"))
    if seen is not None:
        await notify_admins(message.bot,
                            f"↩️ Refund {texts.tag(order_id)}: ✅ TXID matches a deposit "
                            f"we recorded of <b>{seen.amount:g} USDT</b>. Refund that to "
                            f"the SENDER (see Tronscan on the card).")
    else:
        await notify_admins(message.bot,
                            f"↩️ Refund {texts.tag(order_id)}: ⚠️ TXID not in our scan "
                            f"records — verify carefully on Tronscan before refunding.")


@router.message(RefundFlow.txid)
async def refund_txid_not_text(message: Message) -> None:
    await message.answer("Please paste the <b>TXID</b> (transaction hash) as text — "
                         "not a photo — or tap ❌ Cancel.")


_CLAIMABLE = (OrderStatus.AWAITING_DEPOSIT.value, OrderStatus.EXPIRED.value,
              OrderStatus.CANCELLED.value)


@router.callback_query(ClaimReqCb.filter())
async def request_claim(callback: CallbackQuery, callback_data: ClaimReqCb,
                        state: FSMContext) -> None:
    async with Session() as session:
        order = await session.get(Order, callback_data.order_id)
        if order is None or order.user_id != callback.from_user.id:
            await callback.answer("Order not found.", show_alert=True)
            return
        if order.claim_txid:
            await callback.answer("We already have your TXID — it's under review.",
                                  show_alert=True)
            return
        if order.status not in _CLAIMABLE:
            await callback.answer("This order can't be confirmed by TXID anymore — "
                                  "contact support.", show_alert=True)
            return
        lang = await get_lang(session, callback.from_user.id)
    await state.clear()
    await state.set_state(ClaimFlow.txid)
    await state.update_data(order_id=order.id)
    await callback.message.answer(texts.ask_claim_txid(order.id, lang),
                                  reply_markup=cancel_kb())
    await callback.answer()


async def _verify_deposit_tx(txid: str, address: str, order: Order,
                             lang: str = "en") -> tuple[bool, str, dict]:
    """On-chain gate for a user-submitted claim/refund TXID. Looks the tx up BY HASH
    (any destination) and, if it clearly wasn't a USDT transfer to our address (or is
    far too old), returns (False, reject_message, info) so we decline on the user's face
    and never bother the admin. A transient API error passes through (ok=True) for the
    admin to verify manually. `info` is the on-chain result for the admin card."""
    from ..scanner import lookup_tx_global
    info = await lookup_tx_global(txid, address)
    if info.get("error"):
        return True, "", info
    if not info.get("found"):
        return False, texts.tx_not_found(lang), info
    if not info.get("to_ok"):
        # show the customer the address for THEIR network as the expected destination
        # (deposit_address is always the TRC20 desk address, wrong for a BEP20 order)
        expected = order_display_address(order)[0] or address
        return False, texts.tx_wrong_address(info.get("to", ""), expected, lang), info
    # the amount must be THIS order's own deposit (within a fee band) — otherwise a
    # customer could claim another customer's mismatched deposit that merely landed at
    # our shared address
    amount = info.get("amount") or 0.0
    expected = order.usd_amount or 0.0
    band = max(settings.claim_fee_band_abs, settings.claim_fee_band_pct * expected)
    if expected and abs(amount - expected) > band:
        return False, texts.tx_amount_mismatch(amount, expected, lang), info
    ts = info.get("timestamp") or 0
    if ts and ts < _ms(order.created_at) - 6 * 3600 * 1000:   # >6h before the order
        age_days = max(1, int((_ms(utcnow()) - ts) / 86_400_000))
        return False, texts.tx_too_old(age_days, lang), info
    return True, "", info


async def _post_claim_card(bot, order_id: int, txid: str, verify: dict) -> None:
    """Post the admin review card for a payment claim: bank details, the TXID
    with a Tronscan link, the on-chain auto-check result, and Confirm/Reject."""
    async with Session() as session:
        order = await session.get(Order, order_id)
        if order is None:
            return
        user = await session.get(User, order.user_id)
        card = await session.get(BankCard, order.bank_card_id) if order.bank_card_id else None
        expected = order.usd_amount
        if verify.get("error"):
            vsum = "⚠️ Couldn't reach TronGrid to auto-check — verify on Tronscan."
        elif not verify.get("found"):
            vsum = ("⚠️ <b>NOT found</b> as a confirmed USDT transfer to your deposit "
                    "address — could be unconfirmed, wrong network, or wrong TXID. "
                    "Check Tronscan before confirming.")
        else:
            amt = verify.get("amount") or 0.0
            ts = verify.get("timestamp") or 0
            m = lambda ok: "✅" if ok else "⚠️"
            vsum = (
                f"{m(verify.get('to_ok'))} to your address · "
                f"{m(abs(amt - expected) <= 0.01)} amount <b>{texts.usd_str(amt)} USDT</b> "
                f"(order expects {texts.usd_str(expected)}) · "
                f"{m(ts >= _ms(order.created_at))} sent after the order was created")
        bank = (f"🏦 Payout bank:\n<code>{esc(card.details)}</code>\n" if card
                else "🏦 Payout bank: ⚠️ none on file\n")
        text = (
            f"🧾 <b>Payment claim — Order {texts.tag(order_id)}</b>\n"
            f"👤 {esc(user.first_name or '')} · 🆔 <code>{user.id}</code>\n"
            f"💱 pay <b>₹{order.inr_amount:,.2f}</b> "
            f"({texts.usd_str(order.usd_amount)}$ × ₹{order.rate_inr:g})\n"
            f"{bank}"
            f"🔗 TXID: <code>{esc(txid)}</code>\n"
            f"🔎 {explorer_tx(esc(txid))}\n\n"
            f"<b>On-chain check:</b> {vsum}\n\n"
            "⚠️ This proves the tx <b>reached our address</b> — <b>not who sent it</b>. "
            "Deposit hashes are public, so confirm this customer really made this "
            "transfer (not a hash copied off the explorer) before paying.\n\n"
            "Confirm credits the <b>actual amount received</b> (above), converted at "
            "this order's rate, and moves it to your payout queue.")
        targets = await get_admin_targets(session)
        for chat_id in targets:
            try:
                msg = await bot.send_message(chat_id, text,
                                             reply_markup=claim_review_kb(order_id))
                session.add(OrderMsg(order_id=order_id, chat_id=chat_id,
                                     message_id=msg.message_id))
            except Exception:
                pass
        await session.commit()


@router.message(ClaimFlow.txid, F.text)
async def claim_txid(message: Message, state: FSMContext) -> None:
    txid = norm_txid(message.text)
    if not TXID_RE.fullmatch(txid):
        await message.answer("That doesn't look like a TXID — it's 64 letters/numbers "
                             "(the transaction hash). Paste it again, or tap ❌ Cancel.")
        return
    data = await state.get_data()
    await state.clear()
    order_id = data.get("order_id")
    async with Session() as session:
        order = await session.get(Order, order_id) if order_id else None
        if order is None or order.user_id != message.from_user.id:
            await message.answer("Order not found — contact support.", reply_markup=hide_kb())
            return
        if order.status not in _CLAIMABLE:
            await message.answer("This order can't be confirmed by TXID anymore — "
                                 "contact support.", reply_markup=hide_kb())
            return
        # a TXID can only ever back ONE payout — reject one already tied to
        # another order (auto-detected deposit, claim, refund, or seen-tx)
        used = await txid_used_elsewhere(session, txid, order.id)
        if used is not None:
            await message.answer(
                f"🚫 That TXID has already been used for order {texts.tag(used)} — "
                "an on-chain transfer can only be cashed out once. If you think "
                "this is a mistake, contact support.", reply_markup=hide_kb())
            return
        lang, footer = await _ctx(session, message.from_user)
        address = order.deposit_address

    # verify on-chain BEFORE accepting — decline a tx that didn't reach us (wrong
    # address / not found / far too old) on the user's face, with proof, and never
    # create an admin claim card for it
    ok, reject, verify = await _verify_deposit_tx(txid, address, order, lang)
    if not ok:
        await message.answer(reject + footer, reply_markup=hide_kb(),
                             disable_web_page_preview=True)
        return

    async with Session() as session:
        order = await session.get(Order, order_id)
        if order is None or order.status not in _CLAIMABLE:
            await message.answer("This order can't be confirmed by TXID anymore — "
                                 "contact support.", reply_markup=hide_kb())
            return
        order.claim_txid = txid
        await session.commit()
    await message.answer(texts.claim_submitted(order_id, lang) + footer,
                         reply_markup=hide_kb())
    await _post_claim_card(message.bot, order_id, txid, verify)


@router.message(ClaimFlow.txid)
async def claim_txid_not_text(message: Message) -> None:
    await message.answer("Please paste the <b>TXID</b> (transaction hash) as text — "
                         "not a photo — or tap ❌ Cancel.")