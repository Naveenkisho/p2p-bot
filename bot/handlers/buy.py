from aiogram import F, Router
from aiogram.types import CallbackQuery

from .. import texts

router = Router(name="buy")


@router.callback_query(F.data == "menu:buy")
async def buy_menu(callback: CallbackQuery) -> None:
    await callback.message.answer(texts.BUY_SOON)
    await callback.answer()
