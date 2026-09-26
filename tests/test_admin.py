"""Dispatcher-level tests for the master's private dashboard."""

import csv
import io
import tempfile
import unittest
from datetime import datetime, timedelta
from itertools import count
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.methods import SendDocument, SendMessage
from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message, MessageEntity, Update, User

from app.catalog import BUSINESS_TZ
from app.handlers.admin import create_router
from app.settings import Settings
from app.storage import BookingStore


NOW = datetime(2026, 9, 27, 8, tzinfo=BUSINESS_TZ)


class AdminTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.store = BookingStore(self.directory / "admin.sqlite3")
        self.store.initialize()
        self.settings = Settings(demo_mode=False, admin_ids=frozenset({42}))
        self.bot = Bot(token="123456:TEST_TOKEN_FOR_LOCAL_TESTS")
        self.addAsyncCleanup(self.bot.session.close)
        self.dp = Dispatcher(events_isolation=SimpleEventIsolation())
        self.dp.include_router(create_router())
        self.addAsyncCleanup(self.dp.storage.close)
        self.sequence = count(1)
        self.last_message = None
        api_patch = patch.object(Bot, "__call__", new_callable=AsyncMock)
        self.api_call = api_patch.start()
        self.addCleanup(api_patch.stop)
        self.api_call.side_effect = self.fake_api
        clock_patch = patch("app.handlers.admin.now_local", return_value=NOW)
        clock_patch.start()
        self.addCleanup(clock_patch.stop)

    def fake_api(self, method, **kwargs):
        if isinstance(method, SendMessage):
            self.last_message = Message(
                message_id=next(self.sequence), date=NOW,
                chat=Chat(id=int(method.chat_id), type="private"), text=method.text,
                reply_markup=method.reply_markup if isinstance(method.reply_markup, InlineKeyboardMarkup) else None,
            )
            return self.last_message
        return True

    def seed(self, *, store=None, starts_at=None, client_name="Анна Петрова", user_id=17):
        return (store or self.store).create_booking(
            request_id=f"test-{next(self.sequence)}", user_id=user_id,
            service_id="manicure", service_name="Маникюр", master_id="violetta",
            master_name="Виолетта", starts_at=starts_at or NOW + timedelta(hours=1),
            duration_minutes=120, client_name=client_name, phone="+79991234567",
        )

    async def deliver(self, text, *, user_id=42, chat_type="private", store=None):
        await self.dp.feed_update(
            self.bot,
            Update(update_id=next(self.sequence), message=Message(
                message_id=next(self.sequence), date=NOW,
                chat=Chat(id=user_id if chat_type == "private" else -42, type=chat_type),
                from_user=User(id=user_id, is_bot=False, first_name="User"), text=text,
                entities=[MessageEntity(type="bot_command", offset=0, length=len(text))] if text.startswith("/") else [],
            )),
            settings=self.settings, booking_store=store if store is not None else self.store,
        )

    async def click(self, data, *, user_id=42, chat_type="private", store=None):
        await self.dp.feed_update(
            self.bot,
            Update(update_id=next(self.sequence), callback_query=CallbackQuery(
                id=str(next(self.sequence)), from_user=User(id=user_id, is_bot=False, first_name="User"),
                chat_instance="test", data=data,
                message=Message(
                    message_id=next(self.sequence), date=NOW,
                    chat=Chat(id=user_id if chat_type == "private" else -42, type=chat_type), text="UI",
                ),
            )),
            settings=self.settings, booking_store=store if store is not None else self.store,
        )

    def action(self, prefix):
        self.assertIsNotNone(self.last_message)
        self.assertIsInstance(self.last_message.reply_markup, InlineKeyboardMarkup)
        for row in self.last_message.reply_markup.inline_keyboard:
            for button in row:
                if button.callback_data and button.callback_data.startswith(prefix):
                    return button.callback_data
        self.fail(f"Missing admin action {prefix!r}")

    async def test_unauthorized_messages_callbacks_and_group_access_never_read_store(self):
        forbidden = Mock(spec=BookingStore)
        for user_id, chat_type in ((99, "private"), (42, "group")):
            with self.subTest(user=user_id, chat=chat_type):
                for text in ("/admin", "🛠 Кабинет мастера", "/admin_export"):
                    await self.deliver(text, user_id=user_id, chat_type=chat_type, store=forbidden)
                for data in ("adm:home", "adm:list:week:0", "adm:card:1", "adm:cancel:1:1", "adm:apply:fake", "adm:days", "adm:export"):
                    await self.click(data, user_id=user_id, chat_type=chat_type, store=forbidden)
        self.assertEqual(forbidden.mock_calls, [])
        self.assertFalse(any(isinstance(call.args[0], SendDocument) for call in self.api_call.await_args_list))

    async def test_demo_dashboard_and_cards_only_use_the_injected_personal_store(self):
        self.settings = Settings(demo_mode=True)
        self.seed(client_name="Чужой клиент")
        personal = BookingStore(self.directory / "personal-demo.sqlite3")
        personal.initialize()
        own = self.seed(store=personal, client_name="Демо клиент")
        await self.deliver("/admin", user_id=99, store=personal)
        self.assertIn("Деморежим", self.last_message.text)
        self.assertIn("Сегодня: записей 1 · занято 2 ч", self.last_message.text)
        await self.click("adm:list:week:0", user_id=99, store=personal)
        self.assertIn("Демо клиент", self.last_message.text)
        self.assertNotIn("Чужой клиент", self.last_message.text)
        await self.click(f"adm:card:{own.id}", user_id=99, store=personal)
        self.assertIn("Демо клиент", self.last_message.text)
        self.assertIn("+79991234567", self.last_message.text)
        self.assertNotIn("Чужой клиент", self.last_message.text)

    async def test_cancellation_requires_confirmation_and_rechecks_authorization(self):
        booking = self.seed()
        await self.click(f"adm:card:{booking.id}")
        await self.click(self.action("adm:cancel:"))
        confirmation = self.action("adm:apply:")
        self.assertEqual(self.store.get_booking(booking.id).status, "confirmed")
        await self.click(confirmation, user_id=99)
        self.assertEqual(self.store.get_booking(booking.id).status, "confirmed")
        # Losing the role also invalidates access to an already displayed button.
        self.settings = Settings(demo_mode=False)
        await self.click(confirmation)
        self.assertEqual(self.store.get_booking(booking.id).status, "confirmed")
        self.settings = Settings(demo_mode=False, admin_ids=frozenset({42}))
        await self.click(confirmation)
        cancelled = self.store.get_booking(booking.id)
        self.assertEqual(cancelled.status, "cancelled")
        self.assertEqual(cancelled.version, booking.version + 1)
        await self.click(confirmation)
        self.assertEqual(self.store.get_booking(booking.id), cancelled)
        self.assertIn("устарело", self.last_message.text)
        await self.click("adm:list:history:0")
        self.assertIn("Отменена", self.last_message.text)

    async def test_rescheduled_booking_cannot_be_cancelled_with_old_confirmation(self):
        booking = self.seed()
        await self.click(f"adm:cancel:{booking.id}:{booking.version}")
        confirmation = self.action("adm:apply:")
        moved = self.store.reschedule_booking(
            booking.id, user_id=booking.user_id, expected_version=booking.version,
            starts_at=booking.starts_at + timedelta(hours=2), now=NOW,
        )
        await self.click(confirmation)
        self.assertEqual(self.store.get_booking(booking.id), moved)
        self.assertIn("изменилась", self.last_message.text)

    async def test_closing_busy_day_is_refused_then_empty_day_can_close_and_reopen(self):
        booking = self.seed()
        busy_day = booking.starts_at.astimezone(BUSINESS_TZ).date()
        await self.click(f"adm:day:{busy_day.isoformat()}:block")
        await self.click(self.action("adm:apply:"))
        self.assertFalse(self.store.is_day_blocked("violetta", busy_day))
        self.assertIn("Сначала отмените или перенесите", self.last_message.text)
        self.assertEqual(self.store.get_booking(booking.id).status, "confirmed")

        empty_day = busy_day + timedelta(days=1)
        await self.click(f"adm:day:{empty_day.isoformat()}:block")
        self.assertFalse(self.store.is_day_blocked("violetta", empty_day))
        await self.click(self.action("adm:apply:"))
        self.assertTrue(self.store.is_day_blocked("violetta", empty_day))
        await self.click(f"adm:day:{empty_day.isoformat()}:unblock")
        self.assertTrue(self.store.is_day_blocked("violetta", empty_day))
        await self.click(self.action("adm:apply:"))
        self.assertFalse(self.store.is_day_blocked("violetta", empty_day))

    async def test_list_pagination_keeps_every_booking_accessible(self):
        for index in range(5):
            self.seed(starts_at=NOW + timedelta(days=index, hours=1), client_name=f"Клиент {index}")
        await self.click("adm:list:week:0")
        self.assertIn("Клиент 0", self.last_message.text)
        self.assertNotIn("Клиент 4", self.last_message.text)
        self.assertIn("Страница 1 из 2", self.last_message.text)
        await self.click("adm:list:week:1")
        self.assertIn("Клиент 4", self.last_message.text)
        self.assertNotIn("Клиент 0", self.last_message.text)
        self.assertIn("Страница 2 из 2", self.last_message.text)

    async def test_csv_export_is_authorized_and_neutralizes_spreadsheet_formulas(self):
        self.seed(client_name='=HYPERLINK("https://example.invalid")')
        await self.deliver("/admin_export")
        document = self.api_call.await_args.args[0]
        self.assertIsInstance(document, SendDocument)
        self.assertEqual(document.chat_id, 42)
        rows = list(csv.reader(io.StringIO(document.document.data.decode("utf-8-sig")), delimiter=";"))
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[1][6].startswith("'="))
        self.assertEqual(rows[1][7], "'+79991234567")
        self.assertEqual(rows[1][8], "Подтверждена")


if __name__ == "__main__":
    unittest.main()
