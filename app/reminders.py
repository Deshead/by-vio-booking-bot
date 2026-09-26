"""Opt-in visit reminders. No client receives a reminder by default."""

import asyncio
import logging
from datetime import datetime

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

from app.catalog import BUSINESS_TZ, TIMEZONE_LABEL, now_local
from app.storage import BookingStore
from app.store_provider import StoreProvider


logger = logging.getLogger(__name__)


async def send_due_reminders(
    bot: Bot, store: BookingStore, *, now: datetime | None = None,
    blocked: set | None = None,
) -> int:
    current = now or now_local()
    ignored = blocked if blocked is not None else set()
    due = await asyncio.to_thread(store.due_reminders, current, within_minutes=120)
    sent = 0
    for item in due:
        key = (str(store.path), item.id, item.version)
        if key in ignored:
            continue
        latest = await asyncio.to_thread(store.get_booking, item.id)
        if (latest is None or latest.status != "confirmed"
                or latest.version != item.version or not latest.reminders_enabled
                or latest.reminder_sent_version == latest.version
                or latest.starts_at <= current):
            continue
        start = latest.starts_at.astimezone(BUSINESS_TZ)
        end = latest.ends_at.astimezone(BUSINESS_TZ)
        try:
            await bot.send_message(
                chat_id=latest.user_id,
                text=(
                    "BY VIO · СКОРО ВСТРЕЧА 🔔\n\n"
                    f"{latest.service_name}\n"
                    f"{start:%d.%m.%Y} · {start:%H:%M}–{end:%H:%M}\n"
                    f"Мастер: {latest.master_name}\n"
                    f"Время: {TIMEZONE_LABEL}\n\n"
                    "Все детали и управление визитом — в «📋 Мои записи».\n"
                    "Если вы недавно переносили или отменяли визит, проверьте актуальную карточку."
                ),
            )
        except TelegramForbiddenError:
            ignored.add(key)
            logger.info("Напоминания недоступны для записи %s", latest.id)
            continue
        except TelegramRetryAfter:
            # The limit applies to the bot, including every personal demo store.
            raise
        except Exception as exc:
            logger.warning("Напоминание записи %s отложено: %s", latest.id, type(exc).__name__)
            continue
        await asyncio.to_thread(store.mark_reminder_sent, latest.id, latest.version)
        sent += 1
    return sent


async def reminder_loop(bot: Bot, provider: StoreProvider):
    blocked: set = set()
    while True:
        delay = provider.settings.reminder_interval
        try:
            for store in await provider.reminder_stores():
                await send_due_reminders(bot, store, blocked=blocked)
        except TelegramRetryAfter as exc:
            delay = max(delay, exc.retry_after)
            logger.warning("Telegram ограничил напоминания; повтор через %s с", delay)
        except Exception as exc:
            logger.warning("Проверка напоминаний отложена: %s", type(exc).__name__)
        await asyncio.sleep(delay)
