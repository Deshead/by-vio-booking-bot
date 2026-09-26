"""Customer actions through the real dispatcher, with no Telegram network I/O."""

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from itertools import count
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiogram import Bot
from aiogram.methods import EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message, MessageEntity, Update, User

from app.catalog import BUSINESS_TZ
from app.handlers.booking import BookingStates
from app.settings import Settings
from app.storage import BookingStore


TOKEN = "123456:TEST_TOKEN_FOR_LOCAL_TESTS"
NOW = datetime(2026, 9, 24, 8, tzinfo=BUSINESS_TZ)
with patch.dict(os.environ, {"BOT_TOKEN": TOKEN}):
    import bot as bot_app


class AppointmentTests(unittest.IsolatedAsyncioTestCase):
    ids = count(97000, 10)

    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = BookingStore(Path(temp.name) / "test.sqlite3")
        self.store.initialize()
        self.user_id = next(self.ids)
        self.other_id = self.user_id + 1
        self.settings = Settings(demo_mode=False)
        self.bot = Bot(TOKEN)
        self.addAsyncCleanup(self.bot.session.close)
        self.sequence = count(1)
        self.messages = {}
        api = patch.object(Bot, "__call__", new_callable=AsyncMock)
        self.api = api.start()
        self.addCleanup(api.stop)
        self.api.side_effect = self.response
        for module in ("app.catalog", "app.handlers.booking", "app.handlers.appointments", "app.handlers.menu"):
            clock = patch(f"{module}.now_local", return_value=NOW)
            clock.start()
            self.addCleanup(clock.stop)
        for user_id in (self.user_id, self.other_id):
            await self.state(user_id).clear()
            self.addAsyncCleanup(self.state(user_id).clear)

    def state(self, user_id=None):
        user_id = user_id or self.user_id
        return bot_app.dp.fsm.get_context(bot=self.bot, chat_id=user_id, user_id=user_id)

    def response(self, method, **kwargs):
        if isinstance(method, (SendMessage, EditMessageText)):
            chat_id = int(method.chat_id)
            message = Message(
                message_id=getattr(method, "message_id", None) or next(self.sequence),
                date=NOW, chat=Chat(id=chat_id, type="private"),
                from_user=User(id=self.bot.id, is_bot=True, first_name="Bot"),
                text=method.text,
                reply_markup=method.reply_markup if isinstance(method.reply_markup, InlineKeyboardMarkup) else None,
            )
            self.messages[chat_id] = message
            return message
        return True

    async def send(self, text, *, user_id=None):
        user_id = user_id or self.user_id
        message = Message(
            message_id=next(self.sequence), date=NOW,
            chat=Chat(id=user_id, type="private"),
            from_user=User(id=user_id, is_bot=False, first_name="Клиент"),
            text=text,
            entities=[MessageEntity(type="bot_command", offset=0, length=len(text))] if text.startswith("/") else [],
        )
        await bot_app.dp.feed_update(self.bot, Update(update_id=next(self.sequence), message=message), booking_store=self.store, settings=self.settings)

    async def click(self, data, *, user_id=None, message=None):
        user_id = user_id or self.user_id
        callback = CallbackQuery(
            id=str(next(self.sequence)),
            from_user=User(id=user_id, is_bot=False, first_name="Клиент"),
            chat_instance=str(user_id), message=message or self.messages[user_id], data=data,
        )
        await bot_app.dp.feed_update(self.bot, Update(update_id=next(self.sequence), callback_query=callback), booking_store=self.store, settings=self.settings)

    def button(self, fragment, *, user_id=None):
        message = self.messages[user_id or self.user_id]
        self.assertIsInstance(message.reply_markup, InlineKeyboardMarkup)
        for row in message.reply_markup.inline_keyboard:
            for item in row:
                if fragment in (item.callback_data or ""):
                    return item.callback_data
        self.fail(f"Button not found: {fragment}")

    def create(self, *, user_id=None, hour=9, days=0, reminders=False, duration=120):
        return self.store.create_booking(
            request_id=f"test-{next(self.sequence)}", user_id=user_id or self.user_id,
            service_id="manicure", service_name="💅 Маникюр",
            master_id="violetta", master_name="Виолетта",
            starts_at=NOW.replace(hour=hour) + timedelta(days=days), duration_minutes=duration,
            client_name="Анна", phone="+79991234567", reminders_enabled=reminders,
        )

    async def reach_confirmation(self):
        await self.send("/book")
        await self.click(self.button("service_manicure"))
        await self.click(self.button(":master:"))
        await self.click(self.button(":date:"))
        await self.click(self.button(":time:"))
        await self.send("Анна")
        await self.send("89991234567")
        self.assertEqual(await self.state().get_state(), BookingStates.confirm.state)

    async def begin_move(self, booking, time_suffix="1100"):
        await self.send("/my")
        await self.click(self.button("appt:move:"))
        self.assertEqual(await self.state().get_state(), BookingStates.date.state)
        await self.click(self.button(":date:"))
        await self.click(self.button(time_suffix))
        self.assertEqual(await self.state().get_state(), BookingStates.confirm.state)
        self.assertEqual(self.store.get_booking(booking.id), booking)

    async def test_cancellation_requires_confirmation_and_repeated_click_is_safe(self):
        booking = self.create()
        await self.send("/my")
        card = self.messages[self.user_id]
        await self.click(f"appt:cancelok:{booking.id}:{booking.version}:forged")
        self.assertEqual(self.store.get_booking(booking.id), booking)
        await self.click(self.button("appt:cancel:"), message=card)
        confirmation = self.button("appt:cancelok:")
        prompt = self.messages[self.user_id]
        self.assertEqual(self.store.get_booking(booking.id), booking)
        await self.click(confirmation)
        cancelled = self.store.get_booking(booking.id)
        self.assertEqual(cancelled.status, "cancelled")
        self.assertEqual(cancelled.version, booking.version + 1)
        await self.click(confirmation, message=prompt)
        self.assertEqual(self.store.get_booking(booking.id), cancelled)
        await self.send("/my")
        await self.click(self.button("appt:list:history:"))
        self.assertIn("Отменена", self.messages[self.user_id].text)
        self.assertNotIn("appt:move:", str(self.messages[self.user_id].reply_markup))

    async def test_ownership_and_stale_version_protect_customer_actions(self):
        booking = self.create()
        await self.send("/my", user_id=self.other_id)
        await self.click(f"appt:cancel:{booking.id}:{booking.version}", user_id=self.other_id)
        self.assertNotIn("Анна", self.messages[self.other_id].text)
        self.assertNotIn("+7999", self.messages[self.other_id].text)
        self.assertEqual(self.store.get_booking(booking.id), booking)
        await self.click(f"appt:move:{booking.id}:{booking.version}", user_id=self.other_id)
        self.assertIsNone(await self.state(self.other_id).get_state())
        await self.send("/my")
        await self.click(self.button("appt:cancel:"))
        confirmation = self.button("appt:cancelok:")
        changed = self.store.reschedule_booking(booking.id, user_id=self.user_id, expected_version=booking.version, starts_at=NOW.replace(hour=12), now=NOW)
        await self.click(confirmation)
        self.assertEqual(self.store.get_booking(booking.id), changed)
        self.assertEqual(changed.status, "confirmed")

    async def test_reschedule_preserves_original_until_confirmation_and_ignores_own_interval(self):
        booking = self.create(reminders=True)
        await self.begin_move(booking, "1000")  # The new interval overlaps this same booking.
        confirmation = self.button(":confirm:")
        summary = self.messages[self.user_id]
        self.assertIn("Прежнее время", summary.text)
        self.assertTrue((await self.state().get_data())["reminders_enabled"])
        await self.click(confirmation)
        changed = self.store.get_booking(booking.id)
        self.assertEqual(changed.id, booking.id)
        self.assertEqual(changed.version, booking.version + 1)
        self.assertEqual(changed.starts_at.astimezone(BUSINESS_TZ).hour, 10)
        self.assertTrue(changed.reminders_enabled)
        await self.click(confirmation, message=summary)
        self.assertEqual(self.store.get_booking(booking.id), changed)
        self.assertEqual(len(self.store.list_user_bookings(self.user_id)), 1)

    async def test_reschedule_conflict_and_abandonment_keep_original(self):
        booking = self.create()
        await self.begin_move(booking, "1100")
        confirmation = self.button(":confirm:")
        self.create(user_id=self.other_id, hour=11)
        await self.click(confirmation)
        self.assertEqual(self.store.get_booking(booking.id), booking)
        self.assertEqual(await self.state().get_state(), BookingStates.time.state)
        await self.send("/cancel")
        self.assertEqual(self.store.get_booking(booking.id), booking)
        self.assertIn("Прежняя запись сохранена", self.messages[self.user_id].text)

    async def test_reminders_are_off_by_default_and_require_explicit_toggle(self):
        await self.reach_confirmation()
        self.assertFalse((await self.state().get_data())["reminders_enabled"])
        await self.click(self.button(":confirm:"))
        first = self.store.list_user_bookings(self.user_id)[0]
        self.assertFalse(first.reminders_enabled)
        await self.reach_confirmation()
        old_confirmation = self.button(":confirm:")
        old_message = self.messages[self.user_id]
        await self.click(self.button(":reminder:"))
        self.assertTrue((await self.state().get_data())["reminders_enabled"])
        fresh_confirmation = self.button(":confirm:")
        self.assertNotEqual(old_confirmation, fresh_confirmation)
        await self.click(old_confirmation, message=old_message)
        self.assertEqual(len(self.store.list_user_bookings(self.user_id)), 1)
        await self.click(fresh_confirmation)
        bookings = self.store.list_user_bookings(self.user_id)
        self.assertEqual(len(bookings), 2)
        self.assertTrue(bookings[1].reminders_enabled)

    async def test_cards_paginate_and_history_is_separate(self):
        first = self.create()
        second = self.create(hour=12)
        past = self.create(days=-1)
        await self.send("/my")
        self.assertIn(f"Запись №{first.id}", self.messages[self.user_id].text)
        self.assertNotIn(f"Запись №{second.id}", self.messages[self.user_id].text)
        self.assertIn("1 из 2", self.messages[self.user_id].text)
        await self.click(self.button("appt:list:upcoming:1"))
        self.assertIn(f"Запись №{second.id}", self.messages[self.user_id].text)
        await self.click(self.button("appt:list:history:"))
        self.assertIn(f"Запись №{past.id}", self.messages[self.user_id].text)
        self.assertIn("Завершена", self.messages[self.user_id].text)
        self.assertNotIn("appt:cancel:", str(self.messages[self.user_id].reply_markup))

    async def test_reschedule_stale_version_cannot_overwrite_admin_change(self):
        booking = self.create()
        await self.begin_move(booking, "1100")
        confirmation = self.button(":confirm:")
        changed = self.store.reschedule_booking(booking.id, user_id=self.user_id, expected_version=booking.version, starts_at=NOW.replace(hour=14), now=NOW)
        await self.click(confirmation)
        self.assertEqual(self.store.get_booking(booking.id), changed)
        self.assertIsNone(await self.state().get_state())

    async def test_reschedule_respects_saved_duration_after_catalog_change(self):
        booking = self.create(duration=180)
        await self.send("/my")
        await self.click(self.button("appt:move:"))
        await self.click(self.button(":date:"))
        choices = [button.callback_data for row in self.messages[self.user_id].reply_markup.inline_keyboard for button in row]
        self.assertFalse(any("1600" in value for value in choices))
        await self.click(self.button("1500"))
        await self.click(self.button(":confirm:"))
        moved = self.store.get_booking(booking.id)
        self.assertEqual(moved.duration_minutes, 180)
        self.assertEqual(moved.starts_at.astimezone(BUSINESS_TZ).hour, 15)
        self.assertEqual(moved.ends_at.astimezone(BUSINESS_TZ).hour, 18)


if __name__ == "__main__":
    unittest.main()
