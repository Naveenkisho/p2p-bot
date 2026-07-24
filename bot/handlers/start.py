import re

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from .. import texts
from ..config import SERVICES
from ..db import Session, get_lang, get_or_create_user, get_rates, get_support
from ..helpers import edit_or_send, esc, strip_kb
from ..keyboards import (
    BankRmCb,
    banks_menu_kb,
    cancel_kb,
    hide_kb,
    language_kb,
    main_menu,
    support_row_kb,
    with_back,
)
from ..models import OPEN_STATUSES, BankCard, Order
from ..states import AddBank

router = Router(name="start")


def bank_details_error(details: str) -> str | None:
    lines = details.splitlines()
    if len(lines) < 3:
        return ("Please send bank name, account holder, account number "
                "and IFSC — one per line.")
    if len(details) > 350 or len(lines) > 8:
        return "That's too long — just the bank name, holder, account number and IFSC please."
    return None


def make_bank_label(details: str) -> str:
    lines = [ln.strip() for ln in details.strip().splitlines() if ln.strip()]
    bank_name = lines[0] if lines else "Bank"
    # prefer the value after a "Bank:" label if the user used the labelled format
    for ln in lines:
        if ":" in ln and ln.split(":", 1)[0].strip().lower() in ("bank", "bank name"):
            bank_name = ln.split(":", 1)[1].strip() or bank_name
            break
    bank_name = bank_name[:20]
    digits = re.findall(r"\d{6,}", details)
    if not digits:
        return bank_name
    account = max(digits, key=len)  # longest run = the account number, not the IFSC
    return f"{bank_name} ••{account[-4:]}"


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    async with Session() as session:
        user = await get_or_create_user(session, message.from_user.id,
                                        message.from_user.username, message.from_user.first_name)
        support = await get_support(session)
    if user.banned:
        await message.answer(texts.BANNED)
        return
    await message.answer(
        texts.welcome(message.from_user.first_name, message.from_user.id, support,
                      user.lang),
        reply_markup=main_menu())


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Cancelled — back to the menu.", reply_markup=main_menu())


@router.message(Command("whoami"))
async def cmd_whoami(message: Message) -> None:
    """User's own Telegram ID — handy to share with support for a manual payout."""
    await message.answer(
        f"🆔 Your Telegram ID: <code>{message.from_user.id}</code>\n"
        "Tap to copy and send it to support if they ask for it.")


@router.callback_query(F.data == "menu:home")
async def menu_home(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    async with Session() as session:
        support = await get_support(session)
        lang = await get_lang(session, callback.from_user.id)
    text = texts.welcome(callback.from_user.first_name, callback.from_user.id,
                         support, lang)
    try:
        await callback.message.edit_text(text, reply_markup=main_menu())
    except Exception:
        # old/inaccessible message — send a fresh menu instead
        await callback.bot.send_message(callback.from_user.id, text,
                                        reply_markup=main_menu())
    await callback.answer()


@router.callback_query(F.data == "menu:lang")
async def menu_lang(callback: CallbackQuery) -> None:
    await callback.message.answer(texts.CHOOSE_LANGUAGE, reply_markup=language_kb())
    await callback.answer()


@router.callback_query(F.data.startswith("lang:"))
async def set_language(callback: CallbackQuery) -> None:
    lang = callback.data.split(":", 1)[1]
    if lang not in ("en", "hi"):
        await callback.answer()
        return
    async with Session() as session:
        user = await get_or_create_user(session, callback.from_user.id,
                                        callback.from_user.username,
                                        callback.from_user.first_name)
        user.lang = lang
        await session.commit()
        support = await get_support(session)
    await strip_kb(callback.message)
    await callback.message.answer(texts.language_saved(lang))
    await callback.message.answer(
        texts.welcome(callback.from_user.first_name, callback.from_user.id,
                      support, lang),
        reply_markup=main_menu())
    await callback.answer()


@router.callback_query(F.data == "menu:rates")
async def menu_rates(callback: CallbackQuery) -> None:
    async with Session() as session:
        rates = await get_rates(session)
    if not rates:
        await callback.answer(texts.DESK_CLOSED, show_alert=True)
        return
    lines = ["📈 <b>Live rates</b>", ""]
    for key, rate in rates.items():
        lines.append(f"• {SERVICES[key]} — <b>1$ / ₹{rate:g}</b>")
    await edit_or_send(callback, "\n".join(lines), with_back())
    await callback.answer()


@router.callback_query(F.data == "menu:orders")
async def my_orders(callback: CallbackQuery) -> None:
    async with Session() as session:
        orders = (await session.scalars(
            select(Order).where(Order.user_id == callback.from_user.id)
            .order_by(Order.id.desc()).limit(10)
        )).all()
        support = await get_support(session)
        lang = await get_lang(session, callback.from_user.id)
    footer = texts.trust_footer(callback.from_user.first_name,
                                callback.from_user.id, support, lang)
    if not orders:
        empty = ("📋 Abhi tak koi order nahi — 💵 USDT Sell dabakar shuru karein!"
                 if lang == "hi" else
                 "📋 You have no orders yet — tap 💵 USDT Sell to start!")
        await edit_or_send(callback, empty + footer, with_back())
        await callback.answer()
        return
    heading = "📋 <b>Aapke last orders</b>" if lang == "hi" else "📋 <b>Your last orders</b>"
    lines = [heading, ""]
    for o in orders:
        status = o.status.value if hasattr(o.status, "value") else str(o.status)
        emoji = texts.STATUS_EMOJI.get(status, "•")
        lines.append(f"{emoji} <code>{texts.tag(o.id)}</code> — {o.usd_amount:g}$ "
                     f"→ ₹{o.inr_amount:,.2f} — <i>{status}</i>")
    await edit_or_send(callback, "\n".join(lines) + footer, with_back())
    await callback.answer()


@router.callback_query(F.data == "menu:support")
async def menu_support(callback: CallbackQuery) -> None:
    async with Session() as session:
        support = await get_support(session)
        lang = await get_lang(session, callback.from_user.id)
    await edit_or_send(callback, texts.support_msg(lang),
                       with_back(support_row_kb(support.split())))
    await callback.answer()


@router.callback_query(F.data == "menu:guarantee")
async def menu_guarantee(callback: CallbackQuery) -> None:
    async with Session() as session:
        support = await get_support(session)
        lang = await get_lang(session, callback.from_user.id)
    await edit_or_send(callback, texts.guarantee(lang),
                       with_back(support_row_kb(support.split())))
    await callback.answer()


async def _banks_view(user_id: int) -> tuple[str, object]:
    async with Session() as session:
        cards = (await session.scalars(
            select(BankCard).where(BankCard.user_id == user_id).order_by(BankCard.id)
        )).all()
    if not cards:
        text = "🏦 <b>My Bank Cards</b>\n\nNo banks saved yet — add one below."
    else:
        blocks = [f"🏦 <b>{esc(c.label)}</b>\n<code>{esc(c.details)}</code>" for c in cards]
        text = "🏦 <b>My Bank Cards</b>\n\n" + "\n\n".join(blocks)
    return text, banks_menu_kb(cards)


@router.callback_query(F.data == "menu:banks")
async def menu_banks(callback: CallbackQuery) -> None:
    text, kb = await _banks_view(callback.from_user.id)
    await edit_or_send(callback, text, kb)
    await callback.answer()


@router.callback_query(F.data == "banks:add")
async def banks_add(callback: CallbackQuery, state: FSMContext) -> None:
    async with Session() as session:
        lang = await get_lang(session, callback.from_user.id)
    await state.set_state(AddBank.details)
    await callback.message.answer(texts.ask_bank_new(lang), reply_markup=cancel_kb())
    await callback.answer()


@router.message(AddBank.details, F.text)
async def banks_add_details(message: Message, state: FSMContext) -> None:
    details = message.text.strip()
    error = bank_details_error(details)
    if error:
        await message.answer(error)
        return
    await state.clear()
    async with Session() as session:
        session.add(BankCard(user_id=message.from_user.id,
                             label=make_bank_label(details), details=details))
        await session.commit()
    await message.answer("✅ Bank saved.", reply_markup=hide_kb())
    text, kb = await _banks_view(message.from_user.id)
    await message.answer(text, reply_markup=kb)


@router.message(AddBank.details)
async def banks_add_not_text(message: Message) -> None:
    await message.answer("Please <b>type</b> the bank details as text — "
                         "not a photo or file.")


@router.callback_query(BankRmCb.filter())
async def banks_remove(callback: CallbackQuery, callback_data: BankRmCb) -> None:
    async with Session() as session:
        card = await session.get(BankCard, callback_data.card_id)
        if card is None or card.user_id != callback.from_user.id:
            await callback.answer("Not found.", show_alert=True)
            return
        open_order = await session.scalar(
            select(Order).where(Order.bank_card_id == card.id,
                                Order.status.in_([s.value for s in OPEN_STATUSES]))
            .limit(1))
        if open_order is not None:
            await callback.answer(
                f"This bank is used by open order #{open_order.id} — "
                "you can remove it once that order finishes.", show_alert=True)
            return
        await session.delete(card)
        await session.commit()
    text, kb = await _banks_view(callback.from_user.id)
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        await callback.bot.send_message(callback.from_user.id, text, reply_markup=kb)
    await callback.answer("Removed")
