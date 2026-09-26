import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendMessage

from app.reminders import reminder_loop, send_due_reminders
from app.settings import Settings
from app.storage import BookingStore
from app.store_provider import StoreProvider


class DemoIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "bookings.sqlite3"

    async def test_each_demo_visitor_gets_a_separate_persistent_store(self):
        settings = Settings(demo_mode=True)
        provider = StoreProvider(self.path, settings)
        first, second = await asyncio.gather(provider.get_store(41), provider.get_store(42))
        self.assertNotEqual(first.path, second.path)
        self.assertFalse(self.path.exists())
        self.assertTrue(settings.is_admin(41))
        first.create_booking(
            request_id="one", user_id=41, service_id="manicure", service_name="Маникюр",
            master_id="violetta", master_name="Виолетта",
            starts_at=datetime(2026, 10, 1, 9, tzinfo=timezone.utc), duration_minutes=120,
            client_name="Анна", phone="+79991234567",
        )
        self.assertEqual(second.list_user_bookings(41), [])
        reopened = StoreProvider(self.path, settings)
        self.assertEqual(len((await reopened.get_store(41)).list_user_bookings(41)), 1)
        self.assertEqual(len(await reopened.reminder_stores()), 2)

    async def test_live_store_is_shared_but_admin_access_is_explicit(self):
        settings = Settings(demo_mode=False, admin_ids=frozenset({41}))
        provider = StoreProvider(self.path, settings)
        first = await provider.get_store(41)
        second = await provider.get_store(42)
        self.assertEqual(first.path, second.path)
        self.assertTrue(settings.is_admin(41))
        self.assertFalse(settings.is_admin(42))


class ReminderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = BookingStore(Path(directory.name) / "bookings.sqlite3")
        self.store.initialize()
        self.now = datetime(2026, 10, 1, 8, tzinfo=timezone.utc)
        self.bot = AsyncMock()

    def booking(self, request_id, *, enabled, offset):
        return self.store.create_booking(
            request_id=request_id, user_id=41, service_id="manicure", service_name="Маникюр",
            master_id=request_id, master_name="Виолетта",
            starts_at=self.now + timedelta(minutes=offset), duration_minutes=120,
            client_name="Анна", phone="+79991234567", reminders_enabled=enabled,
        )

    async def test_reminders_require_opt_in_and_are_not_repeated_after_success(self):
        self.booking("off", enabled=False, offset=90)
        self.booking("later", enabled=True, offset=300)
        self.booking("due", enabled=True, offset=90)
        self.assertEqual(await send_due_reminders(self.bot, self.store, now=self.now), 1)
        self.bot.send_message.assert_awaited_once()
        self.assertEqual(await send_due_reminders(self.bot, self.store, now=self.now), 0)
        self.bot.send_message.assert_awaited_once()

    async def test_failed_delivery_can_be_retried_and_cancelled_booking_is_skipped(self):
        item = self.booking("due", enabled=True, offset=90)
        self.bot.send_message.side_effect = OSError("offline")
        self.assertEqual(await send_due_reminders(self.bot, self.store, now=self.now), 0)
        self.assertEqual(self.store.get_booking(item.id).reminder_sent_version, 0)
        self.bot.send_message.side_effect = None
        self.store.cancel_booking(item.id, user_id=41, expected_version=1, now=self.now)
        self.assertEqual(await send_due_reminders(self.bot, self.store, now=self.now), 0)

    async def test_telegram_backoff_stops_all_demo_stores_for_required_delay(self):
        item = self.booking("due", enabled=True, offset=90)
        self.bot.send_message.side_effect = TelegramRetryAfter(
            method=SendMessage(chat_id=41, text="test"), message="Too many requests", retry_after=300,
        )
        second_store = AsyncMock()
        provider = SimpleNamespace(settings=Settings(), reminder_stores=AsyncMock(return_value=[self.store, second_store]))
        with patch("app.reminders.now_local", return_value=self.now), patch(
            "app.reminders.asyncio.sleep", new_callable=AsyncMock,
        ) as sleep:
            sleep.side_effect = asyncio.CancelledError()
            with self.assertRaises(asyncio.CancelledError):
                await reminder_loop(self.bot, provider)
        sleep.assert_awaited_once_with(300)
        self.bot.send_message.assert_awaited_once()
        second_store.due_reminders.assert_not_called()
        self.assertEqual(self.store.get_booking(item.id).reminder_sent_version, 0)


if __name__ == "__main__":
    unittest.main()
