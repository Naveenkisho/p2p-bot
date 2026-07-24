"""Keeps a user on-task: a Cancel that works from any step, and a block on
tapping stale inline buttons from earlier messages while mid-flow."""

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from .. import texts
from ..db import Session, get_lang, get_support
from ..keyboards import CANCEL_TEXT, hide_kb, main_menu
from ..states import AddBank, BankForOrder, RefundFlow, SellFlow

router = Router(name="guard")

# States where the bot is waiting for the user to TYPE something. A stray tap
# on an old inline button here should not silently start something else.
TASK_STATES = (SellFlow.amount, SellFlow.bank_details, BankForOrder.details,
               AddBank.details, RefundFlow.txid)


@router.message(F.text == CANCEL_TEXT)
async def cancel_task(message: Message, state: FSMContext) -> None:
    was_in_task = await state.get_state() is not None
    await state.clear()
    async with Session() as session:
        support = await get_support(session)
        lang = await get_lang(session, message.from_user.id)
    await message.answer("❌ Cancelled." if was_in_task else "✔️",
                         reply_markup=hide_kb())
    # land the user back on the full /start-style menu, greeting and all
    await message.answer(
        texts.welcome(message.from_user.first_name, message.from_user.id,
                      support, lang),
        reply_markup=main_menu())


# Any callback fired while mid-typing (except picking a bank, prefix "pb"/"pbk")
# is a stale button from an earlier message — nudge instead of acting on it.
@router.callback_query(StateFilter(*TASK_STATES), ~F.data.startswith("pb"))
async def block_stray(callback: CallbackQuery) -> None:
    await callback.answer("⚠️ Please finish the current step, or tap ❌ Cancel first.",
                          show_alert=True)
