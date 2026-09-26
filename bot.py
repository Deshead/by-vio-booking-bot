import asyncio
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
from urllib.request import getproxies, proxy_bypass

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.types import BotCommand
from dotenv import load_dotenv

from app.handlers.menu import router as menu_router
from app.handlers.start import router as start_router
from app.handlers.booking import router as booking_router
from app.handlers.commands import router as commands_router
from app.handlers.appointments import router as appointments_router
from app.handlers.admin import router as admin_router
from app.health import start_health_server
from app.reminders import reminder_loop
from app.runtime import AlreadyRunningError, single_instance
from app.settings import Settings
from app.storage import BookingStore
from app.store_provider import BookingStoreMiddleware, StoreProvider


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", encoding="utf-8-sig")

TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    raise RuntimeError("BOT_TOKEN не найден в .env")


database_path = Path(os.getenv("BOOKING_DB_PATH", "data/bookings.sqlite3"))
if not database_path.is_absolute():
    database_path = BASE_DIR / database_path
booking_store = BookingStore(database_path)
settings = Settings.from_env()
store_provider = StoreProvider(database_path, settings)

dp = Dispatcher(events_isolation=SimpleEventIsolation())
dp["settings"] = settings
dp.update.outer_middleware(BookingStoreMiddleware(store_provider))
dp.include_router(start_router)
dp.include_router(commands_router)
dp.include_router(admin_router)
dp.include_router(menu_router)
dp.include_router(appointments_router)
dp.include_router(booking_router)
logger = logging.getLogger(__name__)


def get_proxy_url() -> str | None:
    explicit_proxy = os.getenv("BOT_PROXY_URL", "").strip()
    if explicit_proxy.lower() in {"none", "direct"}:
        return None
    if explicit_proxy:
        return explicit_proxy
    if proxy_bypass("api.telegram.org"):
        return None
    proxies = getproxies()
    return proxies.get("https") or proxies.get("http") or proxies.get("all")


async def main():
    if not settings.demo_mode:
        await store_provider.get_store(1)
    proxy_url = get_proxy_url()
    logger.info("Подключение к Telegram: %s", "через прокси" if proxy_url else "напрямую")
    session = AiohttpSession(proxy=proxy_url)
    health_status = {"ready": False}
    health_runner = await start_health_server(int(os.getenv("PORT", "0")), health_status)
    try:
        async with Bot(token=TOKEN, session=session) as bot:
            me = await bot.get_me(request_timeout=20)
            await bot.set_my_commands([
                BotCommand(command="start", description="Главное меню"),
                BotCommand(command="book", description="Записаться на услугу"),
                BotCommand(command="my", description="Мои записи: перенос и отмена"),
                BotCommand(command="services", description="Услуги и мастер"),
                BotCommand(command="help", description="Как пользоваться ботом"),
                BotCommand(command="admin", description="Кабинет мастера"),
                BotCommand(command="myid", description="Мой Telegram ID"),
            ])
            await bot.set_my_short_description(
                short_description="BY VIO · Запись к мастеру. Услуги, свободные окна и управление визитом — в Telegram."
            )
            await bot.set_my_name(name="BY VIO · Запись к Виолетте")
            await bot.set_my_description(description=(
                "BY VIO · Бьюти-мастер Виолетта\n"
                "Маникюр, Френч и педикюр · салон «Ювента».\n\n"
                "Выбирайте свободное время, переносите визит и управляйте записями в Telegram.\n"
                + ("\nДемо для портфолио: личный пример салона для каждого посетителя."
                   if settings.demo_mode else "\nНажмите «Начать», чтобы записаться.")
            ))
            logger.info("Бот @%s подключён. Режим: %s", me.username, "DEMO" if settings.demo_mode else "SALON")
            health_status["ready"] = True
            reminders = asyncio.create_task(reminder_loop(bot, store_provider))
            try:
                await dp.start_polling(bot, close_bot_session=False)
            finally:
                health_status["ready"] = False
                reminders.cancel()
                await asyncio.gather(reminders, return_exceptions=True)
    finally:
        if health_runner is not None:
            await health_runner.cleanup()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [PID %(process)d] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            RotatingFileHandler(BASE_DIR / "bot.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"),
        ],
    )
    try:
        with single_instance(BASE_DIR / ".bot.lock"):
            asyncio.run(main())
    except AlreadyRunningError:
        logger.error("Бот уже запущен. Перед повторным запуском остановите работающую копию.")
        raise SystemExit(1)
    except KeyboardInterrupt:
        logger.info("Бот остановлен")
