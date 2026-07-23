from aiogram import F, Router
from aiogram.types import CallbackQuery

from .. import texts
from ..db import Session, get_lang, get_support

router = Router(name="buy")


@router.callback_query(F.data == "menu:buy")
async def buy_menu(callback: CallbackQuery) -> None:
    async with Session() as session:
        support = await get_support(session)
        lang = await get_lang(session, callback.from_user.id)
    await callback.message.answer(
        texts.buy_soon(support, lang)
        + texts.trust_footer(callback.from_user.first_name, callback.from_user.id,
                             support, lang))
    await callback.answer()
