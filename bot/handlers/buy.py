from aiogram import F, Router
from aiogram.types import CallbackQuery

from .. import texts
from ..db import Session, get_lang, get_support
from ..helpers import edit_or_send
from ..keyboards import support_row_kb, with_back

router = Router(name="buy")


@router.callback_query(F.data == "menu:buy")
async def buy_menu(callback: CallbackQuery) -> None:
    async with Session() as session:
        support = await get_support(session)
        lang = await get_lang(session, callback.from_user.id)
    await edit_or_send(callback, texts.buy_soon(support, lang),
                       with_back(support_row_kb(support.split())))
    await callback.answer()
