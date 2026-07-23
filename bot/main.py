import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, ErrorEvent

from .config import settings
from .db import init_db
from .handlers import routers


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    await init_db()

    bot = Bot(settings.bot_token,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    for router in routers:
        dp.include_router(router)

    async def on_error(event: ErrorEvent) -> None:
        # never leave a tap hanging on a spinner, whatever went wrong
        logging.getLogger(__name__).error("handler error", exc_info=event.exception)
        callback = event.update.callback_query
        if callback is not None:
            try:
                await callback.answer("Something went wrong — please try again.",
                                      show_alert=True)
            except Exception:
                pass

    dp.errors.register(on_error)

    await bot.set_my_commands([
        BotCommand(command="start", description="Main menu"),
        BotCommand(command="cancel", description="Cancel the current step"),
    ])

    logging.getLogger(__name__).info("P2P desk bot starting (polling)…")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
