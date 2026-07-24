"""Keeps a user on-task: a Cancel that works from any step, and a block on
tapping stale inline buttons from earlier messages while mid-flow."""

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from ..keyboards import CANCEL_TEXT, hide_kb, main_menu
from ..states import AddBank, BankForOrder, RefundFlow, SellFlow

router = Router(name="guard")

# States where the bot is waiting for the user to TYPE something. A stray tap
# on an old inline button here should not silently start something else.
TASK_STATES = (SellFlow.amount, SellFlow.bank_details, BankForOrder.details,
               AddBank.details, RefundFlow.address)


@router.message(F.text == CANCEL_TEXT)
async def cancel_task(message: Message, state: FSMContext) -> None:
    was_in_task = await state.get_state() is not None
    await state.clear()
    if was_in_task:
        await message.answer("❌ Cancelled.", reply_markup=hide_kb())
        await message.answer("Back to the menu 👇", reply_markup=main_menu())
    else:
        await message.answer("Nothing to cancel — you're at the menu.",
                             reply_markup=hide_kb())


# Any callback fired while mid-typing (except picking a saved bank, prefix "pb")
# is a stale button from an earlier message — nudge instead of acting on it.
@router.callback_query(StateFilter(*TASK_STATES), ~F.data.startswith("pb"))
async def block_stray(callback: CallbackQuery) -> None:
    await callback.answer("⚠️ Please finish the current step, or tap ❌ Cancel first.",
                          show_alert=True)
