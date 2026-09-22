"""Bot process: long polling (against the local Bot API server when TELEGRAM_API_BASE is set)."""

import asyncio
import logging

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import MenuButtonWebApp, WebAppInfo

from app.bot import keyboards as kb
from app.bot.setup import bot_commands, create_dispatcher
from app.core.config import Settings, get_settings
from app.core.db import get_sessionmaker
from app.core.logging import setup_logging

logger = logging.getLogger(__name__)


def build_bot(settings: Settings) -> Bot:
    session = None
    if settings.telegram_api_base:
        # VERIFY: is_local=True lets the local Bot API server read/write files by path (large uploads)
        session = AiohttpSession(api=TelegramAPIServer.from_base(settings.telegram_api_base, is_local=True))
    return Bot(token=settings.bot_token, session=session)


async def on_startup(bot: Bot, settings: Settings) -> None:
    try:
        await bot.set_my_commands(bot_commands())
    except Exception:
        logger.warning("could not set bot commands", exc_info=True)
    url = kb.mini_app_url(settings)
    if url is None:
        logger.warning("PUBLIC_BASE_URL is not https: skipping the Mini App menu button and web_app buttons")
        return
    try:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="Video yuklash", web_app=WebAppInfo(url=url))
        )
    except Exception:
        logger.warning("could not set the Mini App menu button", exc_info=True)


async def run() -> None:
    settings = get_settings()
    if not settings.bot_token:
        logger.error("BOT_TOKEN is empty: set it in .env")
        return
    bot = build_bot(settings)
    storage = RedisStorage.from_url(settings.redis_url)
    dp = create_dispatcher(settings, get_sessionmaker(), storage)
    try:
        await on_startup(bot, settings)
        logger.info("bot started")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await storage.close()
        await bot.session.close()


def main() -> None:
    setup_logging()
    asyncio.run(run())


if __name__ == "__main__":
    main()
