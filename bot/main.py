import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, ErrorEvent

from .config import settings
from .db import Session, get_bot_token, init_db
from .handlers import routers
from .panel import start_panel
from .scanner import scan_loop


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    await init_db()
    log = logging.getLogger(__name__)

    async with Session() as session:
        token = await get_bot_token(session)   # DB (panel-set) wins over env

    if not token:
        # No token yet: run the web panel ONLY so the operator can set the
        # token (and admins) in Settings. Saving the token restarts the
        # process (systemd), which then boots the full bot below.
        log.warning("No bot token set — starting WEB PANEL ONLY. Add the bot "
                    "token in the panel's Settings and the bot will start.")
        panel_runner = await start_panel(None)
        if panel_runner is None:
            log.error("Nothing to run: no bot token AND no panel password. "
                      "Set P2P_BOT_TOKEN or P2P_PANEL_PASSWORD in .env.")
            return
        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            await panel_runner.cleanup()
        return

    bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
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

    logging.getLogger(__name__).info("P2P desk bot starting (polling + tron scan)…")
    await bot.delete_webhook(drop_pending_updates=True)
    scanner = asyncio.create_task(scan_loop(bot))

    def _scanner_done(task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception():
            logging.getLogger(__name__).error("scanner task exited unexpectedly",
                                              exc_info=task.exception())

    scanner.add_done_callback(_scanner_done)

    panel_runner = await start_panel(bot)  # web admin panel (if enabled)

    try:
        await dp.start_polling(bot)
    finally:
        scanner.cancel()
        if panel_runner is not None:
            await panel_runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
