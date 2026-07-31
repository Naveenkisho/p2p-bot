import re

from aiogram import F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
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
from ..states import AddBank, EmailFlow

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
    was_in_task = await state.get_state() is not None
    await state.clear()
    async with Session() as session:
        support = await get_support(session)
        lang = await get_lang(session, message.from_user.id)
    await message.answer("❌ Cancelled." if was_in_task else "✔️", reply_markup=hide_kb())
    # land back on the full /start-style menu, greeting and data — same as the
    # ❌ Cancel button, so /cancel never drops the user on a bare line
    await message.answer(
        texts.welcome(message.from_user.first_name, message.from_user.id, support, lang),
        reply_markup=main_menu())


@router.message(Command("whoami"))
async def cmd_whoami(message: Message) -> None:
    """User's own Telegram ID — handy to share with support for a manual payout."""
    await message.answer(
        f"🆔 Your Telegram ID: <code>{message.from_user.id}</code>\n"
        "Tap to copy and send it to support if they ask for it.")


@router.callback_query(F.data == "emnudge:no")
async def email_nudge_dismiss(callback: CallbackQuery) -> None:
    """'No thanks' under the email nudge — acknowledge and drop the button."""
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.answer("No problem — add it anytime with /email.")


# per-user cap on /email submissions — the instant-activation check would
# otherwise let a user probe addresses to learn which are registered here
_email_try_times: dict[int, list] = {}
_EMAIL_TRIES_PER_HOUR = 6


def _email_try_throttled(uid: int) -> bool:
    import time as _tm
    now = _tm.time()
    times = [t for t in _email_try_times.get(uid, []) if now - t < 3600]
    if len(times) >= _EMAIL_TRIES_PER_HOUR:
        _email_try_times[uid] = times
        return True
    times.append(now)
    _email_try_times[uid] = times
    return False


async def _submit_email(message: Message, cand: str,
                        state: FSMContext | None = None) -> None:
    """One pasted address → instant activation (already verified anywhere in
    the system) or the OTP dance. Shared by /email, the guided flow, and the
    paste-an-email-in-chat shortcut."""
    from ..models import User
    from ..sender import issue_email_otp
    uid = message.from_user.id
    if _email_try_throttled(uid):
        await message.answer("Too many email attempts this hour — please wait "
                             "a bit and try again.")
        return
    # already proven somewhere in the system? (a website account verified it
    # by OTP/Google, or another Telegram user OTP'd it) — deliverability and
    # ownership were shown once; don't make the customer do the dance twice
    from sqlalchemy import func as _f
    from ..models import Account
    from ..sender import _EMAIL_RE as _EM
    cand = cand.strip()
    if _EM.match(cand):
        low = cand.lower()
        async with Session() as session:
            acct_ok = await session.scalar(select(Account.id).where(
                _f.lower(Account.email) == low,
                Account.email_verified.is_(True)).limit(1))
            tg_ok = await session.scalar(select(User.id).where(
                _f.lower(User.email) == low,
                User.email_verified.is_(True), User.id != uid).limit(1))
        if acct_ok or tg_ok:
            # instant activation would skip the OTP — so make the customer
            # LOOK at the address once before any order mail can go there
            # (a typo that hits someone else's registered address must never
            # silently receive this user's order details)
            if state is not None:
                await state.set_state(EmailFlow.confirm)
                await state.update_data(email=cand)
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✅ Yes — send my receipts there",
                                          callback_data="emconf:yes")],
                    [InlineKeyboardButton(text="✏️ No — let me re-type it",
                                          callback_data="emconf:no")]])
                await message.answer(
                    "⚠️ <b>Please check this is YOUR address:</b>\n"
                    f"<code>{esc(cand)}</code>\n\n"
                    "Your order details and payout receipts will be sent "
                    "there. Is it correct?", reply_markup=kb)
                return
            # no FSM context — fall through to the OTP, which proves ownership
    ok, result = await issue_email_otp(uid, cand)
    if not ok:
        await message.answer(f"⚠️ {esc(result)}")
        return
    await message.answer(
        f"📨 Code sent to <code>{esc(result)}</code> — check the inbox (and "
        "spam folder), then <b>just send the 6-digit code here</b>.\n"
        "It expires in 15 minutes.")


@router.message(Command("email"))
async def cmd_email(message: Message, state: FSMContext) -> None:
    """Add (or remove) an email for order updates. Bare /email guides the user:
    they just paste the address as the next message — no syntax to learn."""
    from ..models import User
    args = (message.text or "").split(maxsplit=1)
    arg = args[1].strip() if len(args) > 1 else ""
    uid = message.from_user.id
    await state.clear()
    if arg.lower() in ("off", "remove", "delete"):
        async with Session() as session:
            u = await session.get(User, uid)
            if u is not None:
                u.email = ""
                u.email_verified = False
                await session.commit()
        await message.answer("✅ Email removed — order updates by email are off.")
        return
    if not arg:
        async with Session() as session:
            u = await session.get(User, uid)
        if u is not None and u.email and u.email_verified:
            await message.answer(
                f"📧 Order updates go to <code>{esc(u.email)}</code> (verified).\n"
                "To change it, just send the new address here.\n"
                "<code>/email off</code> — stop email updates")
            await state.set_state(EmailFlow.address)
        else:
            await message.answer(
                "📧 Get your order confirmations, deposit alerts and payment "
                "receipts by email.\n\n<b>Just send your email address here</b> "
                "— e.g. <code>name@gmail.com</code>", reply_markup=cancel_kb())
            await state.set_state(EmailFlow.address)
        return
    await _submit_email(message, arg, state)


@router.message(EmailFlow.address, F.text)
async def email_address_typed(message: Message, state: FSMContext) -> None:
    """The guided step: whatever they paste next is the address. A command
    quietly closes the step; a malformed address is asked again."""
    from ..sender import _EMAIL_RE as _EM
    text = (message.text or "").strip()
    if text.startswith("/"):
        await state.clear()      # they moved on — the email step expires
        return
    if not _EM.match(text):
        await message.answer(
            "That doesn't look like an email address — it should be like "
            "<code>name@gmail.com</code>. Send it again, or tap ❌ Cancel.")
        return
    await state.clear()
    await _submit_email(message, text, state)


async def _activate_email(uid: int, username, first_name, cand: str) -> None:
    from ..models import User
    async with Session() as session:
        u = await get_or_create_user(session, uid, username, first_name)
        u.email = cand
        u.email_verified = True
        await session.commit()


@router.callback_query(EmailFlow.confirm, F.data == "emconf:yes")
async def email_confirm_yes(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    cand = (data.get("email") or "").strip()
    await state.clear()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    if not cand:
        await callback.answer("Expired — send the address again.", show_alert=True)
        return
    await _activate_email(callback.from_user.id, callback.from_user.username,
                          callback.from_user.first_name, cand)
    await callback.answer()
    await callback.message.answer(
        f"✅ <code>{esc(cand)}</code> confirmed — your order confirmations and "
        "payout receipts now go there. No code needed.\n"
        "<code>/email off</code> to stop.")


@router.callback_query(EmailFlow.confirm, F.data == "emconf:no")
async def email_confirm_no(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(EmailFlow.address)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.answer()
    await callback.message.answer("No problem — send the correct address, "
                                  "e.g. <code>name@gmail.com</code>")


@router.callback_query(F.data.startswith("emconf:"))
async def email_confirm_expired(callback: CallbackQuery) -> None:
    await callback.answer("This check expired — just send your email address "
                          "again.", show_alert=True)


@router.message(EmailFlow.confirm, F.text)
async def email_confirm_retype(message: Message, state: FSMContext) -> None:
    """They typed instead of tapping — treat a fresh address as a correction;
    a command closes the step."""
    text = (message.text or "").strip()
    await state.clear()
    if text.startswith("/"):
        return
    from ..sender import _EMAIL_RE as _EM
    if _EM.match(text):
        await _submit_email(message, text, state)
    else:
        await message.answer("Tap one of the buttons above, or send the "
                             "correct address like <code>name@gmail.com</code>.")


@router.message(EmailFlow.address)
async def email_address_not_text(message: Message) -> None:
    await message.answer("Please send the email address as text — "
                         "e.g. <code>name@gmail.com</code> — or tap ❌ Cancel.")


async def _submit_code(message: Message, code: str) -> None:
    """One pasted 6-digit code → the address goes live (or a clear error)."""
    from ..models import User
    from ..sender import verify_email_otp
    ok, result = verify_email_otp(message.from_user.id, code)
    if not ok:
        await message.answer(f"⚠️ {esc(result)}")
        return
    async with Session() as session:
        u = await session.get(User, message.from_user.id)
        if u is None:
            await message.answer("Please /start the bot first, then try again.")
            return
        u.email = result
        u.email_verified = True
        await session.commit()
    await message.answer(
        f"✅ <b>{esc(result)} verified.</b> You'll now get order confirmations, "
        "deposit alerts and payment receipts by email — alongside the usual "
        "Telegram updates. Change it any time with /email.")


@router.message(Command("verify"))
async def cmd_verify(message: Message) -> None:
    """Old-style /verify 123456 still works — but just pasting the 6 digits
    in chat does the same thing now."""
    args = (message.text or "").split(maxsplit=1)
    code = args[1].strip() if len(args) > 1 else ""
    if not code:
        await message.answer("Just send the <b>6-digit code</b> we emailed you "
                             "as a plain message — no command needed.")
        return
    await _submit_code(message, code)


@router.message(StateFilter(None), F.text.regexp(r"^\s*\d{6}\s*$"))
async def pasted_code(message: Message) -> None:
    """A bare 6-digit number in chat: it's the email code IF one is pending
    for this user; otherwise it's not ours to interpret — stay silent."""
    from ..sender import has_pending_otp
    if not has_pending_otp(message.from_user.id):
        return
    await _submit_code(message, (message.text or "").strip())


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


@router.message(StateFilter(None),
                F.text.regexp(r"^\s*[^@\s]+@[^@\s]+\.[^@\s]+\s*$"))
async def pasted_email(message: Message, state: FSMContext) -> None:
    """A bare email pasted into the chat with no command — treat it as 'send my
    receipts here'. Already-verified addresses get the are-you-sure step;
    otherwise the usual 6-digit code."""
    await _submit_email(message, (message.text or "").strip(), state)
