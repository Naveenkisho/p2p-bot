from aiogram import Router
from aiogram.types import CallbackQuery

router = Router(name="fallback")


@router.callback_query()
async def unhandled_callback(callback: CallbackQuery) -> None:
    """Last-resort answer so no tap ever hangs on a spinner: admin buttons
    tapped by non-admins in the group, or buttons from expired sessions."""
    data = callback.data or ""
    if data.startswith("a:"):
        await callback.answer("Admins only.", show_alert=True)
    else:
        await callback.answer("This button has expired — press /start.", show_alert=True)
